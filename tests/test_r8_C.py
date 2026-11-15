"""Round-8 lane C regression tests: the chat database open and cut-message versions."""

import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_ask_coverage import ASK, make_chats, settings
from test_outside import needs
from test_r6_P import _chat_db


class ChatDatabaseOpen(unittest.TestCase):
    def test_a_database_renamed_while_sqlite_opens_it_is_refused(self):
        """R8-001: a single rename before SQLite resolves the pinned descriptor's name leaves it on
        the renamed file (and its WAL), not on state.db, so the open is refused."""
        with tempfile.TemporaryDirectory() as d:
            a, aside = Path(d) / "state.db", Path(d) / "aside.db"
            _chat_db(a, "pinned")
            real = sqlite3.connect

            def renaming(*args, **kwargs):
                os.rename(a, aside)
                return real(*args, **kwargs)

            with patch.object(sqlite3, "connect", renaming):
                with self.assertRaises(needs.UnsafeStateFile):
                    needs._open_chat_db(a).close()


def _big_asks(d, n):
    big = "x " * (1 << 20)  # 2 MiB each, so every one is cut
    return make_chats(Path(d), [(f"s{i}", "cli", "dm", "u", 1, big + f"{i} " + ASK) for i in range(n)])


class OverBudgetVersion(unittest.TestCase):
    def test_an_unchanged_ask_past_the_hash_budget_keeps_its_version_and_can_be_dismissed(self):
        """R8-007: past the poll's hash budget a cut ask keeps one version across polls, so Not for
        me applies to it, and an edit to its end still changes the version."""
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()), patch.object(
            needs, "CHAT_HASH_BYTES", 1 << 20
        ):
            home = _big_asks(d, 2)
            first = {i["session_id"]: i["version"] for i in needs.chat_items(home)}
            again = {i["session_id"]: i["version"] for i in needs.chat_items(home)}
            self.assertEqual(first, again)
            needs.hide("chat:work:s1", first["s1"], home)  # raised StaleHide with a random version
            shown = {i["id"] for i in needs.build_queue(home, record=False)["items"]}
            self.assertNotIn("chat:work:s1", shown)
            self.assertIn("chat:work:s0", shown)
            db = sqlite3.connect(Path(home) / "profiles" / "work" / "state.db")
            db.execute("UPDATE messages SET content = content || ' Or wait?' WHERE session_id='s1'")
            db.commit()
            db.close()
            edited = {i["session_id"]: i["version"] for i in needs.chat_items(home)}
            self.assertNotEqual(edited["s1"], first["s1"])


class HashAccounting(unittest.TestCase):
    def test_every_byte_hashed_for_a_cut_message_is_charged_to_the_budget(self):
        """R8-008: the retained end hashed after the whole message counts against the budget too,
        both when the message is hashed whole and when the budget has run out."""
        tail = "y" * 100_000
        body = "a" * 100_001 + tail
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, content TEXT)")
        conn.execute("INSERT INTO messages VALUES(7, ?)", (body,))
        real = hashlib.sha256
        for start in (needs.CHAT_HASH_BYTES, 10):
            with self.subTest(budget=start):
                hashed = [0]

                class Counting:
                    def __init__(self, data=b""):
                        self._h = real(data)

                    def update(self, data):
                        hashed[0] += len(data)
                        self._h.update(data)

                    def hexdigest(self):
                        return self._h.hexdigest()

                budget = [start]
                with patch.object(hashlib, "sha256", Counting):
                    needs._chat_version(conn, 7, len(body), tail, True, budget)
                self.assertGreater(hashed[0], 0)
                self.assertEqual(start - budget[0], hashed[0])


if __name__ == "__main__":
    unittest.main()
