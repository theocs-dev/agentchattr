"""Channel registry — single source of truth for channel names, the active cap,
and the active/archived lists.

Design constraints (audited via tandem, see PR notes):
- PURE / STATELESS: every function receives the `settings` dict (and `config`
  where a cap is needed) explicitly. No module-level mutable state, no global
  `room_settings`, no persistence (`_save_settings` stays in app.py), no import
  of `app`. Callers own read -> mutate -> save -> result -> broadcast.
- Soft archive is METADATA only: archiving moves a name between
  `settings["channels"]` (active) and `settings["archived_channels"]`. It never
  touches the message store. Only an explicit destructive purge deletes
  messages (caller calls `store.delete_channel`).
- Invariants enforced by `normalize_on_load` and respected by every mutator:
  `general` is always active and never archived; active ∩ archived = ∅ (active
  wins on conflict); no duplicates; names match `NAME_RE`.
- `unregistered` is a DERIVED state, never materialised here: a name present in
  the message store but absent from both lists. Import reports it as a category;
  recovery is `restore`.
"""

import re

GENERAL = "general"

# Unified channel-name rule. Previously split between app.py's WS handler
# (`{0,19}`, <=20 chars) and archive.py (`{0,29}`, <=30). Unified to <=20 to
# match the UI input and the tandem skill's `_valid_channel`.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,19}$")

MAX_ACTIVE_DEFAULT = 8
MAX_ARCHIVED_DEFAULT = 50


# --- limits ---------------------------------------------------------------

def max_active(config: dict) -> int:
    """Active-channel cap (general included). Clamped to >= 1 so `general`
    always fits, even if someone configures 0."""
    raw = (config or {}).get("server", {}).get("max_channels", MAX_ACTIVE_DEFAULT)
    try:
        val = int(raw)
    except (TypeError, ValueError):
        val = MAX_ACTIVE_DEFAULT
    return max(1, val)


def max_archived(config: dict) -> int:
    raw = (config or {}).get("server", {}).get("max_archived_channels", MAX_ARCHIVED_DEFAULT)
    try:
        val = int(raw)
    except (TypeError, ValueError):
        val = MAX_ARCHIVED_DEFAULT
    return max(0, val)


# --- helpers --------------------------------------------------------------

def normalize(name) -> str:
    return (name or "").strip().lower()


