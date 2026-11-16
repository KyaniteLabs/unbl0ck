"""#73: the chat source finds the latest turn past any run of blank messages."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_ask_coverage import ASK, make_chats, settings
from test_outside import needs


def _add_blanks(home, sid, blanks):
    db = sqlite3.connect(Path(home) / "profiles" / "work" / "state.db")
    ts = db.execute("SELECT timestamp FROM messages WHERE session_id=?", (sid,)).fetchone()[0]
    for n, text in enumerate(blanks, 1):
        db.execute(
            "INSERT INTO messages(session_id, role, content, timestamp, active) VALUES(?,?,?,?,1)",
            (sid, "user", text, ts + n),
        )
    db.commit()
    db.close()


class BlankTurns(unittest.TestCase):
    def test_an_ask_behind_many_blank_messages_is_found(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()):
            home = make_chats(Path(d), [("s0", "cli", "dm", "u", 1, ASK)])
            _add_blanks(home, "s0", [" "] * 25)
            self.assertEqual([i["session_id"] for i in needs.chat_items(home)], ["s0"])

    def test_a_space_only_message_longer_than_the_read_window_is_blank(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()):
            home = make_chats(Path(d), [("s0", "cli", "dm", "u", 1, ASK)])
            _add_blanks(home, "s0", [" " * (needs.CHAT_READ_BYTES + 10)])
            self.assertEqual([i["session_id"] for i in needs.chat_items(home)], ["s0"])

    def test_a_long_message_with_a_blank_tail_is_still_the_latest_turn(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()):
            home = make_chats(Path(d), [("s0", "cli", "dm", "u", 1, ASK)])
            _add_blanks(home, "s0", ["done." + " " * (needs.CHAT_READ_BYTES + 10)])
            self.assertEqual(needs.chat_items(home), [])

    def test_the_blank_lookback_charges_every_read_and_says_when_it_stopped(self):
        """#73 review: each blank message's first read counts against the poll's byte budget."""
        budget = 10_000
        read = [0]
        real = needs._read_text

        def counting(conn, table, *args, **kwargs):
            raw, length = real(conn, table, *args, **kwargs)
            if table == "messages":
                read[0] += len(raw)
            return raw, length

        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()), patch.object(
            needs, "CHAT_HASH_BYTES", budget
        ), patch.object(needs, "_read_text", counting):
            home = make_chats(Path(d), [("s0", "cli", "dm", "u", 1, ASK)])
            _add_blanks(home, "s0", [" " * 1000] * 50)
            cov, errors = {}, []
            needs.chat_items(home, coverage=cov, errors=errors)
            self.assertLessEqual(read[0], budget + 1000)
            self.assertEqual(cov.get("blank_lookback_cut"), 1)
            self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
