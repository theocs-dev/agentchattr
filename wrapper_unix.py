"""Mac/Linux agent injection — uses tmux send-keys to type into the agent CLI.

Called by wrapper.py on Mac and Linux. Requires tmux to be installed.
  - Mac:   brew install tmux
  - Linux: apt install tmux  (or yum, pacman, etc.)

How it works:
  1. Creates a tmux session running the agent CLI
  2. Queue watcher sends keystrokes via 'tmux send-keys'
  3. Wrapper attaches to the session so you see the full TUI
  4. Ctrl+B, D to detach (agent keeps running in background)
"""

import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid


_CLAUDE_THINKING_BLOCK_ERROR_RE = re.compile(
    r"(?:thinking|redacted_thinking).*blocks.*cannot be modified|"
    r"blocks.*(?:thinking|redacted_thinking).*cannot be modified",
    re.IGNORECASE | re.DOTALL,
)
_CLAUDE_INTERRUPTED_PROMPT_RE = re.compile(
    r"Interrupted.*What should Claude do instead\?",
    re.IGNORECASE | re.DOTALL,
)


def _session_exists(session_name: str) -> bool:
    """Return True while the tmux session is still alive."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    return result.returncode == 0


def _capture_pane(session_name: str, start: str = "-45") -> str:
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-S", start],
            capture_output=True,
            timeout=2,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", errors="replace")


def _recent_nonempty_lines(text: str, max_lines: int) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-max_lines:])


def _looks_like_claude_thinking_block_error(text: str) -> bool:
    return bool(text and _CLAUDE_THINKING_BLOCK_ERROR_RE.search(_recent_nonempty_lines(text, 20)))


def _looks_like_claude_interrupted_prompt(text: str) -> bool:
    return bool(text and _CLAUDE_INTERRUPTED_PROMPT_RE.search(_recent_nonempty_lines(text, 8)))


def _refresh_claude_session_id(args: list[str]) -> str:
    next_id = str(uuid.uuid4())
    for idx, value in enumerate(args[:-1]):
        if value == "--session-id":
            args[idx + 1] = next_id
            return next_id
    args.extend(["--session-id", next_id])
    return next_id


def _clear_event_payload(
    state: str,
    *,
    reason: str,
    action: dict | None = None,
    generation=None,
    session_ref=None,
) -> dict:
    payload = {
        "state": state,
        "strategy": "session_restart",
        "confirmation": "wrapper_machine",
        "reason": reason,
    }
    if action:
        if action.get("requested_at") is not None:
            payload["requested_at"] = action.get("requested_at")
        if action.get("request_id") is not None:
            payload["request_id"] = action.get("request_id")
    if generation is not None:
        payload["generation"] = generation
    if session_ref is not None:
        payload["session_ref"] = session_ref
    return payload


def _handle_claude_clear_context_action(
    action: dict,
    *,
    agent: str,
    session_name: str,
    claude_session_state=None,
    is_busy_fn=None,
    session_exists_fn=_session_exists,
    request_restart_fn=None,
    report_clear_event=None,
) -> bool:
    """Validate a clear action and request a controlled Claude session restart."""
    if action.get("type") != "clear_context":
        return False

    report_clear_event = report_clear_event or (lambda payload: None)
    if agent != "claude" or action.get("strategy") != "session_restart":
        report_clear_event(_clear_event_payload("failed", reason="unsupported_clear_action", action=action))
        return True
    if action.get("confirmed_by_user") is not True:
        report_clear_event(_clear_event_payload("failed", reason="missing_user_confirmation", action=action))
        return True
    if is_busy_fn is not None and is_busy_fn():
        report_clear_event(_clear_event_payload("failed", reason="adapter_refused_busy", action=action))
        return True
    if not session_exists_fn(session_name):
        report_clear_event(_clear_event_payload("failed", reason="terminal_session_not_found", action=action))
        return True
    if request_restart_fn is None:
        report_clear_event(_clear_event_payload("failed", reason="restart_handler_unavailable", action=action))
        return True

    session_ref = None
    generation = None
    if claude_session_state is not None:
        session_ref, generation = claude_session_state.snapshot()
    report_clear_event(_clear_event_payload(
        "pending",
        reason="session_restart_requested",
        action=action,
        generation=generation,
        session_ref=session_ref,
    ))
    if not request_restart_fn({"type": "clear_context", "action": action}):
        report_clear_event(_clear_event_payload("failed", reason="restart_already_pending", action=action))
    return True


def _finalize_claude_clear_restart(
    action: dict,
    *,
    new_session_id: str,
    generation: int | None,
    verify_session_fn=None,
    report_clear_event=None,
    report_usage_event=None,
    verify_timeout: float = 15.0,
) -> bool:
    """Report clear result after a controlled restart with a new session id."""
    report_clear_event = report_clear_event or (lambda payload: None)
    report_usage_event = report_usage_event or (lambda payload: None)

    usage_payload = {
        "provider": "claude",
        "status": "unavailable",
        "reason": "cleared; awaiting fresh telemetry",
        "updated_at": int(time.time()),
        "session_ref": new_session_id,
    }
    if generation is not None:
        usage_payload["generation"] = generation
    report_usage_event(usage_payload)

    proof_observed = False
    if verify_session_fn is not None:
        deadline = time.time() + max(0.0, verify_timeout)
        while True:
            try:
                proof_observed = bool(verify_session_fn(new_session_id))
            except Exception:
                proof_observed = False
            if proof_observed or time.time() >= deadline:
                break
            time.sleep(0.25)

    if proof_observed:
        report_clear_event(_clear_event_payload(
            "confirmed",
            reason="new_session_file_observed",
            action=action,
            generation=generation,
            session_ref=new_session_id,
        ))
        return True

    report_clear_event(_clear_event_payload(
        "pending",
        reason="awaiting_session_file",
        action=action,
        generation=generation,
        session_ref=new_session_id,
    ))
    return False


def _request_claude_recovery_restart(
    reason: str,
    *,
    session_name: str,
    no_restart: bool,
    set_restart_request_fn,
    kill_session_fn,
    session_exists_fn=_session_exists,
    report_terminal_event=None,
    sleep_fn=time.sleep,
    now_fn=time.time,
    timeout: float = 15.0,
    print_fn=print,
) -> bool:
    if no_restart:
        message = (
            f"Claude recovery needed ({reason}) but --no-restart prevented automatic restart; "
            f"trigger skipped. Reattach with: tmux attach -t {session_name}; "
            "recover or restart Claude manually, then resend the message."
        )
        print_fn(f"  {message}")
        if report_terminal_event is not None:
            report_terminal_event("recovery_blocked", message)
        return False

    if not set_restart_request_fn({"type": "recovery", "reason": reason}):
        return False
    print_fn(f"  Claude recovery: {reason}; restarting with a fresh session id before injection.")
    kill_session_fn(session_name)
    deadline = now_fn() + timeout
    while now_fn() < deadline:
        if session_exists_fn(session_name):
            sleep_fn(1.0)
            return True
        sleep_fn(0.25)
    print_fn("  Claude recovery: timed out waiting for restarted tmux session; trigger skipped.")
    return False


def _check_tmux():
    """Verify tmux is installed, exit with helpful message if not."""
    if shutil.which("tmux"):
        return
    print("\n  Error: tmux is required for auto-trigger on Mac/Linux.")
    if sys.platform == "darwin":
        print("  Install: brew install tmux")
    else:
        print("  Install: apt install tmux  (or yum/pacman equivalent)")
    sys.exit(1)


def _build_agent_cmd(command, extra_args, strip_env=None, inject_env=None) -> str:
    agent_cmd = " ".join(
        [shlex.quote(command)] + [shlex.quote(a) for a in extra_args]
    )

    # Build env(1) prefix for the command INSIDE the tmux session.
    # subprocess.run(env=...) only affects the tmux client binary — the
    # session shell inherits from the tmux server instead.  Use env(1)
    # to set (-u to unset, VAR=val to inject) vars in the actual session.
    env_parts = []
    if strip_env:
        env_parts.extend(f"-u {shlex.quote(v)}" for v in strip_env)
    if inject_env:
        env_parts.extend(
            f"{shlex.quote(k)}={shlex.quote(v)}"
            for k, v in inject_env.items()
        )
    if env_parts:
        agent_cmd = f"env {' '.join(env_parts)} {agent_cmd}"
    return agent_cmd


def inject(text: str, *, tmux_session: str, delay: float = 0.3):
    """Send text + Enter to a tmux session via send-keys."""
    # Use -l to send text literally (avoids misinterpreting as key names),
    # then send Enter as a separate key press
    subprocess.run(
        ["tmux", "send-keys", "-t", tmux_session, "-l", text],
        capture_output=True,
    )
    # Scale delay with text length so longer prompts get more processing time
    time.sleep(max(delay, len(text) * 0.001))
    subprocess.run(
        ["tmux", "send-keys", "-t", tmux_session, "Enter"],
        capture_output=True,
    )


def get_activity_checker(session_name, trigger_flag=None):
    """Return a callable that detects tmux pane output by hashing content."""
    last_hash = [None]

    def check():
        # External trigger: queue watcher injected a message
        if trigger_flag is not None and trigger_flag[0]:
            trigger_flag[0] = False
            return True
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", session_name, "-p"],
                capture_output=True, timeout=2,
            )
            h = hash(result.stdout)
            changed = last_hash[0] is not None and h != last_hash[0]
            last_hash[0] = h
            return changed
        except Exception:
            return False

    return check


def run_agent(
    command,
    extra_args,
    cwd,
    env,
    queue_file,
    agent,
    no_restart,
    start_watcher,
    strip_env=None,
    pid_holder=None,
    session_name=None,
    inject_env=None,
    inject_delay: float = 0.3,
    claude_session_state=None,
    start_action_watcher=None,
    is_busy_fn=None,
    report_clear_event=None,
    report_usage_event=None,
    report_terminal_event=None,
    verify_claude_session_fn=None,
):
    """Run agent inside a tmux session, inject via tmux send-keys."""
    _check_tmux()

    session_name = session_name or f"agentchattr-{agent}"

    # Resolve cwd to absolute path (tmux -c needs it)
    from pathlib import Path
    abs_cwd = str(Path(cwd).resolve())

    # Wire up injection with the tmux session name
    restart_request = [None]
    restart_lock = __import__("threading").Lock()

    def _set_restart_request(request: dict) -> bool:
        with restart_lock:
            if restart_request[0] is not None:
                return False
            restart_request[0] = request
            return True

    def _take_restart_request() -> dict | None:
        with restart_lock:
            request = restart_request[0]
            restart_request[0] = None
            return request

    def _has_restart_request() -> bool:
        with restart_lock:
            return restart_request[0] is not None

    def _request_fresh_claude_session(reason: str) -> bool:
        return _request_claude_recovery_restart(
            reason,
            session_name=session_name,
            no_restart=no_restart,
            set_restart_request_fn=_set_restart_request,
            kill_session_fn=lambda target: subprocess.run(
                ["tmux", "kill-session", "-t", target],
                capture_output=True,
            ),
            report_terminal_event=report_terminal_event,
        )

    def inject_fn(text):
        if agent == "claude":
            pane = _capture_pane(session_name)
            if _looks_like_claude_thinking_block_error(pane):
                if not _request_fresh_claude_session("thinking-block API error detected"):
                    return
            elif _looks_like_claude_interrupted_prompt(pane):
                if not _request_fresh_claude_session("interrupted prompt detected"):
                    return
        inject(text, tmux_session=session_name, delay=inject_delay)

    start_watcher(inject_fn)
    if agent == "claude" and start_action_watcher is not None:
        def _handle_action(action: dict):
            _handle_claude_clear_context_action(
                action,
                agent=agent,
                session_name=session_name,
                claude_session_state=claude_session_state,
                is_busy_fn=is_busy_fn,
                request_restart_fn=_request_clear_context_restart,
                report_clear_event=report_clear_event,
            )

        def _request_clear_context_restart(request: dict) -> bool:
            if not _set_restart_request(request):
                return False
            print("  Claude clear: restarting with a fresh session id.")
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True,
            )
            return True

        start_action_watcher(_handle_action)

    print(f"  Using tmux session: {session_name}")
    print(f"  Detach: Ctrl+B, D  (agent keeps running)")
    print(f"  Reattach: tmux attach -t {session_name}\n")

    while True:
        try:
            # Clean up stale session from a previous crash
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True,
            )

            restart_for_this_launch = _take_restart_request() if agent == "claude" else None
            clear_restart_action = None
            clear_restart_generation = None
            clear_restart_session_id = None
            if agent == "claude" and restart_for_this_launch:
                session_id = _refresh_claude_session_id(extra_args)
                if claude_session_state is not None:
                    clear_restart_generation = claude_session_state.set_session_id(session_id)
                clear_restart_session_id = session_id
                if restart_for_this_launch.get("type") == "clear_context":
                    clear_restart_action = restart_for_this_launch.get("action") or {}
                    print(f"  Claude clear: new --session-id {session_id}")
                else:
                    print(f"  Claude recovery: new --session-id {session_id}")

            agent_cmd = _build_agent_cmd(command, extra_args, strip_env, inject_env)

            # Create tmux session running the agent CLI
            result = subprocess.run(
                ["tmux", "new-session", "-d", "-s", session_name,
                 "-c", abs_cwd, agent_cmd],
                env=env,
            )
            if result.returncode != 0:
                if clear_restart_action is not None:
                    report = report_clear_event or (lambda payload: None)
                    report(_clear_event_payload(
                        "failed",
                        reason="restart_failed",
                        action=clear_restart_action,
                        generation=clear_restart_generation,
                        session_ref=clear_restart_session_id,
                    ))
                print(f"  Error: failed to create tmux session (exit {result.returncode})")
                break

            if clear_restart_action is not None and clear_restart_session_id is not None:
                _finalize_claude_clear_restart(
                    clear_restart_action,
                    new_session_id=clear_restart_session_id,
                    generation=clear_restart_generation,
                    verify_session_fn=verify_claude_session_fn,
                    report_clear_event=report_clear_event,
                    report_usage_event=report_usage_event,
                )

            # Attach — blocks until agent exits or user detaches (Ctrl+B, D)
            subprocess.run(["tmux", "attach-session", "-t", session_name])

            # Check: did the agent exit, or did the user just detach?
            if _session_exists(session_name):
                # Session still alive — user detached, agent running in background.
                # Keep the wrapper alive so the local proxy and heartbeats survive.
                print(f"\n  Detached. {agent.capitalize()} still running in tmux.")
                print(f"  Reattach: tmux attach -t {session_name}")
                while _session_exists(session_name):
                    time.sleep(1)
                if agent == "claude" and _has_restart_request():
                    print("\n  Claude recovery requested while detached; restarting in 3s...")
                    time.sleep(3)
                    continue
                break

            # Session gone — agent exited
            if agent == "claude" and _has_restart_request():
                print(f"\n  {agent.capitalize()} restart requested.")
                print("  Restarting in 3s... (Ctrl+C to quit)")
                time.sleep(3)
                continue

            if no_restart:
                break

            print(f"\n  {agent.capitalize()} exited.")
            print(f"  Restarting in 3s... (Ctrl+C to quit)")
            time.sleep(3)
        except KeyboardInterrupt:
            # Kill the tmux session on Ctrl+C
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True,
            )
            break