def is_valid(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


def _active(settings: dict) -> list:
    lst = settings.get("channels")
    if not isinstance(lst, list):
        lst = [GENERAL]
        settings["channels"] = lst
    return lst


def _archived(settings: dict) -> list:
    lst = settings.get("archived_channels")
    if not isinstance(lst, list):
        lst = []
        settings["archived_channels"] = lst
    return lst


def is_active(settings: dict, name: str) -> bool:
    return normalize(name) in _active(settings)


def is_archived(settings: dict, name: str) -> bool:
    return normalize(name) in _archived(settings)


def _result(ok: bool, reason: str, name: str) -> dict:
    return {"ok": ok, "reason": reason, "name": name}


# --- mutators (return a channel_result dict) ------------------------------

def create(settings: dict, name: str, config: dict) -> dict:
    """reason ∈ created | exists | invalid | full | archived."""
    name = normalize(name)
    if not is_valid(name):
        return _result(False, "invalid", name)
    if name == GENERAL or name in _active(settings):
        return _result(True, "exists", name)
    if name in _archived(settings):
        # Exists but hidden — caller (skill/UI) must restore explicitly.
        return _result(False, "archived", name)
    if len(_active(settings)) >= max_active(config):
        return _result(False, "full", name)
    _active(settings).append(name)
    return _result(True, "created", name)


def archive(settings: dict, name: str) -> dict:
    """Soft-close: move active -> archived. Never touches the store.
    reason ∈ archived | general | invalid | not_found."""
    name = normalize(name)
    if not is_valid(name):
        return _result(False, "invalid", name)
    if name == GENERAL:
        return _result(False, "general", name)
    if name in _active(settings):
        _active(settings).remove(name)
        if name not in _archived(settings):
            _archived(settings).append(name)
        return _result(True, "archived", name)
    if name in _archived(settings):
        return _result(True, "archived", name)  # idempotent
    return _result(False, "not_found", name)


def restore(settings: dict, name: str, config: dict) -> dict:
    """Bring an archived (or unregistered-but-known) channel back to active.
    The caller is responsible for confirming a non-archived name actually
    exists in the store (`store.has_channel`) before restoring it.
    reason ∈ restored | full | invalid."""
    name = normalize(name)
    if not is_valid(name):
        return _result(False, "invalid", name)
    if name in _active(settings):
        return _result(True, "restored", name)  # idempotent
    if len(_active(settings)) >= max_active(config):
        return _result(False, "full", name)
    if name in _archived(settings):
        _archived(settings).remove(name)
    _active(settings).append(name)
    return _result(True, "restored", name)


def purge(settings: dict, name: str) -> dict:
    """Mark a channel for destructive removal: drop it from both lists. The
    caller performs the destructive `store.delete_channel(name)` afterwards.
    reason ∈ purged | general | invalid."""
    name = normalize(name)
    if not is_valid(name):
        return _result(False, "invalid", name)
    if name == GENERAL:
        return _result(False, "general", name)
    if name in _active(settings):
        _active(settings).remove(name)
    if name in _archived(settings):
        _archived(settings).remove(name)
    return _result(True, "purged", name)


def rename(settings: dict, old: str, new: str) -> dict:
    """Rename within whichever list holds `old` (active or archived).
    reason ∈ renamed | general | invalid | exists | not_found."""
    old = normalize(old)
    new = normalize(new)
    if old == GENERAL:
        return _result(False, "general", old)
    if not is_valid(new):
        return _result(False, "invalid", new)
    if new == GENERAL or new in _active(settings) or new in _archived(settings):
        return _result(False, "exists", new)
    if old in _active(settings):
        _active(settings)[_active(settings).index(old)] = new
        return _result(True, "renamed", new)
    if old in _archived(settings):
        _archived(settings)[_archived(settings).index(old)] = new
        return _result(True, "renamed", new)
    return _result(False, "not_found", old)


def import_resolve(settings: dict, name: str, config: dict) -> tuple[str, str]:
    """Resolve a channel for an imported message WITHOUT ever remapping to
    `general` (which would destroy channel semantics). Returns
    (resolved_name, category) where category ∈
    existing | created | archived | unregistered.

    `unregistered` keeps the message under its original channel name (it stays
    recoverable later via `restore`); it is reported but never materialised in
    settings.
    """
    name = normalize(name)
    if not name:
        return (GENERAL, "existing")
    if name == GENERAL or name in _active(settings):
        return (name, "existing")
    if name in _archived(settings):
        return (name, "archived")  # keep under its (hidden) name
    if is_valid(name) and len(_active(settings)) < max_active(config):
        _active(settings).append(name)
        return (name, "created")
    if is_valid(name) and len(_archived(settings)) < max_archived(config):
        _archived(settings).append(name)
        return (name, "archived")
    # Over caps, or invalid name: keep messages under the original name,
    # neither active nor archived. Recoverable later.
    return (name, "unregistered")


def normalize_on_load(settings: dict) -> bool:
    """Enforce all invariants on persisted settings. Returns True iff anything
    was corrected (so the caller can save only when needed, avoiding churn).
    Caps use the registry defaults here; `max_active`/`max_archived` with the
    live config are applied by the caller right after, but we also cap with the
    defaults as a floor of sanity. Overflow is moved (active->archived) or
    dropped (archived->unregistered), NEVER purged.
    """
    before = (list(_active(settings)), list(_archived(settings)))

    active = [c for c in _active(settings) if is_valid(c)]
    archived = [c for c in _archived(settings) if is_valid(c)]

    # Dedupe, preserving order.
    active = list(dict.fromkeys(active))
    archived = list(dict.fromkeys(archived))

    # general: always active, first, never archived.
    archived = [c for c in archived if c != GENERAL]
    active = [c for c in active if c != GENERAL]
    active.insert(0, GENERAL)

    # active wins on conflict.
    active_set = set(active)
    archived = [c for c in archived if c not in active_set]

    settings["channels"] = active
    settings["archived_channels"] = archived

    after = (list(_active(settings)), list(_archived(settings)))
    return before != after


def apply_caps(settings: dict, config: dict) -> bool:
    """Enforce the configured caps after `normalize_on_load`. Active overflow
    (newest non-general first kept) moves to archived; archived overflow is
    dropped to `unregistered` (messages stay in the store). Returns True iff
    anything changed.
    """
    before = (list(_active(settings)), list(_archived(settings)))
    cap_a = max_active(config)
    cap_z = max_archived(config)

    active = _active(settings)
    archived = _archived(settings)

    # Active overflow -> archived (keep general + the first cap_a-1 others).
    if len(active) > cap_a:
        keep = active[:cap_a]
        overflow = active[cap_a:]
        settings["channels"] = keep
        for c in overflow:
            if c != GENERAL and c not in archived:
                archived.insert(0, c)

    # Archived overflow -> dropped (unregistered).
    if len(archived) > cap_z:
        del archived[cap_z:]

    after = (list(_active(settings)), list(_archived(settings)))
    return before != after
