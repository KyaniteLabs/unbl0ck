"""Every human chat platform and pending pairing requests reach the queue; agent traffic does not."""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import stock_pairing

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("unbl0ck_needs_c", ROOT / "needs.py")
needs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(needs)
needs._settings = lambda: {"principal_name": "Ada", "comment_author": "ada"}

ASK = "Want me to merge it?"


def home_with(tmp: Path, sessions):
    """sessions: (id, title, source, chat_type, parent, last_role, last_text)."""
    prof = tmp / "profiles" / "ops"
    prof.mkdir(parents=True)
    db = sqlite3.connect(prof / "state.db")
    db.executescript("""
      CREATE TABLE sessions(id TEXT, title TEXT, source TEXT, chat_type TEXT, parent_session_id TEXT,
                            hidden INT, archived INT, started_at REAL, last_activity_at REAL);
      CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                            timestamp REAL, display_kind TEXT, active INT);""")
    now = time.time()
    for n, (sid, title, src, ct, parent, role, text) in enumerate(sessions):
        db.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,0,0,?,?)",
            (sid, title, src, ct, parent, now - 100, now - 50),
        )
        db.execute(
            "INSERT INTO messages(session_id,role,content,timestamp,active) VALUES(?,?,?,?,1)",
            (sid, role, text, now - 50 + n * 0.01),
        )
    db.commit()
    db.close()
    return tmp


class AllPlatformChats(unittest.TestCase):
    def ids(self, rows):
        with tempfile.TemporaryDirectory() as d:
            return sorted(
                i["session_id"] for i in needs.chat_items(home_with(Path(d), rows))
            )

    def test_human_platforms_are_listed_agents_are_not(self):
        rows = [
            ("tg", "", "telegram", "dm", None, "assistant", ASK),
            ("dc", "", "discord", "dm", None, "assistant", ASK),
            ("cli", "", "cli", None, None, "assistant", ASK),
            ("kb", "", "kanban", None, None, "assistant", ASK),
            ("cr", "", "cron", None, None, "assistant", ASK),
            ("sa", "", "subagent", None, None, "assistant", ASK),
            ("a2", "", "a2a", None, None, "assistant", ASK),
        ]
        self.assertEqual(self.ids(rows), ["cli", "dc", "tg"])

    def test_only_the_newest_link_of_a_continued_chat_counts(self):
        rows = [
            ("old", "", "desktop", None, None, "assistant", ASK),
            ("new", "", "desktop", None, "old", "assistant", ASK),
        ]
        self.assertEqual(self.ids(rows), ["new"])

    def test_a_machine_child_does_not_hide_its_human_parent(self):
        rows = [
            ("chat", "", "telegram", "dm", None, "assistant", ASK),
            ("worker", "", "subagent", None, "chat", "assistant", "done"),
        ]
        self.assertEqual(self.ids(rows), ["chat"])

    def test_group_chat_must_name_you(self):
        rows = [
            ("g1", "", "discord", "group", None, "assistant", ASK),
            (
                "g2",
                "",
                "discord",
                "thread",
                None,
                "assistant",
                "Ada, want me to merge it?",
            ),
        ]
        self.assertEqual(self.ids(rows), ["g2"])

    def test_platform_and_wording(self):
        with tempfile.TemporaryDirectory() as d:
            (it,) = needs.chat_items(
                home_with(
                    Path(d),
                    [("tg", "Deploy", "telegram", "dm", None, "assistant", ASK)],
                )
            )
            self.assertEqual(it["platform"], "telegram")
            self.assertFalse(it["bot_chat"])
            self.assertIn("Telegram chat", it["why"])
        with tempfile.TemporaryDirectory() as d:
            (it,) = needs.chat_items(
                home_with(
                    Path(d),
                    [("b", "Bot Chat", "desktop", None, None, "assistant", ASK)],
                )
            )
            self.assertTrue(it["bot_chat"])
            self.assertIn("Bot Chat", it["why"])

    def test_user_answered_last(self):
        self.assertEqual(
            self.ids([("tg", "", "telegram", "dm", None, "user", "yes")]), []
        )

    def test_pocket_option_points_at_the_platform(self):
        with tempfile.TemporaryDirectory() as d:
            home = home_with(
                Path(d), [("tg", "", "telegram", "dm", None, "assistant", ASK)]
            )
            q = {"items": needs.chat_items(home), "count": 1, "errors": []}
            opt = needs.pocket_pack(q)["cards"][0]["options"][0]
            self.assertIn("Telegram", opt["send"])


