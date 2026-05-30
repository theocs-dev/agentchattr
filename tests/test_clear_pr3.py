import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import mcp_bridge  # noqa: E402
import wrapper as wrapper_module  # noqa: E402
import wrapper_unix  # noqa: E402
from agents import AgentTrigger  # noqa: E402
from registry import RuntimeRegistry  # noqa: E402
from wrapper import ClaudeSessionState  # noqa: E402


class DummyRequest:
    def __init__(self, body=None):
        self._body = body if body is not None else {}

    async def json(self):
        return dict(self._body)


class ClearPr3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self._old_registry = app.registry
        self._old_agents = app.agents
        self._old_room_settings = dict(app.room_settings)
        self._old_last_channel = app._last_active_channel

    def tearDown(self):
        app.registry = self._old_registry
        app.agents = self._old_agents
        app.room_settings.clear()
        app.room_settings.update(self._old_room_settings)
        app._last_active_channel = self._old_last_channel
        with mcp_bridge._presence_lock:
            for name in ("claude", "codex"):
                mcp_bridge._presence.pop(name, None)
                mcp_bridge._activity.pop(name, None)
                mcp_bridge._activity_ts.pop(name, None)

    def _registry(self):
        reg = RuntimeRegistry(data_dir=str(self.data_dir))
        reg.seed({
            "claude": {"label": "Claude", "color": "#da7756"},
            "codex": {"label": "Codex", "color": "#4ade80"},
        })
        app.registry = reg
        app.agents = AgentTrigger(reg, data_dir=str(self.data_dir))
        return reg

    def _register_clearable_claude(self):
        reg = self._registry()
        registered = reg.register(
            "claude",
            transport="tmux",
            terminal_injectable=True,
            clear_supported=True,
            clear_strategy="session_restart",
            clear_confirmation="wrapper_machine",
        )
        with mcp_bridge._presence_lock:
            mcp_bridge._presence["claude"] = time.time()
            mcp_bridge._activity["claude"] = False
        return registered

    def test_server_queues_clear_action_without_prompt_queue(self):
        self._register_clearable_claude()

        response = asyncio.run(app.request_clear_context(
            "claude",
            DummyRequest({"channel": "clear-feature", "requested_by": "Theo"}),
        ))
        payload = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["action"]["type"], "clear_context")
        self.assertEqual(payload["action"]["strategy"], "session_restart")

        action_path = self.data_dir / "claude_actions.jsonl"
        prompt_path = self.data_dir / "claude_queue.jsonl"
        action = json.loads(action_path.read_text("utf-8").strip())
        self.assertEqual(action["type"], "clear_context")
        self.assertEqual(action["strategy"], "session_restart")
        self.assertEqual(action["channel"], "clear-feature")
        self.assertEqual(action["requested_by"], "Theo")
        self.assertTrue(action["confirmed_by_user"])
        self.assertFalse(prompt_path.exists())

    def test_server_refuses_busy_clear_request(self):
        self._register_clearable_claude()
        mcp_bridge.set_active("claude", True)

        response = asyncio.run(app.request_clear_context("claude", DummyRequest()))

        self.assertEqual(response.status_code, 409)
        self.assertFalse((self.data_dir / "claude_actions.jsonl").exists())

    def test_server_refuses_non_machine_confirmation(self):
        reg = self._registry()
        reg.register(
            "claude",
            transport="tmux",
            terminal_injectable=True,
            clear_supported=True,
            clear_strategy="session_restart",
            clear_confirmation="heuristic_only",
        )
        with mcp_bridge._presence_lock:
            mcp_bridge._presence["claude"] = time.time()

        response = asyncio.run(app.request_clear_context("claude", DummyRequest()))

        self.assertEqual(response.status_code, 409)
        self.assertFalse((self.data_dir / "claude_actions.jsonl").exists())

    def test_server_refuses_non_claude_adapter(self):
        reg = self._registry()
        reg.register(
            "codex",
            transport="tmux",
            terminal_injectable=True,
            clear_supported=True,
            clear_strategy="session_restart",
            clear_confirmation="wrapper_machine",
        )
        with mcp_bridge._presence_lock:
            mcp_bridge._presence["codex"] = time.time()

        response = asyncio.run(app.request_clear_context("codex", DummyRequest()))

        self.assertEqual(response.status_code, 409)
        self.assertFalse((self.data_dir / "codex_actions.jsonl").exists())

    def test_action_queue_parser_accepts_only_clear_context_records(self):
        actions = wrapper_module._parse_action_lines([
            "",
            "not-json",
            json.dumps({"type": "mention", "prompt": "must not inject"}),
            json.dumps({"type": "clear_context", "request_id": "r1"}),
        ])

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["type"], "clear_context")
        self.assertEqual(actions[0]["strategy"], "session_restart")

    def test_action_watcher_step_drains_actions_separately(self):
        action_path = self.data_dir / "claude_actions.jsonl"
        action_path.write_text(json.dumps({"type": "clear_context", "request_id": "r1"}) + "\n", "utf-8")
        handled = []

        count = wrapper_module._action_watcher_step(
            lambda: ("claude", self.data_dir / "claude_queue.jsonl"),
            self.data_dir,
            handled.append,
        )

        self.assertEqual(count, 1)
        self.assertEqual(handled[0]["request_id"], "r1")
        self.assertEqual(action_path.read_text("utf-8"), "")
        self.assertFalse((self.data_dir / "claude_queue.jsonl").exists())

    def test_claude_action_reports_pending_and_requests_restart(self):
        events = []
        restarts = []
        state = ClaudeSessionState("old-session")
        action = {
            "type": "clear_context",
            "strategy": "session_restart",
            "request_id": "r1",
            "requested_at": 123,
            "confirmed_by_user": True,
        }

        handled = wrapper_unix._handle_claude_clear_context_action(
            action,
            agent="claude",
            session_name="agentchattr-claude",
            claude_session_state=state,
            is_busy_fn=lambda: False,
            session_exists_fn=lambda _: True,
            request_restart_fn=lambda request: restarts.append(request) or True,
            report_clear_event=events.append,
        )

        self.assertTrue(handled)
        self.assertEqual(events[0]["state"], "pending")
        self.assertEqual(events[0]["session_ref"], "old-session")
        self.assertEqual(restarts[0]["type"], "clear_context")
        self.assertEqual(restarts[0]["action"]["request_id"], "r1")

    def test_claude_action_refuses_busy_terminal(self):
        events = []
        action = {
            "type": "clear_context",
            "strategy": "session_restart",
            "confirmed_by_user": True,
        }

        wrapper_unix._handle_claude_clear_context_action(
            action,
            agent="claude",
            session_name="agentchattr-claude",
            is_busy_fn=lambda: True,
            session_exists_fn=lambda _: True,
            request_restart_fn=lambda _: self.fail("restart should not be requested"),
            report_clear_event=events.append,
        )

        self.assertEqual(events[0]["state"], "failed")
        self.assertEqual(events[0]["reason"], "adapter_refused_busy")

    def test_claude_recovery_no_restart_signals_without_killing(self):
        events = []
        killed = []
        restart_requests = []
        printed = []

        result = wrapper_unix._request_claude_recovery_restart(
            "thinking-block API error detected",
            session_name="agentchattr-claude",
            no_restart=True,
            set_restart_request_fn=lambda request: restart_requests.append(request) or True,
            kill_session_fn=killed.append,
            report_terminal_event=lambda kind, text: events.append({"kind": kind, "text": text}),
            print_fn=printed.append,
        )

        self.assertFalse(result)
        self.assertEqual(killed, [])
        self.assertEqual(restart_requests, [])
        self.assertEqual(events[0]["kind"], "recovery_blocked")
        self.assertIn("--no-restart prevented automatic restart", events[0]["text"])
        self.assertIn("trigger skipped", events[0]["text"])
        self.assertIn("resend the message", events[0]["text"])
        self.assertIn("trigger skipped", printed[0])

    def test_claude_recovery_without_no_restart_requests_restart_and_kills(self):
        killed = []
        restart_requests = []

        result = wrapper_unix._request_claude_recovery_restart(
            "interrupted prompt detected",
            session_name="agentchattr-claude",
            no_restart=False,
            set_restart_request_fn=lambda request: restart_requests.append(request) or True,
            kill_session_fn=killed.append,
            session_exists_fn=lambda _: True,
            sleep_fn=lambda _: None,
            now_fn=lambda: 0,
            print_fn=lambda _: None,
        )

        self.assertTrue(result)
        self.assertEqual(killed, ["agentchattr-claude"])
        self.assertEqual(restart_requests[0], {"type": "recovery", "reason": "interrupted prompt detected"})

    def test_claude_clear_restart_is_not_no_restart_gated(self):
        events = []
        restarts = []
        action = {
            "type": "clear_context",
            "strategy": "session_restart",
            "confirmed_by_user": True,
        }

        wrapper_unix._handle_claude_clear_context_action(
            action,
            agent="claude",
            session_name="agentchattr-claude",
            is_busy_fn=lambda: False,
            session_exists_fn=lambda _: True,
            request_restart_fn=lambda request: restarts.append(request) or True,
            report_clear_event=events.append,
        )

        self.assertEqual(events[0]["state"], "pending")
        self.assertEqual(restarts[0]["type"], "clear_context")

    def test_clear_restart_confirmation_requires_session_file_proof(self):
        events = []
        usage = []
        action = {"type": "clear_context", "strategy": "session_restart", "request_id": "r1"}

        confirmed = wrapper_unix._finalize_claude_clear_restart(
            action,
            new_session_id="new-session",
            generation=2,
            verify_session_fn=lambda session_id: session_id == "new-session",
            report_clear_event=events.append,
            report_usage_event=usage.append,
            verify_timeout=0,
        )

        self.assertTrue(confirmed)
        self.assertEqual(usage[0]["status"], "unavailable")
        self.assertEqual(usage[0]["reason"], "cleared; awaiting fresh telemetry")
        self.assertEqual(usage[0]["session_ref"], "new-session")
        self.assertEqual(events[-1]["state"], "confirmed")
        self.assertEqual(events[-1]["reason"], "new_session_file_observed")

    def test_clear_restart_stays_pending_without_session_file_proof(self):
        events = []

        confirmed = wrapper_unix._finalize_claude_clear_restart(
            {"type": "clear_context", "strategy": "session_restart"},
            new_session_id="missing-session",
            generation=2,
            verify_session_fn=lambda _: False,
            report_clear_event=events.append,
            verify_timeout=0,
        )

        self.assertFalse(confirmed)
        self.assertEqual(events[-1]["state"], "pending")
        self.assertEqual(events[-1]["reason"], "awaiting_session_file")
        self.assertFalse(any(event["state"] == "failed" for event in events))


if __name__ == "__main__":
    unittest.main()
