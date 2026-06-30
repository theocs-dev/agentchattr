"""Tests for channel_registry — the pure single-source-of-truth for channel
names, the active cap, and the active/archived lists.

These are deliberately store-free: archiving is metadata only, so the registry
never needs a store. Recoverability that *does* depend on the store
(`store.has_channel`) is exercised at the app layer, not here.
"""

import unittest

import channel_registry as chreg


CFG = {"server": {"max_channels": 3, "max_archived_channels": 4}}


def _settings(active=None, archived=None):
    return {
        "channels": list(active if active is not None else ["general"]),
        "archived_channels": list(archived or []),
    }


class Limits(unittest.TestCase):
    def test_default_when_absent(self):
        self.assertEqual(chreg.max_active({}), chreg.MAX_ACTIVE_DEFAULT)
        self.assertEqual(chreg.max_archived({}), chreg.MAX_ARCHIVED_DEFAULT)

    def test_from_config(self):
        self.assertEqual(chreg.max_active(CFG), 3)
        self.assertEqual(chreg.max_archived(CFG), 4)

    def test_clamped_to_at_least_one(self):
        # 0 / garbage must never let general fall out of the active list.
        self.assertEqual(chreg.max_active({"server": {"max_channels": 0}}), 1)
        self.assertEqual(chreg.max_active({"server": {"max_channels": "nope"}}),
                         chreg.MAX_ACTIVE_DEFAULT)


class Validate(unittest.TestCase):
    def test_valid_names(self):
        for n in ("general", "a", "decision-x", "chan-round9", "a" * 20):
            self.assertTrue(chreg.is_valid(n), n)

    def test_invalid_names(self):
        for n in ("", "-bad", "Bad", "a b", "x" * 21, "té"):
            self.assertFalse(chreg.is_valid(n), n)

    def test_unified_length_cap_is_20(self):
        # Previously archive.py allowed 30; the registry unifies to 20.
        self.assertTrue(chreg.is_valid("a" * 20))
        self.assertFalse(chreg.is_valid("a" * 21))


class Create(unittest.TestCase):
    def test_created(self):
        s = _settings()
        self.assertEqual(chreg.create(s, "plan", CFG),
                         {"ok": True, "reason": "created", "name": "plan"})
        self.assertIn("plan", s["channels"])

    def test_exists_active_and_general(self):
        s = _settings(["general", "plan"])
        self.assertEqual(chreg.create(s, "plan", CFG)["reason"], "exists")
        self.assertEqual(chreg.create(s, "general", CFG)["reason"], "exists")

    def test_invalid(self):
        s = _settings()
        self.assertEqual(chreg.create(s, "Bad Name", CFG)["reason"], "invalid")

    def test_full(self):
        s = _settings(["general", "a", "b"])  # cap is 3
        r = chreg.create(s, "c", CFG)
        self.assertEqual(r, {"ok": False, "reason": "full", "name": "c"})
        self.assertNotIn("c", s["channels"])

    def test_create_on_archived_name_reports_archived(self):
        s = _settings(["general"], ["old"])
        r = chreg.create(s, "old", CFG)
        self.assertEqual(r["reason"], "archived")
        self.assertNotIn("old", s["channels"])  # not silently restored


class Archive(unittest.TestCase):
    def test_moves_active_to_archived(self):
        s = _settings(["general", "plan"])
        r = chreg.archive(s, "plan")
        self.assertEqual(r["reason"], "archived")
        self.assertEqual(s["channels"], ["general"])
        self.assertEqual(s["archived_channels"], ["plan"])

    def test_general_refused(self):
        s = _settings(["general", "plan"])
        self.assertEqual(chreg.archive(s, "general")["reason"], "general")
        self.assertIn("general", s["channels"])

    def test_idempotent_when_already_archived(self):
        s = _settings(["general"], ["plan"])
        self.assertTrue(chreg.archive(s, "plan")["ok"])
        self.assertEqual(s["archived_channels"], ["plan"])

    def test_not_found(self):
        s = _settings(["general"])
        self.assertEqual(chreg.archive(s, "ghost")["reason"], "not_found")


