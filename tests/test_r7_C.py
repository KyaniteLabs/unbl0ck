"""Round-7 lane C regression tests: the chat database open and the chat version budget."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
