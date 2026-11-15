"""Round-8 lane Q: the bounded comment and chat reads, where an answer falls, and the comment signature."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_ask_coverage import ASK, make_chats
from test_r4_stack01d_kanban import HERMES_ERROR, KanbanAnswerCase, kanban_db, needs
from test_r7_M import comments_table


BIG = 24 << 20  # one stored text; SQLite may hold only half of it at once in the child below


def under_heap_limit(call):
    """Source for a child process that prints *call* (an expression using ``needs`` and ``home``)
    as JSON while SQLite may allocate at most BIG // 2 bytes (a limit no process can raise again)."""
    return (
        "import json, sqlite3, sys; from pathlib import Path; from unittest.mock import patch\n"
        f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        "from test_outside import needs\n"
        "home = Path(sys.argv[1])\n"
        f"sqlite3.connect(':memory:').execute('PRAGMA hard_heap_limit={BIG // 2}')\n"
        "with patch.object(needs, '_settings', lambda: {'principal_name': 'Ada'}):\n"
        f"    print(json.dumps({call}))\n"
    )


class CutComments(unittest.TestCase):
    def test_a_credential_cut_from_its_delimiter_is_not_sent_to_the_model(self):
        """R8-003: a cut comment never leaves the readable part of a secret the full text would redact."""
        for body in (
            "https://user:PARTSECRET" + "a" * 24000 + "@example.test",
            'curl -u "alice:PARTSECRET' + "b" * 24000 + '" https://example.test',
        ):
            with self.subTest(body[:12]):
                self.assertNotIn("PARTSECRET", needs._ask_redact(body))
                (row,), _ = needs._newest_comments(comments_table(body), None, "t", 5)
                card = {"title": "t", "comments": [{"author": row[1], "body": row[2]}]}
                sent = json.dumps(needs.build_ask_messages(card, []))
                self.assertNotIn("PARTSECRET", sent)
                self.assertIn("truncated", row[2])


class Signature(unittest.TestCase):
    def test_the_signature_moves_when_a_comment_date_does(self):
        """R8-013: every field of the shared comment view, created_at included, is in the signature."""
        conn = comments_table("same text")
        before = needs._comments_sig(conn, None, "t")
        conn.execute("UPDATE task_comments SET created_at = 2")
        self.assertNotEqual(needs._comments_sig(conn, None, "t"), before)


class BoundedTextReads(unittest.TestCase):
    def run_child(self, home, call):
        done = subprocess.run(
            [sys.executable, "-c", under_heap_limit(call), str(home)], capture_output=True, text=True, timeout=120
        )
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_a_huge_comment_is_read_without_sqlite_loading_it(self):
        """R8-009: the comment view reads a bounded piece of a body, not SQLite's whole value."""
        with tempfile.TemporaryDirectory() as d:
            conn = sqlite3.connect(Path(d) / "k.db")
            conn.execute(
                "CREATE TABLE task_comments(id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INT)"
            )
            conn.execute("INSERT INTO task_comments VALUES(1, 't', 'w', ?, 1)", ("Pick A or B? " * (BIG // 13),))
            conn.commit()
            conn.close()
            rows, count = self.run_child(
                d, "needs._newest_comments(sqlite3.connect(home / 'k.db'), None, 't', 5)"
            )
        self.assertEqual(count, 1)
        self.assertTrue(rows[0][2].startswith("Pick A or B?"))

    def test_a_huge_chat_message_is_read_without_sqlite_loading_it(self):
        """R8-009: the chat read neither slices nor trims SQLite's whole value of a long message."""
        with tempfile.TemporaryDirectory() as d:
            home = make_chats(Path(d), [("big", "cli", "dm", "u", 1, "x " * (BIG // 2) + ASK)])
            found = self.run_child(home, "(lambda e: [[i['session_id'] for i in needs.chat_items(home, errors=e)], e])([])")
        self.assertEqual(found, [["big"], []])


@unittest.skipIf(HERMES_ERROR, f"Hermes unavailable: {HERMES_ERROR}")
class Placement(KanbanAnswerCase):
    def test_an_unlogged_comment_from_before_the_block_is_not_this_blocks_answer(self):
        """R8-004: an answer is a comment whose own logged event follows the block; comments are not
        counted against events, so an unlogged earlier comment is never taken for the answer."""
        with self.conn() as c:
            task = kanban_db.create_task(c, title="Choose", body="", assignee="worker")
            task = getattr(task, "id", task)
            kanban_db.add_comment(c, task, "operator", "Old")
            c.execute(
                "INSERT INTO task_comments(task_id, author, body, created_at) VALUES(?, 'operator', 'No', 1)", (task,)
            )
            c.commit()
            kanban_db.block_task(c, task, reason="Needs CEO approval", kind="needs_input")
            block = needs._latest_block(c, kanban_db, task)
            kanban_db.add_comment(c, task, "operator", "Approved")
            self.assertFalse(needs._answered_with(c, kanban_db, task, "operator", block.id, "No"))
            kanban_db.add_comment(c, task, "operator", "Ok")  # logged with the same length as "No"
            self.assertFalse(needs._answered_with(c, kanban_db, task, "operator", block.id, "No"))
            self.assertTrue(needs._answered_with(c, kanban_db, task, "operator", block.id, "Ok"))
            self.assertTrue(needs._answered_with(c, kanban_db, task, "operator", block.id, "Approved"))

    def test_a_queue_poll_does_not_read_the_comment_event_history(self):
        """R8-005: commented events from before the block, with large payloads, are not read."""
        with self.conn() as c:
            task = kanban_db.create_task(c, title="Choose", body="", assignee="worker")
            task = getattr(task, "id", task)
            c.executemany(
                "INSERT INTO task_events(task_id, kind, payload, created_at) VALUES(?, 'commented', ?, 1)",
                [(task, json.dumps({"author": "w" * 100_000, "len": 1})) for _ in range(200)],
            )
            c.commit()
            kanban_db.block_task(c, task, reason="Needs CEO approval", kind="needs_input")
            block = needs._latest_block(c, kanban_db, task)
            read = [0]

            def measure(cursor, row):
                read[0] += sum(len(v) for v in row if isinstance(v, (str, bytes)))
                return tuple(row)

            c.row_factory = measure
            self.assertFalse(needs._answered_since(c, kanban_db, task, "operator", block.id))
        self.assertLess(read[0], 1_000_000)


if __name__ == "__main__":
    unittest.main()
