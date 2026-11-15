"""Round-5 docs regression tests for the surface matrix (issue #47).

Each test reproduces a round-5 review finding with real files and was seen to fail against the code
before its fix. Siblings are loaded by file path so the tests also run under a shadowing ``tests``
package."""

import importlib.util
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent


def _sibling(name):
    spec = importlib.util.spec_from_file_location(f"ub_r5s09_{name}", _HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


outside = _sibling("test_outside")
needs = outside.needs

GUIDE = (ROOT / "docs/operator-guide.md").read_text()
VERIFICATION = (ROOT / "docs/verification.md").read_text()


class PocketActionsMatchTheGuide(unittest.TestCase):
    """R5-desk-027 (class I): the surface row must describe the options a pack really carries."""

    def pocket_row(self):
        rows = [r for r in GUIDE.splitlines() if r.startswith("| Pocket pack |")]
        self.assertEqual(len(rows), 1)
        return " ".join(rows[0].split())

    def test_an_ordinary_kanban_ask_carries_two_named_choices(self):
        ask = {"source": "kanban", "id": "kanban:default:t1", "board": "default", "task_id": "t1", "tier": "ask",
               "title": "Card", "ask": "Question?", "why": "blocked", "profile": "default", "version": "3"}
        cards = needs.pocket_pack({"items": [ask], "errors": []})["cards"]
        options = cards[0]["options"]
        self.assertEqual([o["send"] for o in options], [
            "Answer and unblock t1 on board default (block 3)",
            "Keep t1 on board default (block 3) blocked",
        ])

    def test_the_pocket_row_says_so(self):
        row = self.pocket_row()
        self.assertNotIn("One fixed pointer per card", row)
        self.assertNotIn("inline action", row)
        self.assertIn("Kanban", row)
        self.assertIn("Answer and unblock", row)
        self.assertIn("Keep <task> blocked", row)
        self.assertIn("never an approval", row)

    def test_the_verification_table_does_not_claim_packs_have_no_action(self):
        self.assertNotIn("Pocket packs carry no inline action", VERIFICATION)


if __name__ == "__main__":
    unittest.main()
