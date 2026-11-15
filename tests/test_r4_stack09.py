"""Round 4, surface matrix (#47): honest counts, bounded limits, withheld password prompts, docs."""

import argparse
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_outside import OutsideFixture, needs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("unbl0ck_pkg_r4s09", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
pkg = importlib.util.module_from_spec(spec)
sys.modules["unbl0ck_pkg_r4s09"] = pkg
spec.loader.exec_module(pkg)

GUIDE = (ROOT / "docs/operator-guide.md").read_text()
MARK = "SUDO-PROMPT-EXCERPT-MARK"


def ask(i):
    return {"source": "kanban", "id": f"kanban:default:t{i}", "board": "default", "task_id": f"t{i}", "tier": "ask",
            "title": f"Card {i}", "ask": f"Question {i}?", "why": "blocked", "profile": "default", "version": "1"}


def with_queue(q):
    pkg._needs = lambda: types.SimpleNamespace(build_queue=lambda: q, pocket_pack=needs.pocket_pack, _clean=needs._clean, _SUDO_WITHHELD=needs._SUDO_WITHHELD)
    return q


class UnreadSourceCount(unittest.TestCase):
    """09-003 (class E, false-complete coverage): unread_sources counts sources, not messages."""

    def test_two_failures_in_one_source_are_one_unread_source(self):
        pack = needs.pocket_pack({"items": [], "errors": ["outside: a.txt: OSError", "outside: b.txt: OSError"]})
        self.assertEqual(pack["omitted"]["unread_sources"], 1)
        self.assertFalse(pack["complete"])

    def test_failures_in_two_sources_are_two(self):
        pack = needs.pocket_pack({"items": [], "errors": ["kanban[a]: OperationalError", "chat[p]: OSError", "chat[p]: again"]})
        self.assertEqual(pack["omitted"]["unread_sources"], 2)

    def test_state_and_limit_notices_are_not_sources(self):
        notices = [
            "outside: could not remove 2 expired done record(s); their text stays until a later cleanup succeeds",
            "outside: retention cleanup did not run: OSError; it retries on the next poll",
            "outside: x.json: invalid done_at; kept for repair",
            "outside: 200 agent-filed items are waiting (limit 200); agents cannot file more until you finish some",
        ]
        pack = needs.pocket_pack({"items": [], "errors": notices})
        self.assertEqual(pack["omitted"]["unread_sources"], 0)
        self.assertTrue(pack["complete"])

    def test_a_listing_cut_makes_the_pack_incomplete_without_an_unread_source(self):
        pack = needs.pocket_pack({"items": [], "errors": ["outside: exports: folder limit, showing the newest 200; 3 older files are waiting. Finish or hide items to bring them into view"]})
        self.assertEqual(pack["omitted"]["unread_sources"], 0)
        self.assertFalse(pack["complete"])


class FilledToCapacity(OutsideFixture):
    def test_a_full_filed_queue_is_not_an_unread_source(self):
        with patch.object(needs, "_OUTSIDE_LIMIT", 3):
            for n in range(3):
                self.file(dedupe_key=f"k{n}", title=f"T{n}")
            q = needs.build_queue()
        self.assertTrue(any("agent-filed items are waiting" in e for e in q["errors"]), q["errors"])
        pack = needs.pocket_pack(q)
        self.assertEqual(pack["omitted"]["unread_sources"], 0)
        self.assertTrue(pack["complete"])


class NegativeLimits(unittest.TestCase):
    """09-004 (class M, negative slice): a negative limit never selects cards or miscounts."""

    def test_pack_with_a_negative_limit_selects_nothing_and_counts_every_ask(self):
        q = {"items": [ask(1), ask(2), ask(3)], "errors": []}
        pack = needs.pocket_pack(q, limit=-1)
        self.assertEqual(len(pack["cards"]), 0)
        self.assertEqual(pack["omitted"]["asks"], 3)
        self.assertFalse(pack["complete"])

    def test_omitted_asks_always_equals_asks_minus_cards(self):
        q = {"items": [ask(i) for i in range(5)], "errors": []}
        for limit in (-7, -1, 0, 1, 4, 5, 9):
            pack = needs.pocket_pack(q, limit=limit)
            self.assertEqual(pack["omitted"]["asks"], 5 - len(pack["cards"]), limit)

    def test_summary_with_a_negative_limit_lists_nothing_and_says_how_many(self):
        with_queue({"count": 3, "counts": {"asks": 3}, "errors": [], "items": [ask(1), ask(2), ask(3)]})
        out = pkg._summary(limit=-1)
        self.assertFalse("Card " in out)
        self.assertIn("and 3 more", out)


class PasswordPromptsStayHome(unittest.TestCase):
    """09-002 (class I, docs contradict behavior): a password prompt's text is on neither the
    local list nor the Pocket pack."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        needs.record_wait(home, kind="sudo", request_id="a" * 32, prompt=f"[sudo] password for me: {MARK}")
        needs.record_wait(home, kind="approval", request_id="b" * 32, prompt="rm -rf build/ APPROVAL-MARK")
        items = needs.wait_items(home)
        self.assertEqual({i["kind"] for i in items}, {"sudo", "approval"})
        self.q = with_queue({"count": 2, "counts": {"asks": 2}, "errors": [], "items": items})

    def test_pocket_pack_withholds_the_prompt_text(self):
        pack = needs.pocket_pack(self.q)
        blob = json.dumps(pack)
        self.assertNotIn(MARK, blob)
        self.assertIn("APPROVAL-MARK", blob)  # other prompts keep their text
        card = next(c for c in pack["cards"] if "sudo" in c["id"] or "a" * 32 in c["id"])
        self.assertIn("password", card["prompt"].lower())

    def test_local_list_withholds_the_prompt_text(self):
        out = pkg._summary()
        self.assertNotIn(MARK, out)
        self.assertIn("APPROVAL-MARK", out)

    def test_docs_say_where_a_password_prompt_is_visible(self):
        flat = " ".join(GUIDE.split())
        self.assertNotIn("password prompts and pairing codes never leave the original surface", flat)
        self.assertIn("local list and the Pocket pack withhold a password prompt's text", flat)


class CliJson(unittest.TestCase):
    """09-005 (class L): the --json surface is the serialized full queue, tested by running it."""

    def test_cli_json_prints_every_tier_of_the_queue(self):
        stuck = dict(ask(90), tier="stuck")
        answered = dict(ask(91), tier="answered")
        q = with_queue({"count": 1, "counts": {"asks": 1, "stuck": 1, "answered": 1}, "errors": ["chat: x"], "items": [ask(1), stuck, answered]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pkg._cli(argparse.Namespace(json=True, pack=False))
        data = json.loads(out.getvalue())
        self.assertEqual(data, q)
        self.assertEqual({i["tier"] for i in data["items"]}, {"ask", "stuck", "answered"})


class CompletionIsPerAction(unittest.TestCase):
    """09-001 (class I): the Desktop completion cell does not promise every card leaves."""

    def test_keep_blocked_is_not_promised_to_leave(self):
        row = next(l for l in GUIDE.splitlines() if l.startswith("| Desktop |"))
        cell = [c.strip() for c in row.strip("|").split("|")][4]
        self.assertNotIn("The card leaves on the next poll;", cell)
        self.assertIn("keep blocked", cell.lower())
        self.assertIn("answered", cell.lower())


class DesktopSubtitleIsNotAFalseFault(unittest.TestCase):
    """Sweep extra (class I): errors include capacity and cleanup notices, so the subtitle must
    not claim that a source failed."""

    def test_subtitle_does_not_say_a_source_failed(self):
        src = (ROOT / "desktop/src/plugin.src.js").read_text()
        self.assertFalse("A source failed to load" in src)
        self.assertTrue("See the notices below; the list may be incomplete." in src)


if __name__ == "__main__":
    unittest.main()
