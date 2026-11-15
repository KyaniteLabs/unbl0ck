"""The history window, what is not covered, and the bounded backfill setting (issue #40)."""

import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from test_needs import needs  # noqa: E402

DAY = 86400


def settings(**kw):
    return lambda: {"principal_name": "Ada", **kw}


def make_chats(tmp, rows):
    """rows: (session id, platform, chat_type, user_id, age_days, text)."""
    prof = tmp / "profiles" / "work"
    prof.mkdir(parents=True)
    db = sqlite3.connect(prof / "state.db")
    db.executescript("""
      CREATE TABLE sessions(id TEXT, title TEXT, archived INT, source TEXT, chat_type TEXT,
                            user_id TEXT, display_name TEXT, started_at REAL, last_activity_at REAL);
      CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                            timestamp REAL, display_kind TEXT, active INT);
    """)
    now = time.time()
    for i, (sid, platform, ctype, uid, age, text) in enumerate(rows, 1):
        ts = now - age * DAY
        db.execute("INSERT INTO sessions VALUES(?,?,0,?,?,?,?,?,?)", (sid, "t", platform, ctype, uid, "Pat", ts, ts))
        db.execute("INSERT INTO messages VALUES(?,?,?,?,?,NULL,1)", (i, sid, "assistant", text, ts))
    db.commit()
    db.close()
    return tmp


ASK = "Should I open the PR now?"


class Window(unittest.TestCase):
    def test_default_is_14_days_and_the_setting_is_clamped(self):
        for raw, want in [(None, 14), (30, 30), ("45", 45), (0, 14), (-3, 14), (500, 14), (90, 90), (91, 14), ("soon", 14), (True, 14), (7.5, 14)]:
            with self.subTest(raw=raw), patch.object(needs, "_settings", settings(**({} if raw is None else {"chat_window_days": raw}))):
                self.assertEqual(needs.chat_window_days(), want)

    def test_coverage_reports_the_real_window_and_what_was_skipped(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()):
            home = make_chats(Path(d), [("a", "cli", "dm", "u1", 1, ASK)])
            cov = {}
            items = needs.chat_items(home, coverage=cov)
            self.assertEqual(len(items), 1)
            self.assertEqual(cov["window_days"], 14)
            self.assertAlmostEqual(cov["since"], time.time() - 14 * DAY, delta=60)
            self.assertEqual(cov["sessions_checked"], 1)
            self.assertEqual(cov["sessions_omitted"], 0)
            self.assertEqual(cov["profiles"], 1)

    def test_group_chats_that_never_name_you_are_counted_not_hidden_silently(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()):
            home = make_chats(Path(d), [
                ("g1", "discord", "group", "u1", 1, "Team, which region should you pick first? Should I start now?"),
                ("g2", "discord", "group", "u1", 1, "Ada, should I start now?"),
            ])
            cov = {}
            items = needs.chat_items(home, coverage=cov)
            self.assertEqual([i["session_id"] for i in items], ["g2"])
            self.assertEqual(cov["skipped_group_unnamed"], 1)

    def test_chats_of_other_people_are_counted_when_owner_ids_are_set(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings(owner_user_ids="me")):
            home = make_chats(Path(d), [("a", "telegram", "dm", "me", 1, ASK), ("b", "telegram", "dm", "other", 1, ASK)])
            cov = {}
            items = needs.chat_items(home, coverage=cov)
            self.assertEqual([i["session_id"] for i in items], ["a"])
            self.assertEqual(cov["skipped_other_owner"], 1)


class Backfill(unittest.TestCase):
    def test_older_ask_appears_only_after_the_window_is_widened(self):
        with tempfile.TemporaryDirectory() as d:
            home = make_chats(Path(d), [("old", "cli", "dm", "u", 20, ASK), ("new", "cli", "dm", "u", 1, ASK)])
            with patch.object(needs, "_settings", settings()):
                self.assertEqual([i["session_id"] for i in needs.chat_items(home)], ["new"])
            with patch.object(needs, "_settings", settings(chat_window_days=30)):
                cov = {}
                got = needs.chat_items(home, coverage=cov)
                self.assertEqual(sorted(i["session_id"] for i in got), ["new", "old"])
                self.assertEqual(cov["window_days"], 30)

    def test_backfill_adds_no_duplicates_and_keeps_identity(self):
        with tempfile.TemporaryDirectory() as d:
            home = make_chats(Path(d), [("a", "cli", "dm", "u", 1, ASK), ("b", "cli", "dm", "u", 20, ASK)])
            with patch.object(needs, "_settings", settings()):
                narrow = needs.chat_items(home)
            with patch.object(needs, "_settings", settings(chat_window_days=60)):
                wide = needs.chat_items(home)
            ids = [i["id"] for i in wide]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual({(i["id"], i["version"]) for i in narrow}, {(i["id"], i["version"]) for i in wide if i["session_id"] == "a"})

    def test_widened_window_keeps_hidden_items_hidden(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings(chat_window_days=60)), \
                patch.object(needs, "kanban_items", lambda boards=None, errors=None: []):
            home = make_chats(Path(d), [("old", "cli", "dm", "u", 20, ASK)])
            q = needs.build_queue(home)
            self.assertEqual(q["count"], 1)
            needs.hide(q["items"][0]["id"], q["items"][0]["version"], home)
            self.assertEqual(needs.build_queue(home)["count"], 0)

    def test_session_scan_is_bounded_and_the_rest_is_reported(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()), patch.object(needs, "CHAT_SESSION_CAP", 3):
            home = make_chats(Path(d), [(f"s{i}", "cli", "dm", "u", 1 + i / 100, ASK) for i in range(8)])
            cov = {}
            items = needs.chat_items(home, coverage=cov)
            self.assertEqual(len(items), 3)
            self.assertEqual((cov["sessions_checked"], cov["sessions_omitted"]), (3, 5))
            # the newest sessions are the ones kept
            self.assertEqual(sorted(i["session_id"] for i in items), ["s0", "s1", "s2"])

    def test_backfill_is_read_only(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings(chat_window_days=90)):
            home = make_chats(Path(d), [("a", "cli", "dm", "u", 40, ASK)])
            db = home / "profiles" / "work" / "state.db"
            before = db.read_bytes()
            needs.chat_items(home)
            self.assertEqual(db.read_bytes(), before)