class Restore(unittest.TestCase):
    def test_restores_from_archived(self):
        s = _settings(["general"], ["plan"])
        r = chreg.restore(s, "plan", CFG)
        self.assertEqual(r["reason"], "restored")
        self.assertIn("plan", s["channels"])
        self.assertNotIn("plan", s["archived_channels"])

    def test_full_blocks_restore(self):
        s = _settings(["general", "a", "b"], ["plan"])  # active cap 3 reached
        r = chreg.restore(s, "plan", CFG)
        self.assertEqual(r["reason"], "full")
        self.assertIn("plan", s["archived_channels"])  # untouched

    def test_idempotent_when_already_active(self):
        s = _settings(["general", "plan"])
        self.assertEqual(chreg.restore(s, "plan", CFG)["reason"], "restored")

    def test_restores_unregistered_name(self):
        # Name not in either list (would be store-backed in real use).
        s = _settings(["general"])
        r = chreg.restore(s, "hidden", CFG)
        self.assertEqual(r["reason"], "restored")
        self.assertIn("hidden", s["channels"])


class Purge(unittest.TestCase):
    def test_removes_from_both_lists(self):
        s = _settings(["general", "a"], ["b"])
        self.assertEqual(chreg.purge(s, "a")["reason"], "purged")
        self.assertEqual(chreg.purge(s, "b")["reason"], "purged")
        self.assertEqual(s["channels"], ["general"])
        self.assertEqual(s["archived_channels"], [])

    def test_general_refused(self):
        s = _settings(["general"])
        self.assertEqual(chreg.purge(s, "general")["reason"], "general")


class Rename(unittest.TestCase):
    def test_active(self):
        s = _settings(["general", "old"])
        self.assertEqual(chreg.rename(s, "old", "new")["reason"], "renamed")
        self.assertEqual(s["channels"], ["general", "new"])

    def test_archived(self):
        s = _settings(["general"], ["old"])
        self.assertEqual(chreg.rename(s, "old", "new")["reason"], "renamed")
        self.assertEqual(s["archived_channels"], ["new"])

    def test_collision_refused(self):
        s = _settings(["general", "a"], ["b"])
        self.assertEqual(chreg.rename(s, "a", "b")["reason"], "exists")
        self.assertEqual(chreg.rename(s, "a", "general")["reason"], "exists")

    def test_general_and_invalid(self):
        s = _settings(["general", "a"])
        self.assertEqual(chreg.rename(s, "general", "x")["reason"], "general")
        # "Bad Name" normalizes to "bad name" (space) -> invalid. ("Bad" alone
        # would normalize to the valid "bad", which is intended.)
        self.assertEqual(chreg.rename(s, "a", "Bad Name")["reason"], "invalid")


class NormalizeOnLoad(unittest.TestCase):
    def test_noop_returns_false(self):
        s = _settings(["general", "a"], ["b"])
        self.assertFalse(chreg.normalize_on_load(s))

    def test_inserts_general_first(self):
        s = _settings(["a", "b"], [])
        self.assertTrue(chreg.normalize_on_load(s))
        self.assertEqual(s["channels"][0], "general")

    def test_dedup_and_active_wins_over_archived(self):
        s = _settings(["general", "a", "a"], ["a", "b", "b"])
        self.assertTrue(chreg.normalize_on_load(s))
        self.assertEqual(s["channels"], ["general", "a"])
        self.assertEqual(s["archived_channels"], ["b"])  # 'a' removed (active wins)

    def test_general_never_archived(self):
        s = _settings(["general"], ["general", "b"])
        chreg.normalize_on_load(s)
        self.assertNotIn("general", s["archived_channels"])

    def test_invalid_names_dropped_valid_normalized(self):
        # "Bad Name" (space) is unsalvageable -> dropped. "AlsoBad" just needs
        # lowercasing -> kept as "alsobad".
        s = _settings(["general", "Bad Name", "ok"], ["AlsoBad"])
        chreg.normalize_on_load(s)
        self.assertEqual(s["channels"], ["general", "ok"])
        self.assertEqual(s["archived_channels"], ["alsobad"])

    def test_robust_to_non_list_channels(self):
        # Corrupt settings.json must not crash the boot; it gets coerced and
        # the correction must be reported as changed (so the caller persists it).
        s = {"channels": "oops", "archived_channels": None}
        self.assertTrue(chreg.normalize_on_load(s))
        self.assertEqual(s["channels"], ["general"])
        self.assertEqual(s["archived_channels"], [])

    def test_robust_to_non_string_entries(self):
        s = {"channels": ["general", 123, {"x": 1}, "ok"], "archived_channels": [None, "arch"]}
        self.assertTrue(chreg.normalize_on_load(s))  # no TypeError
        self.assertEqual(s["channels"], ["general", "ok"])
        self.assertEqual(s["archived_channels"], ["arch"])

    def test_normalizes_case_instead_of_dropping(self):
        # "General"/"Plan" should be lowercased, not discarded.
        s = {"channels": ["General", "Plan"], "archived_channels": []}
        chreg.normalize_on_load(s)
        self.assertEqual(s["channels"], ["general", "plan"])

    def test_correction_is_idempotent(self):
        s = {"channels": "oops", "archived_channels": None}
        self.assertTrue(chreg.normalize_on_load(s))
        self.assertFalse(chreg.normalize_on_load(s))  # second pass: nothing to fix


