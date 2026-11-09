"""Regression tests for undo, priority order and comment-aware summaries."""

import importlib.util
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ub_needs_polish", ROOT / "needs.py")
needs = importlib.util.module_from_spec(spec)
sys.modules["ub_needs_polish"] = needs
spec.loader.exec_module(needs)


class Unhide(unittest.TestCase):
    def test_unhide_restores_only_that_item(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            needs.hide("a", "1", home)
            needs.hide("b", "1", home)
            self.assertTrue(needs.unhide("a", home))
            self.assertEqual(needs.load_hidden(home), {"b": "1"})
            self.assertFalse(needs.unhide("a", home))  # second undo is a no-op
            self.assertFalse(needs.unhide("never", home))


class QueueOrder(unittest.TestCase):
    def test_priority_first_then_newest(self):
        items = [
            {"id": "old-urgent", "version": "1", "priority": 3, "since": 10},
            {"id": "new-normal", "version": "1", "priority": 0, "since": 99},
            {"id": "new-urgent", "version": "1", "priority": 3, "since": 50},
            {"id": "chat", "version": "1", "since": 70},  # chats carry no priority
        ]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(needs, "kanban_items", lambda errors: [dict(i) for i in items]), \
                mock.patch.object(needs, "chat_items", lambda home, errors: []):
            q = needs.build_queue(Path(d))
        self.assertEqual([i["id"] for i in q["items"]], ["new-urgent", "old-urgent", "new-normal", "chat"])


class CommentAwareBluf(unittest.TestCase):
    def test_comments_are_sent_capped_and_trimmed(self):
        comments = [{"author": f"u{i}", "body": "x" * 900} for i in range(9)]
        msgs = needs.bluf_messages({"title": "T", "ask": "A"}, comments)
        card = json.loads(msgs[1]["content"].split("```json\n", 1)[1].rsplit("\n```", 1)[0])
        self.assertEqual([c["author"] for c in card["recent_comments"]], [f"u{i}" for i in range(3, 9)])
        self.assertTrue(all(len(c["text"]) == 600 for c in card["recent_comments"]))
        self.assertIn("recent_comments", msgs[0]["content"])

    def test_no_comments_no_key(self):
        msgs = needs.bluf_messages({"title": "T", "ask": "A"}, [])
        self.assertNotIn("recent_comments", msgs[1]["content"])

    def test_recent_comments_non_kanban_is_empty(self):
        self.assertEqual(needs.recent_comments({"source": "chat", "id": "c"}), [])

    def _fake_hermes(self, conn, listed=()):
        kdb = types.SimpleNamespace(list_comments=lambda c, t: list(listed))
        kbc = types.SimpleNamespace(connect=lambda board: conn)
        pkg = types.ModuleType("hermes_cli")
        pkg.kanban_db, pkg.kanban_db_connect = kdb, kbc
        return mock.patch.dict(sys.modules, {"hermes_cli": pkg, "hermes_cli.kanban_db": kdb,
                                             "hermes_cli.kanban_db_connect": kbc})

    def test_recent_comments_newest_last_limited(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE task_comments(id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INT)")
        conn.executemany("INSERT INTO task_comments(task_id, author, body, created_at) VALUES(?,?,?,?)",
                         [("t1", f"a{i}", f"b{i}", i) for i in range(10)] + [("t2", "z", "other", 99)])
        item = {"source": "kanban", "board": "b", "task_id": "t1"}
        with self._fake_hermes(conn):
            got = needs.recent_comments(item, limit=3)
        self.assertEqual(got, [{"author": f"a{i}", "body": f"b{i}"} for i in (7, 8, 9)])

    def test_recent_comments_falls_back_on_schema_drift(self):
        class Broken:
            def execute(self, *a):
                raise sqlite3.OperationalError("no such table: task_comments")

            def close(self):
                pass

        listed = [types.SimpleNamespace(author=f"a{i}", body=f"b{i}") for i in range(8)]
        with self._fake_hermes(Broken(), listed):
            got = needs.recent_comments({"source": "kanban", "board": "b", "task_id": "t1"}, limit=2)
        self.assertEqual(got, [{"author": "a6", "body": "b6"}, {"author": "a7", "body": "b7"}])


if __name__ == "__main__":
    unittest.main()