def pairing_home(
    tmp: Path, entries, platform="telegram", where=("platforms", "pairing")
):
    d = tmp.joinpath(*where)
    d.mkdir(parents=True)
    f = d / f"{platform}-pending.json"
    f.write_text(entries if isinstance(entries, str) else json.dumps(entries))
    return tmp, f


class Pairing(unittest.TestCase):
    def test_fresh_request_listed_without_leaking_ids(self):
        with tempfile.TemporaryDirectory() as d:
            home, f = pairing_home(
                Path(d),
                {
                    "ABCD1234": {
                        "user_id": "998877",
                        "user_name": "Ana",
                        "created_at": time.time() - 60,
                        "hash": "h",
                        "salt": "s",
                    }
                },
            )
            before = f.stat().st_mtime_ns, f.read_bytes()
            (it,) = needs.pairing_items(home, store=stock_pairing.factory(home))
            self.assertEqual(it["source"], "pairing")
            self.assertIn("Ana", it["title"])
            self.assertIn("Telegram", it["title"])
            blob = json.dumps(it)
            for secret in ("998877", "ABCD1234", '"h"', '"s"'):
                self.assertNotIn(secret, blob)
            self.assertEqual(
                (f.stat().st_mtime_ns, f.read_bytes()), before
            )  # read-only

    def test_expired_and_malformed_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            home, _ = pairing_home(
                Path(d),
                {
                    "a": {"created_at": time.time() - 4000},
                    "b": {"created_at": "x"},
                    "c": "junk",
                    "d": {},
                },
            )
            self.assertEqual(
                needs.pairing_items(home, store=stock_pairing.factory(home)), []
            )
        with tempfile.TemporaryDirectory() as d:
            home, _ = pairing_home(
                Path(d), "{not json"
            )  # the stock store reads it as empty
            self.assertEqual(
                needs.pairing_items(home, store=stock_pairing.factory(home)), []
            )
        with tempfile.TemporaryDirectory() as d:
            home, _ = pairing_home(Path(d), {})
            errors = []

            def broken(profile):
                raise ImportError("no gateway.pairing")

            self.assertEqual(needs.pairing_items(home, errors, store=broken), [])
            self.assertEqual(errors, ["pairing[default]: ImportError"])

    def test_never_reads_the_pending_files_itself(self):
        # The pending files hold the pairing codes; only the gateway's store may open them.
        src = (ROOT / "needs.py").read_text()
        self.assertNotIn("-pending.json", src)
        with tempfile.TemporaryDirectory() as d:
            home, f = pairing_home(
                Path(d),
                {
                    "0123456789abcdef": {
                        "created_at": time.time(),
                        "hash": "h",
                        "salt": "s",
                    }
                },
            )
            f.chmod(
                0
            )  # unreadable: listing still works because the store is asked, not the file
            calls = []

            def store(profile):
                calls.append(profile)
                return types.SimpleNamespace(
                    list_pending=lambda: [
                        {
                            "platform": "telegram",
                            "request_id": "0123456789abcdef",
                            "user_id": "9",
                            "user_name": "Ana",
                            "age_minutes": 2,
                        }
                    ]
                )

            (it,) = needs.pairing_items(home, store=store)
            f.chmod(0o600)
            self.assertEqual(calls, ["default"])
            self.assertEqual(
                it["approve_command"],
                "hermes pairing approve telegram 0123456789abcdef",
            )
            self.assertIn("58 min", it["why"])

    def test_profiles_and_legacy_dir_dedupe(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            pairing_home(
                home,
                {"e": {"created_at": time.time()}},
                "discord",
                ("profiles", "ops", "pairing"),
            )
            (home / "platforms").mkdir()
            os.symlink(
                home / "profiles" / "ops" / "pairing", home / "platforms" / "pairing"
            )
            self.assertEqual(
                len(needs.pairing_items(home, store=stock_pairing.factory(home))), 1
            )

    def test_in_queue_and_pocket(self):
        with tempfile.TemporaryDirectory() as d:
            home, _ = pairing_home(
                Path(d), {"e": {"user_name": "Bo", "created_at": time.time()}}
            )
            needs.kanban_items, saved = (
                (lambda boards=None, errors=None: []),
                needs.kanban_items,
            )
            try:
                with stock_pairing.patched(needs, home):
                    q = needs.build_queue(home)
            finally:
                needs.kanban_items = saved
            self.assertEqual([i["source"] for i in q["items"]], ["pairing"])
            self.assertNotIn("Bot Chat", json.dumps(needs.pocket_pack(q)))


if __name__ == "__main__":
    unittest.main()
