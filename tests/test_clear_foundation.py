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
import wrapper_api  # noqa: E402
from agents import AgentTrigger  # noqa: E402
from registry import RuntimeRegistry  # noqa: E402


class DummyRequest:
    def __init__(self, body=None, headers=None):
        self._body = body or {}
        self.headers = headers or {}

    async def json(self):
        return dict(self._body)


class DummyRouter:
    def is_paused(self, channel):
        return False


def response_json(response):
    return json.loads(response.body.decode("utf-8"))


class ClearFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_registry = app.registry
        self._old_agents = app.agents
        self._old_router = app.router
        self._old_event_loop = app._event_loop
        self._old_usage_snapshots = dict(app.usage_snapshots)
        app._event_loop = None
        app.router = DummyRouter()
        app.usage_snapshots.clear()

    def tearDown(self):
        app.registry = self._old_registry
        app.agents = self._old_agents
        app.router = self._old_router
        app._event_loop = self._old_event_loop
        app.usage_snapshots.clear()
        app.usage_snapshots.update(self._old_usage_snapshots)
        with mcp_bridge._presence_lock:
            mcp_bridge._presence.pop("claude", None)
            mcp_bridge._activity.pop("claude", None)
            mcp_bridge._activity_ts.pop("claude", None)

    def _registry(self):
        reg = RuntimeRegistry(data_dir=self.tmp.name)
        reg.seed({
            "claude": {"label": "Claude", "color": "#da7756"},
            "codex": {"label": "Codex", "color": "#4ade80"},
            "qwen": {"label": "Qwen", "color": "#b891ff"},
        })
        app.registry = reg
        return reg

    def test_register_defaults_are_safe_without_capabilities(self):
        reg = self._registry()

        registered = reg.register("claude")

        self.assertEqual(registered["transport"], "unknown")
        self.assertFalse(registered["terminal_injectable"])
        self.assertFalse(registered["clear_supported"])
        self.assertEqual(registered["clear_strategy"], "unsupported")
        self.assertEqual(registered["clear_confirmation"], "none")
        self.assertEqual(registered["clear_state"]["state"], "not_supported")

    def test_register_sanitizes_unknown_or_spoofed_capabilities(self):
        self._registry()

        response = asyncio.run(app.register_agent(DummyRequest({
            "base": "claude",
            "transport": "ssh",
            "terminal_injectable": "yes",
            "clear_supported": True,
            "clear_strategy": "magic",
            "clear_confirmation": "trust_me",
            "clear_state": {"state": "confirmed", "reason": "spoofed"},
        })))
        payload = response_json(response)

        self.assertEqual(payload["transport"], "unknown")
        self.assertFalse(payload["terminal_injectable"])
        self.assertFalse(payload["clear_supported"])
        self.assertEqual(payload["clear_strategy"], "unsupported")
        self.assertEqual(payload["clear_confirmation"], "none")
        self.assertEqual(payload["clear_state"]["state"], "not_supported")

    def test_cli_wrapper_declares_injectable_but_clear_unsupported(self):
        import urllib.request

        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"name":"claude","token":"tok"}'

        def fake_urlopen(request, timeout=0):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        original_urlopen = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            wrapper_module._register_instance(8123, "claude")
        finally:
            urllib.request.urlopen = original_urlopen

        self.assertEqual(captured["payload"]["transport"], "windows_console" if sys.platform == "win32" else "tmux")
        self.assertTrue(captured["payload"]["terminal_injectable"])
        self.assertFalse(captured["payload"]["clear_supported"])
        self.assertEqual(captured["payload"]["clear_strategy"], "unsupported")
        self.assertEqual(captured["payload"]["clear_confirmation"], "none")

    def test_api_wrapper_declares_clear_not_applicable_explicitly(self):
        captured = {}

        def fake_register(server_port, agent, label, **kwargs):
            captured.update(kwargs)
            return {"name": agent, "token": "tok"}

        wrapper_api._register_api_instance(fake_register, 8123, "qwen", None)

        self.assertEqual(captured["transport"], "api")
        self.assertFalse(captured["terminal_injectable"])
        self.assertFalse(captured["clear_supported"])
        self.assertEqual(captured["clear_strategy"], "not_applicable")
        self.assertEqual(captured["clear_confirmation"], "none")
        self.assertEqual(captured["clear_reason"], "stateless_chat_replay")

    def test_status_exposes_presence_and_clear_capability_separately(self):
        reg = self._registry()
        reg.register(
            "claude",
            transport="tmux",
            terminal_injectable=True,
            clear_supported=False,
            clear_strategy="unsupported",
        )
        app.agents = AgentTrigger(reg, data_dir=self.tmp.name)
        with mcp_bridge._presence_lock:
            mcp_bridge._presence["claude"] = time.time()

        status = app._status_payload()

        self.assertTrue(status["claude"]["registered"])
        self.assertTrue(status["claude"]["available"])
        self.assertTrue(status["claude"]["terminal_injectable"])
        self.assertFalse(status["claude"]["clear_supported"])
        self.assertEqual(status["claude"]["clear_state"]["state"], "not_supported")

    def test_clear_event_requires_auth_and_mutates_only_authenticated_instance(self):
        reg = self._registry()
        claude = reg.register(
            "claude",
            transport="tmux",
            terminal_injectable=True,
            clear_supported=True,
            clear_strategy="session_restart",
            clear_confirmation="wrapper_machine",
        )
        codex = reg.register("codex")

        unauth = asyncio.run(app.clear_event(DummyRequest({"state": "confirmed"})))
        self.assertEqual(unauth.status_code, 403)

        authed = asyncio.run(app.clear_event(DummyRequest(
            {
                "name": codex["name"],
                "state": "confirmed",
                "strategy": "session_restart",
                "confirmation": "wrapper_machine",
                "reason": "new session file observed",
                "generation": "g2",
            },
            headers={"authorization": f"Bearer {claude['token']}"},
        )))
        payload = response_json(authed)

        self.assertEqual(authed.status_code, 200)
        self.assertEqual(payload["clear_state"]["state"], "confirmed")
        self.assertEqual(reg.get_instance("claude")["clear_state"]["state"], "confirmed")
        self.assertEqual(reg.get_instance("codex")["clear_state"]["state"], "not_supported")

    def test_clear_event_rejects_invalid_enums(self):
        reg = self._registry()
        registered = reg.register("claude")
        headers = {"authorization": f"Bearer {registered['token']}"}

        invalid_payloads = [
            {
                "state": "banana",
                "strategy": "session_restart",
                "confirmation": "wrapper_machine",
            },
            {
                "state": "pending",
                "strategy": "magic",
                "confirmation": "wrapper_machine",
            },
            {
                "state": "pending",
                "strategy": "session_restart",
                "confirmation": "trust_me",
            },
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = asyncio.run(app.clear_event(DummyRequest(payload, headers=headers)))
                self.assertEqual(response.status_code, 400)

        self.assertEqual(reg.get_instance("claude")["clear_state"]["state"], "not_supported")

    def test_registry_update_clear_state_sanitizes_direct_calls(self):
        reg = self._registry()
        reg.register("claude")

        updated = reg.update_clear_state(
            "claude",
            state="banana",
            strategy="magic",
            confirmation="trust_me",
            reason="bad internal caller",
        )

        self.assertEqual(updated["clear_strategy"], "unsupported")
        self.assertEqual(updated["clear_confirmation"], "none")
        self.assertEqual(updated["clear_state"]["state"], "not_supported")

    def test_registry_update_clear_state_preserves_optional_strategy_and_confirmation(self):
        reg = self._registry()
        reg.register(
            "claude",
            clear_supported=True,
            clear_strategy="session_restart",
            clear_confirmation="wrapper_machine",
        )

        updated = reg.update_clear_state(
            "claude",
            state="pending",
            reason="action requested",
        )

        self.assertEqual(updated["clear_strategy"], "session_restart")
        self.assertEqual(updated["clear_confirmation"], "wrapper_machine")
        self.assertEqual(updated["clear_state"]["state"], "pending")

    def test_usage_event_keeps_cleared_generation_from_old_usage(self):
        reg = self._registry()
        registered = reg.register("claude")
        headers = {"authorization": f"Bearer {registered['token']}"}

        cleared = asyncio.run(app.usage_event(DummyRequest(
            {
                "status": "unavailable",
                "reason": "cleared; awaiting fresh telemetry",
                "updated_at": 10,
                "session_ref": "new",
            },
            headers=headers,
        )))
        self.assertEqual(cleared.status_code, 200)

        stale = asyncio.run(app.usage_event(DummyRequest(
            {
                "status": "ok",
                "used_tokens": 999,
                "updated_at": 11,
                "session_ref": "old",
            },
            headers=headers,
        )))
        self.assertEqual(response_json(stale)["ignored"], True)
        self.assertEqual(app.usage_snapshots["claude"]["status"], "unavailable")
        self.assertEqual(app.usage_snapshots["claude"]["session_ref"], "new")

        fresh = asyncio.run(app.usage_event(DummyRequest(
            {
                "status": "ok",
                "used_tokens": 7,
                "updated_at": 12,
                "session_ref": "new",
            },
            headers=headers,
        )))
        self.assertEqual(fresh.status_code, 200)
        self.assertEqual(app.usage_snapshots["claude"]["status"], "ok")
        self.assertEqual(app.usage_snapshots["claude"]["used_tokens"], 7)


if __name__ == "__main__":
    unittest.main()