class QueuePayload(unittest.TestCase):
    def setUp(self):
        self._k = needs.kanban_items
        needs.kanban_items = lambda boards=None, errors=None: []
        self.addCleanup(setattr, needs, "kanban_items", self._k)

    def test_empty_queue_still_says_what_it_looked_at(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()):
            q = needs.build_queue(make_chats(Path(d), [("a", "cli", "dm", "u", 1, "All done.")]))
            self.assertEqual(q["count"], 0)
            cov = q["coverage"]
            self.assertEqual(cov["chat"]["window_days"], 14)
            joined = " ".join(cov["not_covered"]).lower()
            for phrase in ("14 days", "group", "english"):
                self.assertIn(phrase, joined)

    def test_coverage_is_counts_and_fixed_sentences_only(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()):
            q = needs.build_queue(make_chats(Path(d), [("a", "discord", "group", "u", 1, "SECRET TEAM TEXT: should we go? you decide")]))
            blob = repr(q["coverage"])
            self.assertNotIn("SECRET", blob)
            self.assertNotIn("work", blob.replace("sessions", ""))  # no profile names either

    def test_failed_chat_source_is_not_reported_as_covered(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()):
            with patch.object(needs, "chat_items", side_effect=RuntimeError("boom")):
                q = needs.build_queue(Path(d))
            self.assertIsNone(q["coverage"]["chat"])
            self.assertTrue(any(e.startswith("chat:") for e in q["errors"]))


class ReviewRound3(unittest.TestCase):
    """Round-3 review of #40: coverage must not claim more than was read or summarized."""

    def setUp(self):
        self._k = needs.kanban_items
        needs.kanban_items = lambda boards=None, errors=None: []
        self.addCleanup(setattr, needs, "kanban_items", self._k)

    def test_full_text_search_reports_chat_sessions_the_scan_cap_left_unread(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()), \
                patch.object(needs, "CHAT_SESSION_CAP", 3), patch.object(needs, "hermes_home", lambda: Path(d)):
            make_chats(Path(d), [(f"s{i}", "cli", "dm", "u", 1 + i / 100, "Should I open the PR now?") for i in range(2)]
                       + [(f"o{i}", "cli", "dm", "u", 2 + i / 100, "Should I ship the zebra release?") for i in range(6)])
            r = needs.search_text("zebra")
            self.assertGreater(r["chat_sessions_unread"], 0)
            self.assertTrue(r["partial"])

    def test_full_text_search_is_not_partial_when_every_session_was_read(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()), \
                patch.object(needs, "hermes_home", lambda: Path(d)):
            make_chats(Path(d), [("a", "cli", "dm", "u", 1, "Should I ship the zebra release?")])
            r = needs.search_text("zebra")
            self.assertEqual((r["chat_sessions_unread"], r["partial"], len(r["matches"])), (0, False, 1))

    def test_a_corrupt_chat_database_is_not_reported_as_a_covered_window(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()):
            home = make_chats(Path(d), [("a", "cli", "dm", "u", 1, ASK)])
            (home / "profiles" / "work" / "state.db").write_bytes(b"not a database" * 100)
            q = needs.build_queue(home)
            cov = q["coverage"]
            self.assertTrue(cov["chat"]["failed"])
            self.assertTrue(any("could not be read" in n for n in cov["not_covered"]))

    def test_a_healthy_read_is_not_marked_failed(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()):
            q = needs.build_queue(make_chats(Path(d), [("a", "cli", "dm", "u", 1, ASK)]))
            self.assertFalse(q["coverage"]["chat"]["failed"])

    def test_chats_older_than_the_default_window_are_flagged_backfilled(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings(chat_window_days=60)):
            home = make_chats(Path(d), [("old", "cli", "dm", "u", 20, ASK), ("new", "cli", "dm", "u", 1, ASK)])
            got = {i["session_id"]: i for i in needs.chat_items(home)}
            self.assertTrue(got["old"].get("backfilled"))
            self.assertFalse(got["new"].get("backfilled"))

    def test_backfilled_chats_are_never_sent_to_a_model_automatically(self):
        import test_bluf

        api = test_bluf.BlufTest().load_api_without_fastapi()
        sent = []
        base = {"source": "chat", "tier": "ask", "version": "1", "title": "t", "text": "x"}
        q = {"bluf_enabled": True, "items": [dict(base, id="old", backfilled=True), dict(base, id="new")]}
        n = types.SimpleNamespace(build_queue=lambda: q)
        with patch.object(api, "_needs", lambda: n), patch.object(api, "_loaded_ctx", lambda: types.SimpleNamespace(llm=object())), \
                patch.object(api, "_queue_bluf", lambda n_, ctx, it: sent.append(it["id"])):
            try:
                api.queue()
            except Exception:  # noqa: BLE001 - only the summary decision is under test
                pass
        self.assertEqual(sent, ["new"])


if __name__ == "__main__":
    unittest.main()
