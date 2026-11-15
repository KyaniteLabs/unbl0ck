"""Round-5 findings on the Ask-detection branch (#40): backfill protection that survives narrowing,
the 1000-id cap and concurrent builds, a read-only search that creates nothing, and surfaces that
never claim an all-clear over chats that were not read. Real sqlite databases and real files."""

import inspect
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

from r4c_common import ASK, HomeCase, needs
from test_ask_coverage import settings
from test_r4_stack01 import PRELUDE, run_views

DAY = 86400


def seed(home: Path, profile: str, rows):
    """rows: (session id, age in days). One assistant ask per session."""
    path = home / "profiles" / profile / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE sessions(id TEXT, title TEXT, source TEXT, chat_type TEXT, started_at REAL,"
        " last_activity_at REAL, user_id TEXT, display_name TEXT)"
    )
    db.execute(
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,"
        " timestamp REAL, display_kind TEXT, active INT)"
    )
    now = time.time()
    for sid, age in rows:
        ts = now - age * DAY
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)", (sid, "", "cli", "dm", ts, ts, "u", "n"))
        db.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES(?, 'assistant', ?, ?)", (sid, ASK, ts))
    db.commit()
    db.close()


def mark(home, items, since, record):
    """_mark_backfill, whichever way the branch under test spells its arguments."""
    extra = {"complete": True} if "complete" in inspect.signature(needs._mark_backfill).parameters else {}
    needs._mark_backfill(home, items, since, record=record, errors=[], **extra)


class BackfillCase(HomeCase):
    def setUp(self):
        super().setUp()
        kanban = self.base / "kanban"
        kanban.mkdir()
        p = patch.dict(os.environ, {"HERMES_HOME": str(self.home), "HERMES_KANBAN_HOME": str(kanban)})
        p.start()
        self.addCleanup(p.stop)

    def chats(self, days, **kw):
        with patch.object(needs, "_settings", settings(chat_window_days=days)):
            return {i["session_id"]: i for i in needs.build_queue(self.home)["items"] if i["source"] == "chat"}


class BackfillProtectionIsSticky(BackfillCase):
    def test_narrowing_the_window_does_not_remove_the_protection_of_a_backfilled_ask(self):
        """R5-needs-005 / R5-api-004 (class E, silent loss of a promise): 1 -> 7 -> 1 -> 7 days."""
        seed(self.home, "work", [("recent", 0.2), ("three", 3)])
        self.assertEqual(sorted(self.chats(1)), ["recent"])
        self.assertTrue(self.chats(7)["three"].get("backfilled"))
        self.assertEqual(sorted(self.chats(1)), ["recent"])
        self.assertTrue(self.chats(7)["three"].get("backfilled"), "the second widening must stay protected")

    def test_more_than_the_cap_of_marked_chats_all_stay_protected(self):
        """R5-needs-006 / R5-api-005 (class H): 1001 chats over three profiles, then a later refresh."""
        for profile, lo, hi in (("a", 0, 334), ("b", 334, 668), ("c", 668, 1001)):
            seed(self.home, profile, [(f"s{n}", 3 + n / 100000) for n in range(lo, hi)])
        self.assertEqual(self.chats(1), {})
        first = self.chats(7)
        self.assertEqual(len(first), 1001)
        self.assertTrue(all(i.get("backfilled") for i in first.values()))
        saved = json.loads((needs.data_dir(self.home, create=False) / needs.BACKFILL_FILE).read_text())
        self.assertLessEqual(len(saved["ids"]), needs.BACKFILL_IDS_MAX)
        second = self.chats(7)
        self.assertEqual(len(second), 1001)
        self.assertEqual([k for k, i in second.items() if not i.get("backfilled")], [])

    def test_the_marks_are_written_under_the_shared_state_lock(self):
        """R5-needs-007 / R5-desk-016 (class B): the read-modify-write holds the state lock."""
        entered = []
        real = needs._state_lock

        def spy(home):
            entered.append(1)
            return real(home)

        item = {"id": "chat:work:x", "since": int(time.time() - 3 * DAY)}
        with patch.object(needs, "_state_lock", spy):
            mark(self.home, [item], time.time() - DAY, True)
        self.assertTrue((needs.data_dir(self.home, create=False) / needs.BACKFILL_FILE).exists())
        self.assertTrue(entered, "the backfill file was written without the state lock")

    def test_a_stored_time_past_the_float_range_is_damage_to_that_field_not_a_failure(self):
        """R5-needs-021 (backfill floor reader; class M, malformed record isolation)."""
        (needs.data_dir(self.home) / needs.BACKFILL_FILE).write_text(
            '{"floor": 1' + "0" * 400 + ', "ids": ["chat:work:x"], "id_ts": {"chat:work:x": 1' + "0" * 400 + "}}"
        )
        floor, marks, before, readable = needs._backfill_state(self.home)
        self.assertEqual((floor, marks, before, readable), (None, {"chat:work:x": None}, None, True))

    def test_two_overlapping_builds_do_not_lose_each_others_marks(self):
        """R5-needs-007 (class B): a build that read the floor earlier must not overwrite a newer one."""
        now = time.time()
        a = {"id": "chat:work:a", "since": int(now - 10 * DAY)}
        b = {"id": "chat:work:b", "since": int(now - 12 * DAY)}
        needs._write_json(
            needs.data_dir(self.home) / needs.BACKFILL_FILE, {"floor": now - 7 * DAY, "ids": []}, ordered=True
        )
        second_done = threading.Event()
        paused = []
        real_state, real_lock = needs._backfill_state, needs._state_lock

        def pause_once():
            if threading.current_thread().name == "first" and not paused:
                paused.append(1)
                second_done.wait(2.0)  # the second build runs to the end while the first waits

        def state(home):
            out = real_state(home)
            pause_once()  # unlocked code has read by now: this is the stale read
            return out

        def lock(home):
            pause_once()  # locked code has not read yet: it reads after the second build wrote
            return real_lock(home)

        def build(items):
            mark(self.home, items, now - 14 * DAY, True)

        with patch.object(needs, "_backfill_state", state), patch.object(needs, "_state_lock", lock):
            t1 = threading.Thread(target=build, args=([dict(a)],), name="first")
            t1.start()
            time.sleep(0.2)
            t2 = threading.Thread(target=lambda: (build([dict(a), dict(b)]), second_done.set()), name="second")
            t2.start()
            t1.join(10)
            t2.join(10)
        third = [dict(a), dict(b)]
        mark(self.home, third, now - 14 * DAY, False)
        self.assertEqual([i["id"] for i in third if i.get("backfilled")], ["chat:work:a", "chat:work:b"])


