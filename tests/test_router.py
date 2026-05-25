import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from router import Router


class RouterMentionTests(unittest.TestCase):
    def test_hyphenated_agent_name_is_parsed_as_full_mention(self):
        router = Router(["telegram-bridge"], default_mention="none")

        self.assertEqual(
            set(router.parse_mentions("please ask @telegram-bridge to check")),
            {"telegram-bridge"},
        )

    def test_shorter_agent_name_does_not_match_prefix_of_hyphenated_unknown(self):
        router = Router(["telegram"], default_mention="none")

        self.assertEqual(router.parse_mentions("@telegram-bridge check"), [])
        self.assertEqual(router.get_targets("ben", "@telegram-bridge check"), [])

    def test_longest_hyphenated_name_wins_when_prefix_agent_also_exists(self):
        router = Router(["telegram", "telegram-bridge"], default_mention="none")

        self.assertEqual(
            set(router.parse_mentions("@telegram-bridge check")),
            {"telegram-bridge"},
        )

    def test_unknown_exact_handle_still_does_not_route(self):
        router = Router(["telegram-bridge"], default_mention="none")

        self.assertEqual(router.parse_mentions("@telegram-bot check"), [])
        self.assertEqual(router.get_targets("ben", "@telegram-bot check"), [])

    def test_mentions_inside_markdown_code_do_not_route(self):
        router = Router(["claude", "codex"], default_mention="none")

        self.assertEqual(router.parse_mentions("example: `@codex check this`"), [])
        self.assertEqual(router.parse_mentions("```text\n@claude check this\n```"), [])
        self.assertEqual(router.get_targets("ben", "example: `@codex check this`"), [])

    def test_agent_message_only_routes_leading_mentions(self):
        router = Router(["claude", "codex"], default_mention="none")

        self.assertEqual(router.get_targets("claude", "Example: @codex check this"), [])
        self.assertEqual(router.get_targets("claude", "Please ask @codex to check"), [])
        self.assertEqual(router.get_targets("claude", "@codex check this"), ["codex"])
        self.assertEqual(router.get_targets("claude", "@codex, check this"), ["codex"])

    def test_agent_message_routes_leading_mentions_on_new_lines(self):
        router = Router(["claude", "codex"], default_mention="none")

        self.assertEqual(
            router.get_targets("claude", "I am done.\n@codex please review"),
            ["codex"],
        )


if __name__ == "__main__":
    unittest.main()
