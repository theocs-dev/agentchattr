"""Tests for provider usage telemetry and terminal permission matching."""

import json
import os
import sys
import tempfile
import threading
import time
import tomllib
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wrapper as wrapper_module  # noqa: E402
from wrapper import (  # noqa: E402
    ClaudeSessionState,
    _build_profile_args,
    _find_codex_rollout_file,
    _looks_like_permission_prompt,
    _parse_claude_usage_lines,
    _parse_codex_usage_lines,
    _profile_claude_session_id,
    _usage_monitor,
)
from wrapper_unix import (  # noqa: E402
    _build_agent_cmd,
    _looks_like_claude_interrupted_prompt,
    _looks_like_claude_thinking_block_error,
    _refresh_claude_session_id,
)


class ProfileArgsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)

    def test_claude_profile_pins_session_id(self):
        args = _build_profile_args("claude", {
            "profiles": {
                "normal": {
                    "default": True,
                    "model": "sonnet",
                    "reasoning": "medium",
                }
            }
        }, self.data_dir)

        self.assertIn("--model", args)
        self.assertIn("sonnet", args)
        self.assertIn("--effort", args)
        self.assertIn("medium", args)
        session_id = _profile_claude_session_id(args)
        self.assertIsNotNone(session_id)
        uuid.UUID(session_id)

    def test_codex_profile_does_not_claim_session_pinning(self):
        args = _build_profile_args("codex", {
            "profiles": {
                "balanced": {
                    "default": True,
                    "model": "gpt-5.5",
                    "reasoning": "high",
                }
            }
        }, self.data_dir)

        self.assertIn("--model", args)
        self.assertIn("gpt-5.5", args)
        self.assertIn('-c', args)
        self.assertIn('model_reasoning_effort="high"', args)
        self.assertIsNone(_profile_claude_session_id(args))

    def test_default_config_launches_claude_at_max_effort_with_fast_mode(self):
        config = tomllib.loads((ROOT / "config.toml").read_text("utf-8"))

        args = _build_profile_args("claude", config["agents"]["claude"], self.data_dir)

        self.assertIn("--effort", args)
        self.assertIn("max", args)
        self.assertIn("--settings", args)
        self.assertIn('{"fastMode":true}', args)

    def test_settings_can_disable_claude_fast_mode(self):
        config = tomllib.loads((ROOT / "config.toml").read_text("utf-8"))
        (self.data_dir / "settings.json").write_text(
            '{"agent_fast_modes":{"claude":false}}',
            "utf-8",
        )

        args = _build_profile_args("claude", config["agents"]["claude"], self.data_dir)

        self.assertIn("--settings", args)
        self.assertIn('{"fastMode":false}', args)

    def test_default_config_launches_codex_at_extra_high_effort(self):
        config = tomllib.loads((ROOT / "config.toml").read_text("utf-8"))

        args = _build_profile_args("codex", config["agents"]["codex"], self.data_dir)

        self.assertIn('-c', args)
        self.assertIn('model_reasoning_effort="xhigh"', args)


class UsageParserTests(unittest.TestCase):
    def _codex_session_meta_line(self, cwd: Path, timestamp: float, extra: dict | None = None) -> str:
        iso_timestamp = datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
        payload = {
            "cwd": str(cwd),
            "timestamp": iso_timestamp,
        }
        if extra:
            payload.update(extra)
        return json.dumps({
            "type": "session_meta",
            "payload": payload,
        }) + "\n"

    def test_claude_counts_cache_creation_and_cache_read(self):
        lines = [json.dumps({
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 30,
                    "output_tokens": 999,
                },
                "content": [{"type": "text", "text": "must not leak"}],
            },
        })]

        payload = _parse_claude_usage_lines(lines)
        self.assertEqual(payload["provider"], "claude")
        self.assertEqual(payload["used_tokens"], 60)
        self.assertNotIn("content", json.dumps(payload))
        self.assertNotIn("must not leak", json.dumps(payload))

    def test_codex_uses_input_tokens_not_cached_subset(self):
        lines = [json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 40,
                        "output_tokens": 5,
                    }
                },
            },
        })]

        payload = _parse_codex_usage_lines(lines)
        self.assertEqual(payload["provider"], "codex")
        self.assertEqual(payload["used_tokens"], 100)
        self.assertEqual(payload["details"]["cached_input_tokens"], 40)

    def test_codex_correlation_returns_none_when_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "project"
            cwd.mkdir()
            started_at = time.time() - 10
            for idx in range(2):
                path = root / f"rollout-test-{idx}.jsonl"
                path.write_text(self._codex_session_meta_line(cwd, started_at + idx + 1), "utf-8")
                os.utime(path, (time.time(), time.time()))

            self.assertIsNone(_find_codex_rollout_file(cwd, started_at, roots=[root]))

    def test_codex_correlation_returns_exact_recent_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "project"
            cwd.mkdir()
            started_at = time.time()
            path = root / "rollout-test.jsonl"
            path.write_text(self._codex_session_meta_line(cwd, started_at + 1), "utf-8")
            os.utime(path, (time.time(), time.time()))

            self.assertEqual(_find_codex_rollout_file(cwd, started_at, roots=[root]), path)

    def test_codex_correlation_ignores_subagent_rollouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "project"
            cwd.mkdir()
            started_at = time.time()
            user_path = root / "rollout-user.jsonl"
            subagent_path = root / "rollout-subagent.jsonl"
            user_path.write_text(
                self._codex_session_meta_line(cwd, started_at + 1, {"thread_source": "user", "source": "cli"}),
                "utf-8",
            )
            subagent_path.write_text(
                self._codex_session_meta_line(
                    cwd,
                    started_at + 2,
                    {"thread_source": "subagent", "source": {"subagent": {"other": "guardian"}}},
                ),
                "utf-8",
            )
            os.utime(user_path, (time.time(), time.time()))
            os.utime(subagent_path, (time.time(), time.time()))

            self.assertEqual(_find_codex_rollout_file(cwd, started_at, roots=[root]), user_path)

    def test_codex_correlation_ignores_stale_session_with_fresh_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "project"
            cwd.mkdir()
            started_at = time.time()
            path = root / "rollout-old.jsonl"
            path.write_text(self._codex_session_meta_line(cwd, started_at - 120), "utf-8")
            os.utime(path, (time.time(), time.time()))

            self.assertIsNone(_find_codex_rollout_file(cwd, started_at, roots=[root]))


