"""#42: Desktop shows the live disclosure matrix: per source, whether its text may reach the provider."""

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def render(queue):
    s = (ROOT / "desktop/src/plugin.src.js").read_text()
    start = s.index("function disclosureNote(q) {")
    end = s.index("\nfunction ", start + 1)
    script = (
        "const jsx = (t, p) => ({t, ...p}); const jsxs = jsx;\n"
        "const text = n => n == null ? '' : typeof n === 'string' ? n"
        " : Array.isArray(n) ? n.map(text).join('\\n') : text(n.children);\n"
        + s[start:end]
        + f"\nconsole.log(text(disclosureNote({json.dumps(queue)})))"
    )
    return subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout


class DesktopDisclosure(unittest.TestCase):
    def test_each_source_says_whether_its_text_may_reach_the_provider(self):
        out = render({"disclosure": [
            {"source": "chat", "summary": True, "ask": False, "search": True, "cross_profile": True},
            {"source": "kanban", "summary": True, "ask": True, "search": False, "cross_profile": True},
            {"source": "pairing", "summary": False, "ask": False, "search": False, "cross_profile": True},
        ]})
        self.assertIn("What can leave this machine: 2 of 3 sources", out)
        self.assertIn("chat: text may go to the model provider for TL;DR summaries", out)
        self.assertIn("kanban: text may go to the model provider for TL;DR summaries and Ask", out)
        self.assertIn("pairing: text is never sent to the model provider", out)

    def test_with_summaries_and_ask_off_nothing_is_sent(self):
        out = render({"disclosure": [{"source": "chat", "summary": False, "ask": False}]})
        self.assertIn("Nothing here is sent to a model provider", out)

    def test_the_panel_is_rendered_under_the_queue(self):
        s = (ROOT / "desktop/src/plugin.src.js").read_text()
        self.assertIn("!q.loading ? disclosureNote(q) : null", s)


if __name__ == "__main__":
    unittest.main()
