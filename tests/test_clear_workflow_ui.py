import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read_chat_js() -> str:
    return (ROOT / "static/chat.js").read_text(encoding="utf-8")


def _clear_workflow_helpers() -> str:
    js = _read_chat_js()
    start = js.index("const CLEAR_WORKFLOW_STATES =")
    end = js.index("};", start) + len("};")
    parts = [js[start:end]]
    for name in (
        "getClearWorkflowState",
        "getClearWorkflowActionState",
        "formatClearWorkflowReason",
        "getClearWorkflowAgents",
        "getClearWorkflowSummary",
    ):
        fn_start = js.index(f"function {name}")
        brace = js.index("{", fn_start)
        depth = 0
        for idx in range(brace, len(js)):
            if js[idx] == "{":
                depth += 1
            elif js[idx] == "}":
                depth -= 1
                if depth == 0:
                    parts.append(js[fn_start:idx + 1])
                    break
        else:
            raise AssertionError(f"{name} body not found")
    return "\n\n".join(parts)


def _run_clear_workflow_script(script: str):
    result = subprocess.run(
        ["node", "-e", _clear_workflow_helpers() + "\n" + script],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


class ClearWorkflowUiTests(unittest.TestCase):
    def test_presence_never_promotes_terminal_clear_support(self):
        rows = _run_clear_workflow_script("""
const rows = getClearWorkflowAgents({
  claude: {
    label: 'Claude',
    available: true,
    clear_supported: false,
    clear_state: { state: 'not_supported', reason: 'clear_not_supported' },
  },
  minimax: {
    label: 'MiniMax',
    available: true,
    clear_supported: false,
    clear_state: { state: 'not_applicable', reason: 'stateless_chat_replay' },
  },
  pilot: {
    label: 'Claude pilot',
    available: false,
    busy: true,
    clear_supported: true,
    clear_strategy: 'session_restart',
    clear_state: { state: 'not_supported' },
  },
  pilot_idle: {
    label: 'Claude pilot idle',
    available: true,
    busy: false,
    clear_supported: true,
    clear_strategy: 'session_restart',
    clear_state: { state: 'not_supported' },
  },
});
console.log(JSON.stringify(rows.map(row => ({
  name: row.name,
  state: row.state,
  clearSupported: row.clearSupported,
  actionState: row.actionState,
  detail: row.detail,
}))));
""")

        self.assertEqual(rows[0]["state"], "not_supported")
        self.assertFalse(rows[0]["clearSupported"])
        self.assertEqual(rows[1]["state"], "not_applicable")
        self.assertNotEqual(rows[1]["state"], "failed")
        self.assertEqual(rows[2]["state"], "supported")
        self.assertTrue(rows[2]["clearSupported"])
        self.assertEqual(rows[2]["actionState"], "blocked_busy")
        self.assertIn("no terminal clear request is sent", rows[2]["detail"])
        self.assertEqual(rows[3]["state"], "supported")
        self.assertEqual(rows[3]["actionState"], "requires_explicit_confirmation")
        self.assertIn("Not requested by this chat clear", rows[3]["detail"])

    def test_pending_confirmed_and_failed_are_rendered_per_agent(self):
        rows = _run_clear_workflow_script("""
const rows = getClearWorkflowAgents({
  claude: { clear_supported: true, clear_state: { state: 'pending' } },
  codex: { clear_supported: true, clear_state: { state: 'confirmed', reason: 'wrapper_machine' } },
  qwen: { clear_supported: true, clear_state: { state: 'failed', reason: 'adapter_refused' } },
});
const summary = getClearWorkflowSummary(rows);
console.log(JSON.stringify({
  states: rows.map(row => row.state),
  details: rows.map(row => row.detail),
  summary,
}));
""")

        self.assertEqual(rows["states"], ["pending", "confirmed", "failed"])
        self.assertEqual(rows["summary"]["supported"], 3)
        self.assertEqual(rows["summary"]["pending"], 1)
        self.assertIn("adapter refused", rows["details"][2])

    def test_clear_chat_ui_sends_only_chat_clear_command(self):
        chat_js = _read_chat_js()

        self.assertIn("function _sendClearChatOnly()", chat_js)
        self.assertEqual(chat_js.count("text: '/clear'"), 1)
        self.assertIn("agentConfig[name].busy = !!info.busy;", chat_js)
        self.assertIn("Terminal state stays as shown below.", chat_js)


if __name__ == "__main__":
    unittest.main()
