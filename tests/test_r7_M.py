"""Round-7 lane M: where a comment falls against the block, and the comment signature."""

import sqlite3
import unittest
from unittest.mock import patch

from test_r4_stack01d_kanban import HERMES_ERROR, KanbanAnswerCase, kanban_db, kanban_db_connect, needs


def comments_table(body):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE task_comments(id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INT)")
    conn.execute("INSERT INTO task_comments(task_id, author, body, created_at) VALUES('t', 'w', ?, 1)", (body,))
    return conn


def set_body(conn, body):
    conn.execute("UPDATE task_comments SET body = ? WHERE task_id = 't'", (body,))


@unittest.skipIf(HERMES_ERROR, f"Hermes unavailable: {HERMES_ERROR}")
class Placement(KanbanAnswerCase):
    def block(self, task):
        with self.conn() as c:
            return needs._latest_block(c, kanban_db, task)

    def test_a_later_unlogged_comment_does_not_hide_the_logged_answer_from_release(self):
        """R7-006: releasing with the posted answer's text does not post it a second time."""
        task = self.card()
        block = self.block(task)
        with self.conn() as c:
            kanban_db.add_comment(c, task, "operator", "Approved")
            c.execute("UPDATE task_comments SET created_at = ? WHERE task_id = ?", (int(block.created_at) - 10, task))
            c.execute(
                "INSERT INTO task_comments(task_id, author, body, created_at) VALUES(?,?,?,?)",
                (task, "operator", "Specified \u2014 updated title and promoted to todo.", int(block.created_at) - 20),
            )
            c.commit()
        self.answer(task, "Approved", release=True, version=str(block.id))
        self.assertEqual(self.bodies(task).count("Approved"), 1)

    def test_an_edited_answer_in_the_blocks_second_still_answers_it(self):
        """R7-007: an answer edited after it was logged still counts for the block."""
        task = self.card()
        block = self.block(task)
        with self.conn() as c:
            kanban_db.add_comment(c, task, "operator", "A")
            c.execute(
                "UPDATE task_comments SET body = 'Approved', created_at = ? WHERE task_id = ?",
                (int(block.created_at), task),
            )
            c.commit()
        (item,) = needs.kanban_items(boards=["default"])
        self.assertEqual(item["tier"], "answered")

    def test_the_signature_moves_whenever_the_ask_view_of_the_comments_does(self):
        """R7-010: text that redaction brings into view is covered by the signature."""
        task = self.card()
        with self.conn() as c:
            kanban_db.add_comment(c, task, "worker", "first")

        def seen(body):
            with self.conn() as c:
                c.execute("UPDATE task_comments SET body = ? WHERE task_id = ?", (body, task))
                c.commit()
            (item,) = needs.kanban_items(boards=["default"])
            view = needs.build_ask_messages(needs.card_context("default", task), [])[1]["content"]
            return item["comments_sig"], view

        for prefix in ("password=" + "a" * 30000, "password=a"):
            (sig_a, view_a), (sig_b, view_b) = seen(prefix + "\nDecision A"), seen(prefix + "\nDecision B")
            self.assertEqual(view_a == view_b, sig_a == sig_b, prefix[:12])
        self.assertNotEqual(view_a, view_b)

    def test_a_queue_poll_does_not_read_old_comment_bodies(self):
        """R7-008: 200 large comments from before the block are not read on a queue poll."""
        task = self.card()
        block = self.block(task)
        with self.conn() as c:
            c.executemany(
                "INSERT INTO task_comments(task_id, author, body, created_at) VALUES(?,?,?,?)",
                [(task, "worker", "x" * 100_000, int(block.created_at) - 100) for _ in range(200)],
            )
            c.commit()
        read = [0]
        real = kanban_db_connect.connect

        def measure(cursor, row):
            read[0] += sum(len(v) for v in row if isinstance(v, (str, bytes)))
            return sqlite3.Row(cursor, row)

        def connect(*a, **kw):
            conn = real(*a, **kw)
            conn.row_factory = measure
            return conn

        with patch.object(kanban_db_connect, "connect", connect):
            (item,) = needs.kanban_items(boards=["default"])
        self.assertEqual(item["tier"], "ask")
        self.assertLess(read[0], 2_000_000)


class Signature(unittest.TestCase):
    def test_the_signature_sees_past_an_embedded_nul(self):
        """R7-009: a change after a NUL byte changes the signature."""
        conn = comments_table("prefix\0A")
        before = needs._comments_sig(conn, None, "t")
        set_body(conn, "prefix\0B")
        self.assertNotEqual(needs._comments_sig(conn, None, "t"), before)


if __name__ == "__main__":
    unittest.main()
