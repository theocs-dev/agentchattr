import asyncio
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from app import _leading_mentions_and_command_text  # noqa: E402
from registry import RuntimeRegistry  # noqa: E402
from router import Router  # noqa: E402


class ModelCommandParsingTests(unittest.TestCase):
    def setUp(self):
        self._config = deepcopy(app.config)
        self._room_settings = deepcopy(app.room_settings)
        self._save_settings = app._save_settings
        app._save_settings = lambda: None

    def tearDown(self):
        app._save_settings = self._save_settings
        app.config.clear()
        app.config.update(self._config)
        app.room_settings.clear()
        app.room_settings.update(self._room_settings)

    def test_strips_leading_mentions_before_model_command(self):
        mentions, command = _leading_mentions_and_command_text("@claude /model")

        self.assertEqual(mentions, ["claude"])
        self.assertEqual(command, "/model")

    def test_strips_multiple_mentions_before_model_command(self):
        mentions, command = _leading_mentions_and_command_text("@claude, @codex /model fast")

        self.assertEqual(mentions, ["claude", "codex"])
        self.assertEqual(command, "/model fast")

    def test_leaves_non_leading_mentions_in_command_text(self):
        mentions, command = _leading_mentions_and_command_text("/model @codex")

        self.assertEqual(mentions, [])
        self.assertEqual(command, "/model @codex")

    def test_normalizes_default_mention_handles(self):
        self.assertEqual(app._normalize_default_mention("@Claude"), "claude")
        self.assertEqual(app._normalize_default_mention("both"), "all")
        self.assertEqual(app._normalize_default_mention("../claude", fallback="all"), "all")

    def test_profile_can_be_selected_by_reasoning_alias(self):
        app.config.clear()
        app.config.update({
            "agents": {
                "codex": {
                    "profiles": {
                        "balanced": {"reasoning": "high"},
                        "deep": {"reasoning": "xhigh"},
                    }
                }
            }
        })
        app.room_settings["agent_profiles"] = {}

        self.assertIsNone(app._set_agent_profile("codex", "xhigh"))
        self.assertEqual(app.room_settings["agent_profiles"]["codex"], "deep")

    def test_profile_list_names_claude_limit_as_window(self):
        app.config.clear()
        app.config.update({
            "agents": {
                "claude": {
                    "fast_mode_default": True,
                    "profiles": {
                        "xhigh": {
                            "label": "Opus 4.8 XHigh",
                            "model": "claude-opus-4-8",
                            "reasoning": "xhigh",
                            "context_limit": 1000000,
                            "default": True,
                        }
                    }
                }
            }
        })
        app.room_settings["agent_profiles"] = {}

        listing = app._format_profile_list("claude")

        self.assertIn("window 1 000 000", listing)
        self.assertIn("claude fast mode: ON", listing)
        self.assertNotIn("budget 1 000 000", listing)

    def test_profile_list_names_claude_fast_mode_off(self):
        app.config.clear()
        app.config.update({
            "agents": {
                "claude": {
                    "fast_mode_default": True,
                    "profiles": {
                        "max": {"label": "Opus Max", "reasoning": "max", "default": True},
                    },
                },
            }
        })
        app.room_settings["agent_profiles"] = {}
        app.room_settings["agent_fast_modes"] = {"claude": False}

        listing = app._format_profile_list("claude")

        self.assertIn("claude fast mode: OFF", listing)


class DummyStore:
    def __init__(self):
        self.messages = []

    def add(self, sender, text, msg_type="chat", channel="general", **kwargs):
        msg = {
            "sender": sender,
            "text": text,
            "type": msg_type,
            "channel": channel,
        }
        self.messages.append(msg)
        return msg


class DummyAgents:
    def __init__(self):
        self.triggered = []

    def is_available(self, name):
        return True

    async def trigger(self, agent_name, message="", channel="general", **kwargs):
        self.triggered.append({
            "agent": agent_name,
            "message": message,
            "channel": channel,
            **kwargs,
        })


class DummyRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return dict(self._body)


class HangingClient:
    async def send_text(self, data):
        await asyncio.Event().wait()


class RecordingClient:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(data)


