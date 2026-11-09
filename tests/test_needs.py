import importlib.util
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("unbl0ck_needs", ROOT / "needs.py")
needs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(needs)
needs._settings = lambda: {"principal_name": "Simon", "comment_author": "simon"}


class Classify(unittest.TestCase):
    def test_gate_reasons_need_you(self):
        for r in ["Awaiting Simon's approval to publish", "CEO-gated: repo publication",
                  "needs Simon to pick option A or B", "human gate: credentials"]:
            self.assertTrue(needs.kanban_needs_you(r, None), r)

    def test_needs_input_naming_principal(self):
        self.assertTrue(needs.kanban_needs_you("Which port should Simon want here", "needs_input"))
        self.assertIsNone(needs.kanban_needs_you("Which port", "needs_input"))

    def test_not_asks(self):
        self.assertIsNone(needs.kanban_needs_you("waiting on CI", None))
        self.assertIsNone(needs.kanban_needs_you("", "needs_input"))
        # a stop order that names Simon is not a question for Simon
        self.assertIsNone(needs.kanban_needs_you(
            "Simon 18:07 PT: Claude Code supervisor owns this repair; needs Simon later", None))

    def test_chat_asks(self):
        self.assertTrue(needs.chat_asks_you("Done drafting. Your call: ship A or B?"))
        self.assertTrue(needs.chat_asks_you("Should I open the PR now?"))
        self.assertFalse(needs.chat_asks_you("Deployed v6. All 914 tests pass."))
        long_packet = "Four picks for the CEO:\n1. Ship A.\n2. Park B.\n" + "Detail line. " * 120
        self.assertTrue(needs.chat_asks_you(long_packet))
        self.assertFalse(needs.chat_asks_you("Is the build green? Yes."[:0]))


def make_home(tmp: Path, last_role="assistant", last_text="Want me to merge it?", age=60):
    prof = tmp / "profiles" / "income"
    prof.mkdir(parents=True)
    db = sqlite3.connect(prof / "state.db")
    db.executescript("""
      CREATE TABLE sessions(id TEXT, title TEXT, archived INT);
      CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                            timestamp REAL, display_kind TEXT, active INT);
    """)
    now = time.time()
    db.execute("INSERT INTO sessions VALUES('s1','Bot Chat',0)")
    db.execute("INSERT INTO sessions VALUES('s2','kanban t_1',0)")
    db.execute("INSERT INTO messages VALUES(1,'s1','user','hi',?,NULL,1)", (now - age - 10,))
    db.execute("INSERT INTO messages VALUES(2,'s1',?,?,?,NULL,1)", (last_role, last_text, now - age))
    db.execute("INSERT INTO messages VALUES(3,'s2','assistant','Should I?',?,NULL,1)", (now,))
    db.commit()
    db.close()
    return tmp


class Chats(unittest.TestCase):
    def test_bot_chat_question_is_queued(self):
        with tempfile.TemporaryDirectory() as d:
            items = needs.chat_items(make_home(Path(d)))
            self.assertEqual([i["session_id"] for i in items], ["s1"])
            self.assertEqual(items[0]["version"], "2")

    def test_user_replied_last(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(needs.chat_items(make_home(Path(d), last_role="user")), [])

    def test_old_ask_ages_out(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(needs.chat_items(make_home(Path(d), age=30 * 86400)), [])

    def test_hide_until_changed(self):
        with tempfile.TemporaryDirectory() as d:
            home = make_home(Path(d))
            needs.kanban_items = lambda boards=None: []
            q = needs.build_queue(home)
            self.assertEqual(q["count"], 1)
            needs.hide(q["items"][0]["id"], q["items"][0]["version"], home)
            self.assertEqual(needs.build_queue(home)["count"], 0)
            needs.hide(q["items"][0]["id"], "older", home)  # source changed → shows again
            self.assertEqual(needs.build_queue(home)["count"], 1)

    def test_pocket_pack_shape(self):
        with tempfile.TemporaryDirectory() as d:
            home = make_home(Path(d))
            needs.kanban_items = lambda boards=None: []
            pack = needs.pocket_pack(needs.build_queue(home))
            self.assertEqual(pack["mode"], "unblock")
            card = pack["cards"][0]
            for k in ("id", "kind", "title", "short", "choices", "options", "prompt", "why_now", "ttl_s"):
                self.assertIn(k, card)
            p = needs.write_pocket_pack(needs.build_queue(home), home)
            self.assertEqual(p.parent.name, "unbl0ck")  # never the gadget plugin's pack


class FakeKanban(unittest.TestCase):
    """answer_card writes only through native kanban_db calls."""

    def setUp(self):
        self.calls = []
        task = types.SimpleNamespace(id="t_1", status="blocked")
        kdb = types.ModuleType("hermes_cli.kanban_db")
        kdb.get_task = lambda c, t: task if t == "t_1" else None
        kdb.add_comment = lambda c, t, author, body: self.calls.append(("comment", t, author, body)) or 7
        kdb.unblock_task = lambda c, t: self.calls.append(("unblock", t)) or True
        kbc = types.ModuleType("hermes_cli.kanban_db_connect")
        kbc.connect = lambda board=None: sqlite3.connect(":memory:")
        pkg = types.ModuleType("hermes_cli")
        pkg.kanban_db, pkg.kanban_db_connect = kdb, kbc
        self._saved = {k: sys.modules.get(k) for k in ("hermes_cli", "hermes_cli.kanban_db", "hermes_cli.kanban_db_connect")}
        sys.modules.update({"hermes_cli": pkg, "hermes_cli.kanban_db": kdb, "hermes_cli.kanban_db_connect": kbc})

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def test_answer_and_release(self):
        r = needs.answer_card("factory", "t_1", "Go with A", release=True)
        self.assertEqual(r, {"ok": True, "comment_id": 7, "released": True})
        self.assertEqual(self.calls, [("comment", "t_1", "simon", "Go with A"), ("unblock", "t_1")])

    def test_answer_only(self):
        needs.answer_card("factory", "t_1", "noted", release=False)
        self.assertEqual([c[0] for c in self.calls], ["comment"])

    def test_empty_and_missing(self):
        with self.assertRaises(ValueError):
            needs.answer_card("factory", "t_1", "  ", release=True)
        with self.assertRaises(LookupError):
            needs.answer_card("factory", "t_9", "x", release=True)



class PrincipalName(unittest.TestCase):
    def test_other_name_detected_and_simon_not_hardcoded(self):
        orig = needs._settings
        needs._settings = lambda: {"principal_name": "Ada"}
        try:
            self.assertTrue(needs.kanban_needs_you("Awaiting Ada's approval to publish", None))
            self.assertIsNone(needs.kanban_needs_you("Awaiting Simon's approval to publish", None))
            self.assertTrue(needs.kanban_needs_you("CEO-gated: repo publication", None))
            self.assertEqual(needs.principal_author(), "Ada")
        finally:
            needs._settings = orig

    def test_no_name_configured_uses_generic_words(self):
        self.assertEqual(needs.principal_names({}), [])
        self.assertEqual(needs.principal_author({}), "user")
        self.assertTrue(needs._patterns([])["_GATE"].search("needs human approval"))


if __name__ == "__main__":
    unittest.main()
