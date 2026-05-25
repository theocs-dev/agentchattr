import json
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _extract_mention_source(js: str, const_name: str) -> str:
    match = re.search(rf"const {const_name} = String\.raw`([^`]+)`;", js)
    if not match:
        raise AssertionError(f"{const_name} not found")
    return match.group(1)


class UnicodeMentionRegexTests(unittest.TestCase):
    def test_client_mention_patterns_are_unicode_aware(self):
        chat_js = _read("static/chat.js")
        jobs_js = _read("static/jobs.js")

        chat_source = _extract_mention_source(chat_js, "MENTION_NAME_SOURCE")
        jobs_source = _extract_mention_source(jobs_js, "JOB_MENTION_NAME_SOURCE")

        self.assertEqual(chat_source, jobs_source)
        self.assertIn(r"\p{L}", chat_source)
        self.assertIn(r"\p{N}", chat_source)
        self.assertIn(r"\p{M}", chat_source)

    def test_client_mention_patterns_match_precomposed_and_decomposed_accents(self):
        chat_source = _extract_mention_source(_read("static/chat.js"), "MENTION_NAME_SOURCE")
        script = f"""
const source = String.raw`{chat_source}`;
const mentionRe = new RegExp(`@(${{source}})`, 'gu');
const bareMentionRe = new RegExp(`@${{source}}`, 'gu');
const precomposed = '@Th\u00e9o';
const decomposed = '@The\\u0301o';
const matches = [
  precomposed.match(mentionRe),
  decomposed.match(mentionRe),
  `hello ${{decomposed}} ping`.replace(bareMentionRe, '').trim()
];
console.log(JSON.stringify(matches));
"""
        result = subprocess.run(
            ["node", "-e", script],
            cwd=ROOT,
            check=True,
            text=True,
            capture_output=True,
        )

        self.assertEqual(
            json.loads(result.stdout),
            [["@Th\u00e9o"], ["@The\u0301o"], "hello  ping"],
        )

    def test_legacy_ascii_mention_patterns_are_removed_from_client_code(self):
        combined = "\n".join([_read("static/chat.js"), _read("static/jobs.js")])

        self.assertNotIn(r"@(\w[\w-]*)", combined)
        self.assertNotIn(r"@\w[\w-]*", combined)
        self.assertNotIn(r"@([a-zA-Z][\w-]*)", combined)


if __name__ == "__main__":
    unittest.main()
