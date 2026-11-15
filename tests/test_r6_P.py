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


if __name__ == "__main__":
    unittest.main()
