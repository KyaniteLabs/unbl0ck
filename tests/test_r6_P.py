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
            versions.append(needs._chat_version(conn, 7, len(text), tail, True))
        self.assertNotEqual(versions[0], versions[1])


if __name__ == "__main__":
    unittest.main()
