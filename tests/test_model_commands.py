import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from app import _leading_mentions_and_command_text  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
