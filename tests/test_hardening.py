"""Regression tests for the latency and silent-failure fixes."""

import importlib.util
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ub_needs_hard", ROOT / "needs.py")
needs = importlib.util.module_from_spec(spec)
sys.modules["ub_needs_hard"] = needs
spec.loader.exec_module(needs)


class FakeKdb:
    """Just enough of kanban_db for the fast paths and their fallbacks."""

    class Event:
        @staticmethod
        def from_row(row):
            return types.SimpleNamespace(
                id=row["id"], kind=row["kind"], created_at=row["created_at"]
            )

    def __init__(self, events=(), comments=()):
        self.events, self.comments, self.calls = list(events), list(comments), []

    def list_events(self, conn, task_id):
        self.calls.append("list_events")
        return self.events

    def list_comments(self, conn, task_id):
        self.calls.append("list_comments")
        return self.comments


def board_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE task_events(id INTEGER PRIMARY KEY, task_id TEXT, run_id TEXT, kind TEXT, payload TEXT, created_at INT);
      CREATE TABLE task_comments(id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INT);
    """)
    rows = [
        ("t1", "created", 1),
        ("t1", "blocked", 5),
        ("t1", "commented", 9),
        ("t1", "block_loop_detected", 7),
        ("t2", "blocked", 50),
    ]
    conn.executemany(
        "INSERT INTO task_events(task_id, kind, created_at) VALUES(?,?,?)", rows
    )
    conn.execute(
        "INSERT INTO task_comments(task_id, author, body, created_at) VALUES('t1','Ada','ok',8)"
    )
    return conn


class BrokenConn:
    def execute(self, *a):
        raise sqlite3.OperationalError("no such table: task_events")


class FastPaths(unittest.TestCase):
    def test_latest_block_reads_one_row(self):
        kdb = FakeKdb()
        ev = needs._latest_block(board_db(), kdb, "t1")
        self.assertEqual((ev.kind, ev.created_at), ("block_loop_detected", 7))
        self.assertEqual(kdb.calls, [])  # no full-history load
        self.assertIsNone(needs._latest_block(board_db(), kdb, "nope"))

    def test_latest_block_falls_back_on_schema_drift(self):
        old = types.SimpleNamespace(kind="blocked", created_at=3)
        kdb = FakeKdb(
            events=[old, types.SimpleNamespace(kind="commented", created_at=4)]
        )
        self.assertIs(needs._latest_block(BrokenConn(), kdb, "t1"), old)
        self.assertEqual(kdb.calls, ["list_events"])

    def test_answered_since_is_case_insensitive_and_time_bound(self):
        kdb = FakeKdb()
        self.assertTrue(needs._answered_since(board_db(), kdb, "t1", "ada", 8))
        self.assertFalse(needs._answered_since(board_db(), kdb, "t1", "ada", 9))
        self.assertFalse(needs._answered_since(board_db(), kdb, "t1", "someone", 0))

    def test_answered_since_falls_back(self):
        kdb = FakeKdb(comments=[types.SimpleNamespace(author="ADA", created_at=10)])
        self.assertTrue(needs._answered_since(BrokenConn(), kdb, "t1", "ada", 9))
        self.assertEqual(kdb.calls, ["list_comments"])


class Settings(unittest.TestCase):
    def test_bluf_enabled_parsing(self):
        self.assertTrue(needs.bluf_enabled({}))
        for off in (False, "false", "OFF", "no", "0", ""):
            self.assertFalse(needs.bluf_enabled({"bluf_enabled": off}), off)
        for on in (True, "true", "yes", 1):
            self.assertTrue(needs.bluf_enabled({"bluf_enabled": on}), on)


class SilentFailures(unittest.TestCase):
    def test_unreadable_chat_db_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            prof = Path(d) / "profiles" / "broken"
            prof.mkdir(parents=True)
            (prof / "state.db").write_bytes(b"this is not sqlite" * 100)
            errors = []
            self.assertEqual(needs.chat_items(Path(d), errors=errors), [])
            self.assertTrue(errors and "broken" in errors[0], errors)

    def test_corrupt_state_file_is_logged_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            p.write_text("{nope")
            with self.assertLogs("unbl0ck", "WARNING"):
                self.assertEqual(needs._read_json(p), {})
            p.write_text("[1, 2]")
            self.assertEqual(needs._read_json(p), {})

    def test_bluf_cache_holds_only_current_queue_members(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            for i in range(8):
                needs.save_bluf(f"i{i}", "1", f"b{i}", "w", home)
            needs.prune_blufs(
                [{"id": f"i{i}", "version": "1"} for i in range(5, 8)], home
            )
            self.assertEqual(sorted(needs.load_blufs(home)), ["i5", "i6", "i7"])

    def test_concurrent_hides_lose_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            ts = [
                threading.Thread(target=needs.hide, args=(f"id{i}", "v", home))
                for i in range(20)
            ]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(len(needs.load_hidden(home)), 20)


if __name__ == "__main__":
    unittest.main()
