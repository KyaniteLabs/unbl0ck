"""Shared fixtures for the round-4 chunk-c regression tests (chats, waits, containment, filed items)."""

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_outside import needs, plugin  # noqa: F401  (one shared module instance)

ASK = "Should I merge it now?"
RID = "0123456789abcdef"


def chat_db(path: Path, sessions=None):
    """A state.db with the columns Hermes writes. *sessions*: (id, source, user_id, content, chat_type)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE sessions(id TEXT, title TEXT, source TEXT, chat_type TEXT, started_at REAL,"
        " last_activity_at REAL, user_id TEXT, display_name TEXT)"
    )
    db.execute(
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,"
        " timestamp REAL, display_kind TEXT, active INT)"
    )
    now = time.time()
    for sid, source, user_id, content, chat_type in sessions or [("s1", "cli", "u", ASK, "dm")]:
        db.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)",
            (sid, "", source, chat_type, now - 60, now - 30, user_id, "n"),
        )
        db.execute(
            "INSERT INTO messages(session_id, role, content, timestamp) VALUES(?, 'assistant', ?, ?)",
            (sid, content, now - 30),
        )
    db.commit()
    db.close()


class HomeCase(unittest.TestCase):
    settings: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.settings = {"principal_name": "Ada"}
        for name, value in (
            ("hermes_home", lambda: self.home),
            ("_settings", lambda: self.settings),
        ):
            p = patch.object(needs, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {"UNBL0CK_OUTSIDE_DIRS": ""})
        p.start()
        self.addCleanup(p.stop)
