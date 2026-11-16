"""#73: a targeted read accepts the version an over-budget queue build gave a cut message, and
every byte a version hashes is charged to the poll's budget."""

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_ask_coverage import ASK, make_chats, settings
from test_outside import needs


class CutVersions(unittest.TestCase):
    def test_a_targeted_read_accepts_the_version_an_over_budget_queue_gave(self):
        big = "x " * (1 << 20) + ASK  # 2 MiB: cut, and past the patched budget below
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()):
            home = make_chats(Path(d), [("s0", "cli", "dm", "u", 1, big)])
            with patch.object(needs, "CHAT_HASH_BYTES", 1 << 20):
                (item,) = needs.chat_items(home)
            with patch.object(needs, "hermes_home", lambda: Path(home)):
                got = needs.source_text(item["id"], item["version"])
            self.assertEqual(got["version"], item["version"])

    def test_every_hashed_byte_including_the_length_header_is_charged(self):
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
                        hashed[0] += len(data)
                        self._h = real(data)

                    def update(self, data):
                        hashed[0] += len(data)
                        self._h.update(data)

                    def hexdigest(self):
                        return self._h.hexdigest()

                budget = [start]
                with patch.object(hashlib, "sha256", Counting):
                    needs._chat_version(conn, 7, len(body), tail, True, budget)
                self.assertEqual(start - budget[0], hashed[0])


if __name__ == "__main__":
    unittest.main()
