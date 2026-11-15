"""Desktop says what the chat window and detection do not cover (issue #40)."""
import subprocess
import unittest
from pathlib import Path

from test_operator_recovery import HARNESS

ROOT = Path(__file__).resolve().parents[1]

STORE = r"""
const store=new Function('env',`with(env){${views};return{coverageNote};}`)(env);
const texts=tree=>all(tree).filter(x=>typeof x==='string').join('|');
"""


class Coverage(unittest.TestCase):
    def run_views(self, script):
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                result = subprocess.run(["node", "-e", HARNESS + STORE + script, str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_window_and_fixed_notes_are_shown_as_plain_text(self):
        self.run_views(r"""
const q={coverage:{chat:{window_days:30},not_covered:['Chats older than 30 days are not read.','<b>x</b> group asks']}};
const t=texts(store.coverageNote(q));
assert.ok(t.includes('Chats checked: last 30 days.'),t);
assert.ok(t.includes('Chats older than 30 days are not read.'));
assert.ok(t.includes('<b>x</b> group asks'),'markup stays inert text');
""")

    def test_missing_or_failed_coverage_renders_nothing_or_only_notes(self):
        self.run_views(r"""
assert.equal(store.coverageNote({}),null);
assert.equal(store.coverageNote({coverage:{chat:null,not_covered:[]}}),null);
const t=texts(store.coverageNote({coverage:{chat:null,not_covered:['Chats could not be read this time.']}}));
assert.ok(t.includes('What this list does not cover')&&!t.includes('Chats checked'),t);
""")

    def test_page_renders_the_note_after_the_list(self):
        src = (ROOT / "desktop/src/plugin.src.js").read_text()
        self.assertIn("!q.loading ? coverageNote(q) : null", src)


if __name__ == "__main__":
    unittest.main()
