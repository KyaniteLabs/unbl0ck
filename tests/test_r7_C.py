"""Round-7 lane C regression tests: the chat database open and the chat version budget."""

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
    def test_sqlite_never_reads_a_file_swapped_in_while_it_opens(self):
        """R7-001: another open descriptor on the pinned file, or descriptors that cannot be
        listed, must not let SQLite's own open land on a swapped-in file."""
        real_connect, real_listdir = sqlite3.connect, os.listdir

        def unlistable(path, *args):
            if str(path) == "/dev/fd":
                raise PermissionError(path)
            return real_listdir(path, *args)

        for case in ("another connection is open", "descriptors cannot be listed"):
            with self.subTest(case), tempfile.TemporaryDirectory() as d:
                a, b, aside = Path(d) / "state.db", Path(d) / "other.db", Path(d) / "aside.db"
                _chat_db(a, "pinned")
                _chat_db(b, "elsewhere")

                def racing(*args, **kwargs):
                    os.rename(a, aside)
                    os.symlink(b, a)
                    try:
                        return real_connect(*args, **kwargs)
                    finally:
                        os.unlink(a)
                        os.rename(aside, a)

                held = needs._open_chat_db(a) if case.startswith("another") else None
                try:
                    with patch.object(sqlite3, "connect", racing), patch.object(os, "listdir", unlistable):
                        try:
                            conn = needs._open_chat_db(a)
                        except needs.UnsafeStateFile:
                            continue  # refused: nothing was read
                    with conn:
                        self.assertEqual(conn.execute("SELECT content FROM messages").fetchone()[0], "pinned")
                    conn.close()
                finally:
                    if held is not None:
                        held.close()


class ChatVersionBudget(unittest.TestCase):
    def test_one_poll_hashes_a_bounded_number_of_bytes_and_unhashed_asks_stay_visible(self):
        """R7-005: cut messages are hashed within a per-poll byte budget, a message that is not
        an ask is not hashed at all, and an ask past the budget is never taken as unchanged."""
        big = "x " * (1 << 20)  # 2 MiB: far past what a poll reads of one message
        rows = [(f"s{i}", "cli", "dm", "u", 1, big + ASK) for i in range(4)]
        rows.append(("plain", "cli", "dm", "u", 1, big + "Done, nothing to ask."))
        hashed = [0]
        real = hashlib.sha256

        class Counting:
            def __init__(self, data=b""):
                self._h = real(data)
                hashed[0] += len(data)

            def update(self, data):
                hashed[0] += len(data)
                self._h.update(data)

            def hexdigest(self):
                return self._h.hexdigest()

        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()), patch.object(
            needs, "CHAT_HASH_BYTES", 3 << 20, create=True
        ):
            home = make_chats(Path(d), rows)
            with patch.object(hashlib, "sha256", Counting):
                first = {i["session_id"]: i["version"] for i in needs.chat_items(home)}
            self.assertEqual(set(first), {"s0", "s1", "s2", "s3"})
            self.assertLessEqual(hashed[0], (3 << 20) + (1 << 20))
            dismissed = set(first.values())
            again = needs.chat_items(home, visible=lambda _id, version: version not in dismissed)
            self.assertGreaterEqual(len(again), 3)  # only the asks that fit the budget can be hidden


if __name__ == "__main__":
    unittest.main()
