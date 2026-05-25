"""Tests for provider usage telemetry and terminal permission matching."""

import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wrapper import (  # noqa: E402
    _build_profile_args,
    _find_codex_rollout_file,
    _looks_like_permission_prompt,
    _parse_claude_usage_lines,
    _parse_codex_usage_lines,
    _profile_claude_session_id,
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


if __name__ == "__main__":
    unittest.main()
