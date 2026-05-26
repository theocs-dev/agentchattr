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
                    "profiles": {
                        "xhigh": {
                            "label": "Opus 4.7 XHigh",
                            "model": "opus",
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
        self.assertNotIn("budget 1 000 000", listing)


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


class FrontendMentionCandidateTests(unittest.TestCase):
    def test_job_autocomplete_passes_job_channel_to_candidate_builder(self):
        chat_js = (ROOT / "static/chat.js").read_text(encoding="utf-8")
        jobs_js = (ROOT / "static/jobs.js").read_text(encoding="utf-8")

        self.assertIn("function getMentionCandidates(channel = activeChannel)", chat_js)
        self.assertIn("const effectiveChannel = String(channel || activeChannel || 'general')", chat_js)
        self.assertIn("window.getMentionCandidates(job?.channel || window.activeChannel)", jobs_js)


if __name__ == "__main__":
    unittest.main()