class PermissionPromptTests(unittest.TestCase):
    def test_detects_explicit_interactive_prompts(self):
        positives = [
            "Do you want to allow this command?",
            "Approve?",
            "Approve command?",
            "Allow?",
            "Allow command?",
            "Allow this command to run?",
            "Continue [y/n]",
            "Continue (yes/no)",
            "\x1b[31mDo you want to proceed?\x1b[0m",
        ]
        for text in positives:
            with self.subTest(text=text):
                self.assertTrue(_looks_like_permission_prompt(text))

    def test_ignores_permission_words_in_prose(self):
        negatives = [
            "This permission model should allow future work.",
            "I approve of the direction but cannot know the result.",
            "There is nothing to approve right now.",
            "No permission prompt was shown.",
            "We can allow this in docs later.",
            "Can we allow this in docs later?",
        ]
        for text in negatives:
            with self.subTest(text=text):
                self.assertFalse(_looks_like_permission_prompt(text))


class ClaudeTerminalRecoveryTests(unittest.TestCase):
    def _claude_usage_line(self, tokens: int) -> str:
        return json.dumps({
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": tokens,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 1,
                },
            },
        }) + "\n"

    def _wait_for(self, predicate, timeout: float = 2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("timed out waiting for usage monitor event")

    def test_detects_claude_thinking_block_api_error(self):
        text = (
            "API Error: 400 messages.1.content.9: `thinking` or "
            "`redacted_thinking` blocks in the latest assistant message cannot be modified."
        )

        self.assertTrue(_looks_like_claude_thinking_block_error(text))

    def test_detects_claude_interrupted_prompt(self):
        text = "Interrupted · What should Claude do instead?"

        self.assertTrue(_looks_like_claude_interrupted_prompt(text))

    def test_ignores_normal_claude_output_for_recovery(self):
        text = "Allowed by auto mode classifier\nBrewed for 3s\n>"

        self.assertFalse(_looks_like_claude_thinking_block_error(text))
        self.assertFalse(_looks_like_claude_interrupted_prompt(text))

    def test_ignores_stale_interrupted_prompt_outside_recent_tail(self):
        text = "Interrupted · What should Claude do instead?\n" + "\n".join(
            f"normal line {idx}" for idx in range(12)
        )

        self.assertFalse(_looks_like_claude_interrupted_prompt(text))

    def test_refresh_claude_session_id_replaces_existing_id(self):
        old_id = str(uuid.uuid4())
        args = ["--model", "opus", "--session-id", old_id]

        new_id = _refresh_claude_session_id(args)

        self.assertNotEqual(new_id, old_id)
        self.assertEqual(args[-1], new_id)
        uuid.UUID(new_id)

    def test_refresh_claude_session_id_appends_when_absent(self):
        args = ["--model", "opus"]

        new_id = _refresh_claude_session_id(args)

        self.assertEqual(args[-2:], ["--session-id", new_id])
        uuid.UUID(new_id)

    def test_rebuilt_tmux_command_uses_refreshed_session_id(self):
        old_id = str(uuid.uuid4())
        args = ["--model", "opus", "--session-id", old_id]

        new_id = _refresh_claude_session_id(args)
        command = _build_agent_cmd("claude", args)

        self.assertIn(new_id, command)
        self.assertNotIn(old_id, command)

    def test_usage_monitor_repoints_after_claude_session_revision_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_id = str(uuid.uuid4())
            new_id = str(uuid.uuid4())
            (root / f"{old_id}.jsonl").write_text(self._claude_usage_line(100), "utf-8")

            events: list[dict] = []
            stop_event = threading.Event()
            state = ClaudeSessionState(old_id)
            original_report = wrapper_module._report_usage_event
            wrapper_module._report_usage_event = (
                lambda _port, _token, payload: events.append(dict(payload))
            )
            thread = threading.Thread(
                target=_usage_monitor,
                args=("claude", root, 12345, lambda: "token"),
                kwargs={
                    "claude_session_state": state,
                    "poll_interval": 0.01,
                    "stop_event": stop_event,
                    "claude_session_roots": [root],
                },
                daemon=True,
            )

            try:
                thread.start()
                self._wait_for(lambda: len(events) >= 1)
                self.assertEqual(events[-1]["status"], "ok")
                self.assertEqual(events[-1]["used_tokens"], 100)

                first_generation_events = len(events)
                state.set_session_id(new_id)
                self._wait_for(lambda: len(events) > first_generation_events)
                self.assertEqual(events[-1]["status"], "unavailable")
                self.assertEqual(events[-1]["reason"], "session jsonl not found")

                unavailable_events = len(events)
                (root / f"{new_id}.jsonl").write_text(self._claude_usage_line(7), "utf-8")
                self._wait_for(
                    lambda: len(events) > unavailable_events
                    and events[-1].get("used_tokens") == 7
                )

                self.assertFalse(any(event.get("used_tokens") == 100 for event in events[1:]))
            finally:
                stop_event.set()
                thread.join(timeout=0.5)
                wrapper_module._report_usage_event = original_report


if __name__ == "__main__":
    unittest.main()
