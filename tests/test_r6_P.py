"""Round-6 lane P regression tests: one per finding, each driven through an entry point the
round-5 code already had, so it fails there for the behavioural reason."""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_r4_stack11 as s11
from r4c_common import ASK
from test_outside import OutsideFixture, needs
from test_r4_stack01d_kanban import HERMES_ERROR, KanbanAnswerCase, kanban_db


class LegacyBackfill(s11.QueueCase):
    def test_a_chat_an_older_store_listed_as_backfill_stays_backfill(self):
        """R6-001: a remembered id keeps its flag when the floor alone would clear it."""
        from test_ask_coverage import make_chats, settings

        home = make_chats(Path(tempfile.mkdtemp(dir=self.home)), [("three", "cli", "dm", "u", 3, ASK)])
        store = needs.data_dir(home) / needs.BACKFILL_FILE
        store.write_text(json.dumps({"floor": time.time() - 7 * 86400, "ids": ["chat:work:three"]}))
        with patch.object(needs, "_settings", settings(chat_window_days=7)):
            items = {i["session_id"]: i for i in needs.build_queue(home)["items"] if i["source"] == "chat"}
        self.assertTrue(items["three"].get("backfilled"))


def _chat_db(path: Path, text: str) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE sessions(id TEXT, title TEXT);"
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL);"
    )
    db.execute("INSERT INTO messages VALUES(1, 's', 'assistant', ?, 0)", (text,))
    db.commit()
    db.close()


class ChatDatabaseOpen(unittest.TestCase):
    def test_a_path_swapped_only_while_sqlite_opens_it_is_refused(self):
        """R6-003: SQLite's own open must land on the pinned file, not on a link's target."""
        with tempfile.TemporaryDirectory() as d:
            a, b, aside = Path(d) / "state.db", Path(d) / "other.db", Path(d) / "aside.db"
            _chat_db(a, "pinned")
            _chat_db(b, "elsewhere")
            real = sqlite3.connect

            def racing(*args, **kwargs):
                os.rename(a, aside)
                os.symlink(b, a)
                try:
                    return real(*args, **kwargs)
                finally:
                    os.unlink(a)
                    os.rename(aside, a)

            with patch.object(sqlite3, "connect", racing):
                with self.assertRaises(needs.UnsafeStateFile):
                    needs._open_chat_db(a).close()
            conn = needs._open_chat_db(a)  # without the race the same file opens
            try:
                self.assertEqual(conn.execute("SELECT content FROM messages").fetchone()[0], "pinned")
            finally:
                conn.close()


class ChatVersion(unittest.TestCase):
    def test_an_edit_in_the_middle_of_a_huge_message_changes_its_version(self):
        """R6-007: a cut message's version covers every byte, not only the first 16 MiB."""
        tail = "Should I open the PR now?"
        body = "a" * (17 << 20) + tail
        edited = body[: (16 << 20) + 1000] + "b" + body[(16 << 20) + 1001 :]
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, content TEXT)")
        versions = []
        for text in (body, edited):
            conn.execute("INSERT OR REPLACE INTO messages VALUES(7, ?)", (text,))
            versions.append(needs._chat_version(conn, 7, len(text), tail, True, [needs.CHAT_HASH_BYTES]))
        self.assertNotEqual(versions[0], versions[1])


class CommentSignature(unittest.TestCase):
    def test_the_signature_reads_a_bounded_number_of_comment_bytes(self):
        """R6-008: a long comment history is not read in full on every poll."""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE task_comments(id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INT)")
        conn.executemany(
            "INSERT INTO task_comments(task_id, author, body, created_at) VALUES('t', 'w', ?, ?)",
            [("x" * 100_000, i) for i in range(200)],
        )
        read = [0]

        def measure(cursor, row):
            read[0] += sum(len(v) for v in row if isinstance(v, (str, bytes)))
            return row

        conn.row_factory = measure
        before = needs._comments_sig(conn, None, "t")
        self.assertLess(read[0], 2_000_000)
        conn.execute("UPDATE task_comments SET body = 'y' || SUBSTR(body, 2) WHERE id = 200")
        self.assertNotEqual(needs._comments_sig(conn, None, "t"), before)


class DismissalReads(OutsideFixture):
    def test_confirming_a_dismissal_uses_the_read_budget(self):
        """R6-009: with no read budget, no dismissed folder file is opened to confirm it."""
        self.watch()
        f = self.folder / "note.txt"
        f.write_text("Call the supplier back")
        (item,) = needs.outside_items(self.home)
        needs.hide(item["id"], item["version"], self.home)
        opened = []
        real = needs._pinned_open

        def spy(p, *a, **kw):
            opened.append(Path(p))
            return real(p, *a, **kw)

        with patch.object(needs, "_pinned_open", spy):
            needs.outside_items(self.home, read_budget=0, unread=[0])
        self.assertNotIn(f, opened)


@unittest.skipIf(HERMES_ERROR, f"Hermes unavailable: {HERMES_ERROR}")
class CommentPlacement(KanbanAnswerCase):
    def test_a_logged_answer_counts_beside_an_unlogged_comment(self):
        """R6-010: an unlogged comment does not take the place of the logged answer after the block."""
        task = self.card()
        with self.conn() as c:
            block = needs._latest_block(c, kanban_db, task)
            kanban_db.add_comment(c, task, "operator", "Approved")
            c.execute("UPDATE task_comments SET created_at = ? WHERE task_id = ?", (int(block.created_at) - 10, task))
            c.execute(
                "INSERT INTO task_comments(task_id, author, body, created_at) VALUES(?,?,?,?)",
                (task, "someone", "imported note", int(block.created_at) - 20),
            )
            c.commit()
            self.assertTrue(needs._answered_since(c, kanban_db, task, "operator", block.id))
            self.assertTrue(needs._answered_with(c, kanban_db, task, "operator", block.id, "Approved"))


class DismissalOrder(s11.QueueCase):
    def test_a_later_dismissal_in_another_process_gets_a_higher_order(self):
        """R6-019: orders grow across processes, even within one millisecond."""
        frozen = time.time()
        with patch.object(needs.time, "time", lambda: frozen):
            _, first = needs.hide_ordered("kanban:b:t", "1", self.home)
            with patch.object(needs, "_last_order", 0, create=True):  # a fresh process
                _, second = needs.hide_ordered("kanban:b:t", "1", self.home)
        self.assertGreater(second, first)


class DoneOrder(OutsideFixture):
    def test_refiling_identical_content_keeps_the_rank_of_the_done(self):
        """R6-021: an identical refile keeps done_order with the Done and its token."""
        filed = self.file(dedupe_key="reply-1")
        done = needs.done_outside(filed["id"], filed["version"], self.home)
        path = self.record(filed)
        rec = json.loads(path.read_text())
        rec["done_order"] = 4242
        path.write_text(json.dumps(rec))
        self.assertEqual(self.file(dedupe_key="reply-1")["id"], filed["id"])
        again = needs.done_outside(filed["id"], filed["version"], self.home)
        self.assertEqual((again["token"], again["order"]), (done["token"], 4242))


if __name__ == "__main__":
    unittest.main()