class Normalize(unittest.TestCase):
    def test_coerces_non_strings_without_crashing(self):
        self.assertEqual(chreg.normalize(123), "")
        self.assertEqual(chreg.normalize(None), "")
        self.assertEqual(chreg.normalize({"x": 1}), "")
        self.assertEqual(chreg.normalize("  Plan "), "plan")
        self.assertFalse(chreg.is_valid(123))


class ApplyCaps(unittest.TestCase):
    def test_active_overflow_moves_to_archived(self):
        s = _settings(["general", "a", "b", "c", "d"], [])  # cap 3
        self.assertTrue(chreg.apply_caps(s, CFG))
        self.assertEqual(s["channels"], ["general", "a", "b"])
        self.assertIn("c", s["archived_channels"])
        self.assertIn("d", s["archived_channels"])

    def test_archived_overflow_dropped(self):
        s = _settings(["general"], ["a", "b", "c", "d", "e", "f"])  # archived cap 4
        self.assertTrue(chreg.apply_caps(s, CFG))
        self.assertEqual(len(s["archived_channels"]), 4)

    def test_noop_returns_false(self):
        s = _settings(["general", "a"], ["b"])
        self.assertFalse(chreg.apply_caps(s, CFG))


class ImportResolve(unittest.TestCase):
    def test_blank_is_general(self):
        s = _settings()
        self.assertEqual(chreg.import_resolve(s, "", CFG), ("general", "existing"))

    def test_existing_active(self):
        s = _settings(["general", "plan"])
        self.assertEqual(chreg.import_resolve(s, "plan", CFG), ("plan", "existing"))

    def test_created_within_cap(self):
        s = _settings(["general"])
        name, cat = chreg.import_resolve(s, "newone", CFG)
        self.assertEqual((name, cat), ("newone", "created"))
        self.assertIn("newone", s["channels"])

    def test_archived_when_active_full(self):
        s = _settings(["general", "a", "b"])  # active cap 3 reached
        name, cat = chreg.import_resolve(s, "extra", CFG)
        self.assertEqual(cat, "archived")
        self.assertIn("extra", s["archived_channels"])

    def test_unregistered_when_both_full(self):
        s = _settings(["general", "a", "b"], ["w", "x", "y", "z"])  # both caps full
        name, cat = chreg.import_resolve(s, "spill", CFG)
        self.assertEqual((name, cat), ("spill", "unregistered"))
        self.assertNotIn("spill", s["channels"])
        self.assertNotIn("spill", s["archived_channels"])

    def test_never_remaps_to_general(self):
        # Even when everything is full, a non-general channel keeps its name.
        s = _settings(["general", "a", "b"], ["w", "x", "y", "z"])
        name, _ = chreg.import_resolve(s, "spill", CFG)
        self.assertNotEqual(name, "general")

    def test_invalid_name_is_unregistered_not_general(self):
        s = _settings()
        name, cat = chreg.import_resolve(s, "Bad Name", CFG)
        self.assertEqual(cat, "unregistered")
        self.assertNotEqual(name, "general")


if __name__ == "__main__":
    unittest.main()