class ModelCommandHandlingTests(unittest.TestCase):
    def setUp(self):
        self._config = deepcopy(app.config)
        self._room_settings = deepcopy(app.room_settings)
        self._store = app.store
        self._save_settings = app._save_settings
        self._broadcast_settings = app.broadcast_settings
        self._broadcast_agents = app.broadcast_agents
        self._broadcast_status = app.broadcast_status
        app.store = DummyStore()
        app._save_settings = lambda: None

        async def noop():
            return None

        app.broadcast_settings = noop
        app.broadcast_agents = noop
        app.broadcast_status = noop
        app.config.clear()
        app.config.update({
            "agents": {
                "claude": {
                    "fast_mode_default": True,
                    "profiles": {
                        "max": {"label": "Opus Max", "reasoning": "max", "default": True},
                        "xhigh": {"label": "Opus XHigh", "reasoning": "xhigh"},
                    },
                },
                "codex": {
                    "profiles": {
                        "balanced": {"label": "Balanced", "reasoning": "high"},
                        "deep": {"label": "Extra High", "reasoning": "xhigh", "default": True},
                    },
                },
            },
        })
        app.room_settings["agent_profiles"] = {}
        app.room_settings["agent_fast_modes"] = {}

    def tearDown(self):
        app.config.clear()
        app.config.update(self._config)
        app.room_settings.clear()
        app.room_settings.update(self._room_settings)
        app.store = self._store
        app._save_settings = self._save_settings
        app.broadcast_settings = self._broadcast_settings
        app.broadcast_agents = self._broadcast_agents
        app.broadcast_status = self._broadcast_status

    def test_direct_model_command_selects_claude_max(self):
        asyncio.run(app._handle_model_command(["/model", "claude", "max"], "general"))

        self.assertEqual(app.room_settings["agent_profiles"]["claude"], "max")
        self.assertIn("claude profile set to Opus Max", app.store.messages[-1]["text"])

    def test_addressed_model_command_selects_codex_by_reasoning_alias(self):
        asyncio.run(app._handle_model_command(["/model", "xhigh"], "general", ["codex"]))

        self.assertEqual(app.room_settings["agent_profiles"]["codex"], "deep")
        self.assertIn("codex profile set to Extra High", app.store.messages[-1]["text"])

    def test_fast_command_toggles_claude_fast_mode(self):
        app.room_settings["agent_fast_modes"] = {"claude": True}

        asyncio.run(app._handle_fast_command(["/fast"], "general"))

        self.assertFalse(app.room_settings["agent_fast_modes"]["claude"])
        self.assertIn("claude fast mode set to OFF", app.store.messages[-1]["text"])

    def test_fast_command_accepts_explicit_on_off(self):
        app.room_settings["agent_fast_modes"] = {"claude": False}

        asyncio.run(app._handle_fast_command(["/fast", "on"], "general"))

        self.assertTrue(app.room_settings["agent_fast_modes"]["claude"])
        self.assertIn("claude fast mode set to ON", app.store.messages[-1]["text"])

    def test_addressed_fast_command_rejects_codex(self):
        asyncio.run(app._handle_fast_command(["/fast"], "general", ["codex"]))

        self.assertIn("only configured for Claude", app.store.messages[-1]["text"])


class TargetResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_registry = app.registry
        self._old_store = app.store
        self._old_agents = app.agents
        app.store = DummyStore()
        app.agents = DummyAgents()
        app._target_resolution_warning_times.clear()

    def tearDown(self):
        app.registry = self._old_registry
        app.store = self._old_store
        app.agents = self._old_agents
        app._target_resolution_warning_times.clear()

    def _make_registry(self):
        reg = RuntimeRegistry(data_dir=self.tmp.name)
        reg.seed({
            "claude": {"label": "Claude", "color": "#da7756"},
            "codex": {"label": "Codex", "color": "#4ade80"},
        })
        app.registry = reg
        return reg

    def _register_pair(self, base: str, first_name: str, second_name: str):
        reg = app.registry
        reg.register(base)
        reg.register(base)
        first = reg.rename(f"{base}-1", first_name, first_name)
        second = reg.rename(f"{base}-2", second_name, second_name)
        self.assertIsInstance(first, dict)
        self.assertIsInstance(second, dict)

    def test_family_mention_resolves_to_channel_alias_when_active(self):
        self._make_registry()
        self._register_pair("claude", "claude-test", "claude-test2")
        self._register_pair("codex", "codex-test", "codex-test2")

        self.assertEqual(
            app.resolve_targets_for_channel(["claude"], "test2", "user"),
            ["claude-test2"],
        )
        self.assertEqual(
            app.resolve_targets_for_channel(["codex"], "test2", "user"),
            ["codex-test2"],
        )
        self.assertEqual(app.store.messages, [])

    def test_ambiguous_family_mention_triggers_nobody_and_warns_once(self):
        self._make_registry()
        self._register_pair("claude", "claude-test", "claude-test2")

        self.assertEqual(app.resolve_targets_for_channel(["claude"], "planning", "user"), [])
        self.assertEqual(app.resolve_targets_for_channel(["claude"], "planning", "user"), [])

        self.assertEqual(len(app.store.messages), 1)
        warning = app.store.messages[0]
        self.assertEqual(warning["sender"], "system")
        self.assertEqual(warning["channel"], "planning")
        self.assertIn("@claude is ambiguous", warning["text"])
        self.assertIn("@claude-planning", warning["text"])
        self.assertIn("@all", warning["text"])

    def test_numeric_channel_does_not_create_local_alias(self):
        reg = self._make_registry()
        reg.register("claude")
        reg.register("claude")

        self.assertEqual(app.resolve_targets_for_channel(["claude"], "2", "user"), [])

        self.assertEqual(len(app.store.messages), 1)
        warning = app.store.messages[0]["text"]
        self.assertIn("Numeric-only channels", warning)
        self.assertNotIn("@claude-2 or", warning)

    def test_zero_active_family_preserves_offline_noop_target(self):
        self._make_registry()

        self.assertEqual(
            app.resolve_targets_for_channel(["claude"], "test2", "user"),
            ["claude"],
        )
        self.assertEqual(app.store.messages, [])

    def test_single_active_family_resolves_to_that_instance(self):
        reg = self._make_registry()
        reg.register("claude")
        result = reg.rename("claude", "claude-test", "claude-test")
        self.assertIsInstance(result, dict)

        self.assertEqual(
            app.resolve_targets_for_channel(["claude"], "2", "user"),
            ["claude-test"],
        )

    def test_explicit_canonical_handle_is_preserved_when_family_is_ambiguous(self):
        self._make_registry()
        self._register_pair("claude", "claude-test", "claude-test2")

        self.assertEqual(
            app.resolve_targets_for_channel(["claude-test2"], "planning", "user"),
            ["claude-test2"],
        )
        self.assertEqual(app.store.messages, [])

    def test_all_fanout_targets_pass_through(self):
        self._make_registry()
        self._register_pair("claude", "claude-test", "claude-test2")

        self.assertEqual(
            app.resolve_targets_for_channel(["claude-test", "claude-test2"], "planning", "user"),
            ["claude-test", "claude-test2"],
        )
        self.assertEqual(app.store.messages, [])

    def test_silent_trigger_uses_channel_alias_resolution(self):
        self._make_registry()
        self._register_pair("claude", "claude-test", "claude-test2")

        response = asyncio.run(app.trigger_agent_silent(DummyRequest({
            "agent": "claude",
            "message": "convert this",
            "channel": "test2",
        })))

        self.assertEqual(response["triggered"], ["claude-test2"])
        self.assertEqual([t["agent"] for t in app.agents.triggered], ["claude-test2"])
        self.assertEqual(app.agents.triggered[0]["channel"], "test2")
        self.assertEqual(app.store.messages, [])

    def test_silent_trigger_ambiguous_family_triggers_nobody(self):
        self._make_registry()
        self._register_pair("claude", "claude-test", "claude-test2")

        response = asyncio.run(app.trigger_agent_silent(DummyRequest({
            "agent": "claude",
            "message": "convert this",
            "channel": "planning",
        })))

        self.assertEqual(response["triggered"], [])
        self.assertEqual(app.agents.triggered, [])
        self.assertEqual(len(app.store.messages), 1)
        self.assertIn("@claude is ambiguous", app.store.messages[0]["text"])


class MessageDefaultRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_config = deepcopy(app.config)
        self._old_registry = app.registry
        self._old_router = app.router
        self._old_store = app.store
        self._old_agents = app.agents
        self._old_session_engine = app.session_engine
        self._old_ws_clients = set(app.ws_clients)
        self._old_ws_send_timeout = app._WS_SEND_TIMEOUT

        app.config.clear()
        app.config.update({
            "agents": {
                "claude": {"label": "Claude", "color": "#da7756"},
                "codex": {"label": "Codex", "color": "#4ade80"},
            },
        })
        reg = RuntimeRegistry(data_dir=self.tmp.name)
        reg.seed(app.config["agents"])
        reg.register("claude")
        reg.register("codex")
        app.registry = reg
        app.router = Router(
            ["claude", "codex"],
            default_mention="all",
            online_checker=lambda: set(reg.get_active_names()),
        )
        app.store = DummyStore()
        app.agents = DummyAgents()
        app.session_engine = None
        app.ws_clients.clear()
        app._WS_SEND_TIMEOUT = 0.01

    def tearDown(self):
        app.config.clear()
        app.config.update(self._old_config)
        app.registry = self._old_registry
        app.router = self._old_router
        app.store = self._old_store
        app.agents = self._old_agents
        app.session_engine = self._old_session_engine
        app.ws_clients.clear()
        app.ws_clients.update(self._old_ws_clients)
        app._WS_SEND_TIMEOUT = self._old_ws_send_timeout

    def test_human_message_without_mention_routes_to_all_by_default(self):
        asyncio.run(app._handle_new_message({
            "sender": "Theo",
            "text": "please check this",
            "channel": "general",
        }))

        self.assertEqual(
            [trigger["agent"] for trigger in app.agents.triggered],
            ["claude", "codex"],
        )

    def test_leave_from_unknown_sender_does_not_route_to_default_targets(self):
        asyncio.run(app._handle_new_message({
            "sender": "codex-2",
            "text": "codex-2 disconnected (timeout)",
            "type": "leave",
            "channel": "general",
        }))

        self.assertEqual(app.agents.triggered, [])

    def test_non_agent_join_does_not_route_to_default_targets(self):
        asyncio.run(app._handle_new_message({
            "sender": "observer",
            "text": "observer is online",
            "type": "join",
            "channel": "general",
        }))

        self.assertEqual(app.agents.triggered, [])

    def test_session_request_still_routes_to_target_agent(self):
        asyncio.run(app._handle_new_message({
            "sender": "Theo",
            "text": "@codex Design a session workflow for: **triage failures**",
            "type": "session_request",
            "channel": "general",
        }))

        self.assertEqual(
            [trigger["agent"] for trigger in app.agents.triggered],
            ["codex"],
        )

    def test_stale_websocket_does_not_block_default_routing(self):
        app.ws_clients.add(HangingClient())

        asyncio.run(asyncio.wait_for(app._handle_new_message({
            "sender": "Theo",
            "text": "please check this",
            "channel": "test2",
        }), timeout=0.5))

        self.assertEqual(
            [trigger["agent"] for trigger in app.agents.triggered],
            ["claude", "codex"],
        )
        self.assertEqual(app.ws_clients, set())

    def test_stale_websocket_does_not_drop_healthy_clients(self):
        healthy = RecordingClient()
        app.ws_clients.add(healthy)
        app.ws_clients.add(HangingClient())

        asyncio.run(asyncio.wait_for(app._handle_new_message({
            "sender": "Theo",
            "text": "please check this",
            "channel": "test2",
        }), timeout=0.5))

        self.assertEqual(
            [trigger["agent"] for trigger in app.agents.triggered],
            ["claude", "codex"],
        )
        self.assertEqual(app.ws_clients, {healthy})
        self.assertEqual(len(healthy.sent), 1)
        self.assertIn('"type": "message"', healthy.sent[0])

    def test_manual_default_keeps_unmentioned_message_unrouted(self):
        app.router.default_mention = "none"

        asyncio.run(app._handle_new_message({
            "sender": "Theo",
            "text": "please check this",
            "channel": "general",
        }))

        self.assertEqual(app.agents.triggered, [])

    def test_specific_default_routes_to_that_agent_only(self):
        app.router.default_mention = "codex"

        asyncio.run(app._handle_new_message({
            "sender": "Theo",
            "text": "please check this",
            "channel": "general",
        }))

        self.assertEqual(
            [trigger["agent"] for trigger in app.agents.triggered],
            ["codex"],
        )

    def test_settings_default_mention_applies_to_router(self):
        app.room_settings["default_mention"] = "@codex"

        app._apply_default_mention_to_router()

        self.assertEqual(app.router.default_mention, "codex")


class FrontendMentionCandidateTests(unittest.TestCase):
    def test_job_autocomplete_passes_job_channel_to_candidate_builder(self):
        chat_js = (ROOT / "static/chat.js").read_text(encoding="utf-8")
        jobs_js = (ROOT / "static/jobs.js").read_text(encoding="utf-8")

        self.assertIn("function getMentionCandidates(channel = activeChannel)", chat_js)
        self.assertIn("const effectiveChannel = String(channel || activeChannel || 'general')", chat_js)
        self.assertIn("window.getMentionCandidates(job?.channel || window.activeChannel)", jobs_js)


if __name__ == "__main__":
    unittest.main()