class SearchCreatesNothing(BackfillCase):
    def test_searching_with_aliased_databases_creates_no_state_folder(self):
        """R5-needs-018 (class D, a read path that creates): the alias tie-break read the summary
        store through a path resolver that makes the plugin data folder."""
        seed(self.home, "work", [("s", 0.2)])
        (self.home / "profiles" / "alias").mkdir(parents=True)
        os.link(self.home / "profiles" / "work" / "state.db", self.home / "profiles" / "alias" / "state.db")
        os.link(self.home / "profiles" / "work" / "state.db", self.home / "state.db")
        self.assertFalse((self.home / "plugin-data").exists())
        needs.search_text("merge")
        self.assertFalse((self.home / "plugin-data").exists(), "search created state")


class NoAllClearOverUnreadChats(BackfillCase):
    def test_the_pocket_pack_is_not_complete_when_chat_sessions_were_not_read(self):
        """R5-needs-009 (class E): a known backlog of unread chat sessions is not a complete pack."""
        omitted = {"items": [], "errors": [], "coverage": {"chat": {"sessions_omitted": 101}}}
        self.assertFalse(needs.pocket_pack(omitted)["complete"])
        whole = {"items": [], "errors": [], "coverage": {"chat": {"sessions_omitted": 0}}}
        self.assertTrue(needs.pocket_pack(whole)["complete"])

    def test_the_page_subtitle_does_not_say_nothing_is_blocked_over_unread_chats(self):
        """R5-desk-011 (class G): the subtitle and the banner must agree."""
        body = r"""
const ctx = {rest: async () => ({})}, refresh = async () => {};
env.useValue = key => key === 'queue' ? q : false;
function render(q0) { globalThis.q = q0; state = []; cursor = 0; return Page({ctx, refresh}); }
const base = {items:[], count:0, counts:{asks:0,stuck:0,answered:0}, errors:[], at:Date.now()};
let page = text(render({...base, coverage: {chat: {sessions_omitted: 1}}}));
assert.ok(page.includes('NOT ALL CLEAR'), 'banner');
assert.ok(!page.includes('Nothing is blocked on you'), 'the subtitle claims an all-clear: ' + page);
page = text(render({...base, coverage: {chat: {sessions_omitted: 0}}}));
assert.ok(page.includes('Nothing is blocked on you'), 'a complete read keeps the plain subtitle');
"""
        for name, code, err in run_views(PRELUDE + body):
            with self.subTest(file=name):
                self.assertEqual(code, 0, err)


class BackfillDocs(HomeCase):
    def test_the_guides_state_what_the_marks_do_after_narrowing_and_past_the_cap(self):
        """R5-needs-005/006/007 (class I): the text names the behavior the tests above pin."""
        root = Path(__file__).resolve().parents[1]
        guide = " ".join((root / "docs" / "operator-guide.md").read_text().split())
        self.assertIn("Narrowing the window again keeps the flag", guide)
        self.assertIn("rewritten under the shared state lock", guide)
        self.assertIn("everything as old as the newest dropped stays flagged", guide)
        arch = " ".join((root / "docs" / "architecture.md").read_text().split())
        self.assertIn("filed and chat-backfill state", arch)
