"""Round-8 lane C regression tests: the chat database open and cut-message versions."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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



if __name__ == "__main__":
    unittest.main()
