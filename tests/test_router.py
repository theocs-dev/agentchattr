import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from router import Router
from registry import RuntimeRegistry


AGENTS = {
    "codex": {"label": "Codex", "color": "#10a37f"},
}


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

    def test_default_all_matches_online_all_semantics(self):
        router = Router(
            ["claude", "codex", "gemini"],
            default_mention="all",
            online_checker=lambda: {"claude", "gemini"},
        )

        self.assertEqual(router.get_targets("ben", "please check"), ["claude", "gemini"])

    def test_default_specific_agent_routes_without_explicit_mention(self):
        router = Router(["claude", "codex"], default_mention="codex")

        self.assertEqual(router.get_targets("ben", "please check"), ["codex"])

    def test_explicit_mention_overrides_default_agent(self):
        router = Router(["claude", "codex"], default_mention="codex")

        self.assertEqual(router.get_targets("ben", "@claude please check"), ["claude"])

    def test_unknown_slash_command_does_not_use_default_route(self):
        router = Router(["claude", "codex"], default_mention="all")

        self.assertEqual(router.get_targets("ben", "/unknown"), [])
        self.assertEqual(router.get_targets("ben", "@codex /unknown"), ["codex"])


class RegistryRenameChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = RuntimeRegistry(data_dir=self.tmp.name)
        self.registry.seed(AGENTS)

    def test_registered_instance_stops_stale_rename_chain(self):
        self.registry.register("codex")
        self.registry.register("codex")
        # Simulate persisted stale data from an earlier rename-back cycle.
        self.registry._renames["codex-2"] = "codex"

        self.assertEqual(self.registry.get_all_names(), ["codex-1", "codex-2"])
        self.assertEqual(self.registry.resolve_name("codex-2"), "codex-2")

    def test_heartbeat_resolution_does_not_refresh_ghost_instance(self):
        self.registry.register("codex")
        self.registry.register("codex")
        self.registry._renames["codex-2"] = "codex"

        presence = {}
        current_name = "codex-2"
        presence[current_name] = 1
        canonical = self.registry.resolve_name(current_name)
        inst = self.registry.get_instance(canonical)
        if inst and canonical != current_name:
            presence[canonical] = 1

        self.assertEqual(canonical, "codex-2")
        self.assertNotIn("codex-1", presence)

    def test_fresh_registration_purges_stale_outgoing_rename(self):
        self.registry._renames["codex"] = "codex-1"

        self.registry.register("codex")

        self.assertNotIn("codex", self.registry._renames)
        self.assertEqual(self.registry.resolve_name("codex"), "codex")

    def test_rename_back_purges_stale_target_key(self):
        self.registry.register("codex")
        self.registry.register("codex")
        self.registry._renames["codex"] = "codex-1"

        self.registry.deregister("codex-2")

        self.assertEqual(self.registry.get_all_names(), ["codex"])
        self.assertNotIn("codex", self.registry._renames)
        self.assertEqual(self.registry.resolve_name("codex-1"), "codex")
        self.assertEqual(self.registry.resolve_name("codex"), "codex")

    def test_true_rename_from_old_unregistered_name_still_resolves(self):
        self.registry.register("codex")

        result = self.registry.rename("codex", "codex-review")

        self.assertIsInstance(result, dict)
        self.assertEqual(self.registry.resolve_name("codex"), "codex-review")
        self.assertEqual(self.registry.resolve_name("codex-review"), "codex-review")


if __name__ == "__main__":
    unittest.main()
