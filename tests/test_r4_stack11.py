"""Round-4 findings on the Ask-detection branch (#40): bad settings and stored values, what the
chat reader will open, which profile name a shared database keeps, and what is read from
profile.yaml. Every test that builds the queue isolates HERMES_KANBAN_HOME."""

import os
import json
import sqlite3
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from r4c_common import ASK, HomeCase, chat_db, needs


class QueueCase(HomeCase):
    def setUp(self):
        super().setUp()
        kanban = self.base / "kanban"
        kanban.mkdir()
        p = patch.dict(os.environ, {"HERMES_HOME": str(self.home), "HERMES_KANBAN_HOME": str(kanban)})
        p.start()
        self.addCleanup(p.stop)


class WindowSetting(QueueCase):
    def test_digit_strings_that_cannot_convert_use_the_default(self):
        """11-001 (class M, invalid input): isdigit() accepts what int() rejects."""
        for raw in ("²", "9" * 5000, "٣", "１２", "1" * 7, " ٤ "):
            with self.subTest(raw=raw[:8]):
                self.settings["chat_window_days"] = raw
                self.assertEqual(needs.chat_window_days(), 14)

    def test_the_documented_range_is_one_to_ninety_and_anything_else_is_fourteen(self):
        """11-008 (class I): plugin.yaml promises 1 to 90 and 14 otherwise."""
        for raw, want in [(1, 1), (90, 90), ("90", 90), (91, 14), (500, 14), (0, 14), ("500", 14), (True, 14)]:
            with self.subTest(raw=raw):
                self.settings["chat_window_days"] = raw
                self.assertEqual(needs.chat_window_days(), want)

    def test_the_queue_builds_with_an_unconvertible_window_setting(self):
        """11-001: not_covered_notes() ran the same conversion outside the chat handler."""
        chat_db(self.home / "state.db", [("s1", "cli", "u", ASK, "dm")])
        for raw in ("²", "9" * 5000):
            with self.subTest(raw=raw[:4]):
                self.settings["chat_window_days"] = raw
                queue = needs.build_queue(self.home)
                self.assertEqual(queue["coverage"]["chat"]["window_days"], 14)
                self.assertEqual([i["session_id"] for i in queue["items"]], ["s1"])
                self.assertFalse(any(e.startswith("chat") for e in queue["errors"]), queue["errors"])

    def test_not_covered_notes_accepts_a_missing_chat_coverage_with_a_bad_setting(self):
        self.settings["chat_window_days"] = "²"
        self.assertTrue(needs.not_covered_notes(None))

    def test_infinite_numbers_in_other_inputs_do_not_raise(self):
        """Sweep (class M): int(inf) raises OverflowError, which three handlers did not catch."""
        inf = float("inf")
        self.assertEqual(needs.ask_history_turns({"ask_history_turns": inf}), needs.ASK_HISTORY_TURNS)
        data = {f"k{i}": {"at": inf if i == 0 else i, "version": "v"} for i in range(needs._BLUF_KEEP + 5)}
        self.assertIsInstance(needs._trim_blufs(data), dict)
        needs.wait_state(Path("w.json"), {"pid": inf, "request_id": "0123456789abcdef"})

    def test_an_infinite_stored_message_time_does_not_fail_the_chat_source(self):
        """Sweep (class M): int(ts) on a stored Infinity failed every chat in the profile."""
        chat_db(self.home / "state.db", [("a", "cli", "u", ASK, "dm"), ("b", "cli", "u", ASK, "dm")])
        db = sqlite3.connect(self.home / "state.db")
        db.execute("UPDATE messages SET timestamp = 1e999 WHERE session_id = 'a'")
        db.commit()
        db.close()
        errors: list[str] = []
        ids = sorted(i["session_id"] for i in needs.chat_items(self.home, errors=errors))
        self.assertEqual(errors, [])
        self.assertIn("b", ids)


class WhatIsOpened(QueueCase):
    def _run_with_deadline(self, fn, seconds=5.0):
        box = {}

        def work():
            box["value"] = fn()

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(seconds)
        return t, box

    def test_a_fifo_in_place_of_a_database_is_refused_not_opened(self):
        """11-002 (class M, blocking special file): opening a writerless FIFO blocks forever."""
        (self.home / "profiles" / "work").mkdir(parents=True)
        fifo = self.home / "profiles" / "work" / "state.db"
        os.mkfifo(fifo)

        def release():
            try:
                fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                os.close(fd)
            except OSError:
                pass

        self.addCleanup(release)
        t, box = self._run_with_deadline(lambda: needs.build_queue(self.home))
        self.assertFalse(t.is_alive(), "the queue build blocked on a FIFO")
        queue = box["value"]
        self.assertTrue(any("chat[work]" in e and "not a regular file" in e for e in queue["errors"]), queue["errors"])
        self.assertTrue(queue["coverage"]["chat"]["failed"])

    def test_a_fifo_as_the_default_database_is_refused_too(self):
        os.mkfifo(self.home / "state.db")
        self.addCleanup(lambda: os.path.exists(self.home / "state.db") and self._poke(self.home / "state.db"))
        errors: list[str] = []
        t, box = self._run_with_deadline(lambda: needs._state_dbs(self.home, errors))
        self.assertFalse(t.is_alive())
        self.assertEqual(box["value"], [])
        self.assertTrue(any("chat[default]" in e for e in errors), errors)

    @staticmethod
    def _poke(path):
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass

    def test_a_database_that_cannot_be_inspected_is_a_failed_source(self):
        """11-003 (class G): only genuine absence is an optional missing source."""
        chat_db(self.home / "profiles" / "work" / "state.db")
        real = os.lstat

        def deny(path, *a, **k):
            if str(path).endswith(os.path.join("work", "state.db")):
                raise PermissionError(13, "denied")
            return real(path, *a, **k)

        with patch.object(needs.os, "lstat", deny):
            queue = needs.build_queue(self.home)
        self.assertTrue(any("chat[work]" in e and "could not be inspected" in e for e in queue["errors"]), queue["errors"])
        self.assertTrue(queue["coverage"]["chat"]["failed"])
        self.assertNotIn("PermissionError: denied", " ".join(queue["errors"]))  # no path or message echoed

    def test_a_missing_database_is_not_an_error(self):
        (self.home / "profiles" / "empty").mkdir(parents=True)
        (self.home / "profiles" / "afile").write_text("x")
        errors: list[str] = []
        self.assertEqual(needs._state_dbs(self.home, errors), [])
        self.assertEqual(errors, [])


class SharedDatabaseKeepsItsName(QueueCase):
    def _two_names(self):
        chat_db(self.home / "profiles" / "work" / "state.db", [("s", "cli", "u", ASK, "dm")])
        return self.home / "profiles" / "work" / "state.db"

    def _alias(self, name="alias"):
        (self.home / "profiles" / name).mkdir(parents=True)
        os.link(self.home / "profiles" / "work" / "state.db", self.home / "profiles" / name / "state.db")

    def test_a_dismissal_survives_an_alphabetically_earlier_alias(self):
        """11-004 (class A, unstable identity): the alias must not rename the chat and resurface it."""
        self._two_names()
        first = needs.build_queue(self.home)
        item = first["items"][0]
        self.assertEqual(item["id"], "chat:work:s")
        needs.hide(item["id"], item["version"], self.home)
        self.assertEqual(needs.build_queue(self.home)["items"], [])
        self._alias("alias")
        after = needs.build_queue(self.home)
        self.assertEqual([i["id"] for i in after["items"]], [])
        self.assertEqual(after["hidden"], 1)

    def test_a_cached_summary_keeps_its_profile_too(self):
        self._two_names()
        item = needs.build_queue(self.home)["items"][0]
        needs.save_bluf(item["id"], item["version"], "Yes or no", "why", self.home)
        self._alias("alias")
        ids = [i["id"] for i in needs.build_queue(self.home)["items"]]
        self.assertEqual(ids, ["chat:work:s"])

    def test_without_any_state_the_first_listed_name_is_used_once(self):
        self._two_names()
        self._alias("alias")
        names = [n for n, _ in needs._state_dbs(self.home)]
        self.assertEqual(names, ["alias"])
        self.assertEqual(len(needs.chat_items(self.home)), 1)

    def test_two_different_databases_are_both_read(self):
        self._two_names()
        chat_db(self.home / "profiles" / "alias" / "state.db", [("t", "cli", "u", ASK, "dm")])
        self.assertEqual(sorted(i["id"] for i in needs.chat_items(self.home)), ["chat:alias:t", "chat:work:s"])


class ProfileYamlRead(HomeCase):
    TITLE = "ui_meta:\n  hermes-bots:\n    title: {t}\n"

    def setUp(self):
        super().setUp()
        needs._BOT_NAME_CACHE.clear()
        self.addCleanup(needs._BOT_NAME_CACHE.clear)
        self.prof = self.home / "profiles" / "work"
        self.prof.mkdir(parents=True)
        self.yaml = self.prof / "profile.yaml"
        self.yaml.write_text(self.TITLE.format(t="Small"))

    def _swap_after_lstat(self, swap):
        real = Path.lstat
        done = []

        def lstat(path, *a, **k):
            out = real(path, *a, **k)
            if path.name == "profile.yaml" and not done:
                done.append(1)
                swap()
            return out

        return patch.object(Path, "lstat", lstat)

    def test_a_link_swapped_in_after_the_check_is_not_read(self):
        """11-005 (class A, check/open race): the read is bound to the opened descriptor."""
        secret = self.base / "elsewhere.yaml"
        secret.write_text(self.TITLE.format(t="Leaked"))

        def swap():
            self.yaml.unlink()
            self.yaml.symlink_to(secret)

        with self._swap_after_lstat(swap):
            self.assertEqual(needs._bot_name(self.home, "work"), "work")
        self.assertNotIn(str(self.yaml), needs._BOT_NAME_CACHE)

    def test_an_oversized_file_swapped_in_after_the_check_is_not_read(self):
        def swap():
            self.yaml.write_text(self.TITLE.format(t="Big") + "# pad\n" * 20000)

        with self._swap_after_lstat(swap):
            self.assertEqual(needs._bot_name(self.home, "work"), "work")

    def test_a_file_swapped_after_the_check_is_cached_under_the_signature_it_was_read_with(self):
        other = self.prof / "other.yaml"
        other.write_text(self.TITLE.format(t="Replaced title that is longer"))

        def swap():
            os.replace(other, self.yaml)

        with self._swap_after_lstat(swap):
            self.assertEqual(needs._bot_name(self.home, "work"), "Replaced title that is longer")
        st = self.yaml.stat()
        sig = needs._BOT_NAME_CACHE[str(self.yaml)][0]
        self.assertEqual(sig, (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns))

    def test_a_plain_small_file_is_still_read(self):
        self.assertEqual(needs._bot_name(self.home, "work"), "Small")


DAY = 86400


def legacy_db(path: Path, rows):
    """The oldest supported schema: no session activity columns, Bot Mode chats told apart by title.
    rows: (session id, last message age in days, text) in creation order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE sessions(id TEXT, title TEXT, archived INT);"
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,"
        " timestamp REAL, display_kind TEXT, active INT);"
    )
    now = time.time()
    for sid, age, text in rows:
        db.execute("INSERT INTO sessions VALUES(?, 'Bot Chat', 0)", (sid,))
        db.execute(
            "INSERT INTO messages(session_id, role, content, timestamp, active) VALUES(?, 'assistant', ?, ?, 1)",
            (sid, text, now - age * DAY),
        )
    db.commit()
    db.close()


class LegacyWindow(QueueCase):
    def test_the_first_created_session_with_a_current_ask_is_examined(self):
        """11-006 (class D, budget applied before the window): rowid order spent the cap on expired chats."""
        rows = [("current", 1, ASK)] + [(f"old{i}", 100, ASK) for i in range(needs.CHAT_SESSION_CAP + 5)]
        legacy_db(self.home / "profiles" / "work" / "state.db", rows)
        cov: dict = {}
        ids = [i["session_id"] for i in needs.chat_items(self.home, coverage=cov)]
        self.assertEqual(ids, ["current"])
        self.assertEqual(cov["sessions_omitted"], 0)
        self.assertEqual(cov["sessions_checked"], 1)

    def test_newest_message_decides_the_order_when_the_cap_bites(self):
        rows = [(f"s{i}", 10 - i * 0.001, ASK) for i in range(8)]
        legacy_db(self.home / "profiles" / "work" / "state.db", rows)
        with patch.object(needs, "CHAT_SESSION_CAP", 3):
            cov: dict = {}
            ids = sorted(i["session_id"] for i in needs.chat_items(self.home, coverage=cov))
        self.assertEqual(ids, ["s5", "s6", "s7"])
        self.assertEqual(cov["sessions_omitted"], 5)

    def test_equal_activity_is_ordered_newest_created_first(self):
        chat_db(
            self.home / "state.db",
            [(f"s{i}", "cli", "u", ASK, "dm") for i in range(6)],
        )
        db = sqlite3.connect(self.home / "state.db")
        db.execute("UPDATE sessions SET last_activity_at = 1000000000.0 + 0, started_at = 1000000000.0")
        db.execute("UPDATE sessions SET last_activity_at = ?", (time.time() - 30,))
        db.commit()
        db.close()
        with patch.object(needs, "CHAT_SESSION_CAP", 2):
            ids = sorted(i["session_id"] for i in needs.chat_items(self.home))
        self.assertEqual(ids, ["s4", "s5"])


class CountingConnection(sqlite3.Connection):
    """Counts the virtual-machine steps a connection spends, whatever progress handler the code
    under test installs on top."""

    made: list = []

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.ops = 0
        type(self).made.append(self)
        self.set_progress_handler(None, 1000)

    def set_progress_handler(self, handler, n):
        def counted():
            self.ops += n
            return handler() if handler else 0

        super().set_progress_handler(counted, n)


class ReadBudget(QueueCase):
    def _sessions_without_a_parent_index(self, n):
        db = sqlite3.connect(self.home / "state.db")
        db.executescript(
            "CREATE TABLE sessions(id TEXT, title TEXT, source TEXT, chat_type TEXT, parent_session_id TEXT,"
            " started_at REAL, last_activity_at REAL, user_id TEXT, display_name TEXT);"
            "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,"
            " timestamp REAL, display_kind TEXT, active INT);"
        )
        now = time.time()
        for i in range(n):
            db.execute(
                "INSERT INTO sessions VALUES(?, '', 'cli', 'dm', ?, ?, ?, 'u', 'n')",
                (f"s{i}", f"s{i - 1}" if i % 2 else None, now - 100 - i, now - 50 - i),
            )
            db.execute(
                "INSERT INTO messages(session_id, role, content, timestamp, active) VALUES(?, 'assistant', ?, ?, 1)",
                (f"s{i}", ASK, now - 50 - i),
            )
        db.commit()
        db.close()

    def _counted(self, fn):
        CountingConnection.made.clear()
        real = needs.sqlite3.connect

        def connect(*a, **k):
            return real(*a, factory=CountingConnection, **k)

        with patch.object(needs.sqlite3, "connect", connect):
            fn()
        return sum(c.ops for c in CountingConnection.made)

    def test_a_small_cap_bounds_the_database_work_not_only_the_rows(self):
        """01-054 (class D): the continuation check scanned the table once per session, twice."""
        self._sessions_without_a_parent_index(800)
        with patch.object(needs, "CHAT_SESSION_CAP", 1):
            ops = self._counted(lambda: needs.chat_items(self.home))
        self.assertLess(ops, 200_000, ops)

    def test_work_past_the_budget_is_a_failed_source_not_a_clean_read(self):
        self._sessions_without_a_parent_index(300)
        errors: list[str] = []
        cov: dict = {}
        with patch.object(needs, "CHAT_QUERY_OPS", 3000):
            items = needs.chat_items(self.home, errors=errors, coverage=cov)
        self.assertTrue(any("read budget exceeded" in e for e in errors), errors)
        self.assertEqual(items, [])

    def test_the_budget_applies_to_the_per_session_reads_too(self):
        self._sessions_without_a_parent_index(300)
        errors: list[str] = []
        with patch.object(needs, "CHAT_QUERY_OPS", 60_000):
            needs.chat_items(self.home, errors=errors)
        self.assertTrue(any("read budget exceeded" in e for e in errors), errors)

    def test_a_continuation_chain_still_lists_only_its_newest_link(self):
        chat_db(self.home / "state.db", [("a", "cli", "u", ASK, "dm"), ("b", "cli", "u", ASK, "dm")])
        db = sqlite3.connect(self.home / "state.db")
        db.execute("ALTER TABLE sessions ADD COLUMN parent_session_id TEXT")
        db.execute("UPDATE sessions SET parent_session_id = 'a' WHERE id = 'b'")
        db.commit()
        db.close()
        self.assertEqual([i["session_id"] for i in needs.chat_items(self.home)], ["b"])


class ReadSnapshot(QueueCase):
    def test_the_omitted_count_comes_from_the_same_snapshot_as_the_sessions_read(self):
        """01-053 (class A, stale snapshot): a delete between the two reads hid an unread session."""
        path = self.home / "state.db"
        chat_db(path, [(f"s{i:03d}", "cli", "u", ASK, "dm") for i in range(5)])
        writer = sqlite3.connect(path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE sessions SET last_activity_at = ? + rowid", (time.time() - 3600,))
        writer.commit()
        real = needs.sqlite3.connect
        deleted = []

        def connect(*a, **k):
            conn = real(*a, **k)

            def trace(statement):
                if "COUNT(" in statement and not deleted:
                    deleted.append(1)
                    writer.execute("DELETE FROM sessions WHERE id = 's004'")  # the newest, already read
                    writer.commit()

            conn.set_trace_callback(trace)
            return conn

        cov: dict = {}
        with patch.object(needs, "CHAT_SESSION_CAP", 4), patch.object(needs.sqlite3, "connect", connect):
            needs.chat_items(self.home, coverage=cov)
        self.assertEqual(deleted, [1])
        self.assertEqual(cov["sessions_checked"], 4)
        self.assertEqual(cov["sessions_omitted"], 1)


class UnreadChatSummaries(QueueCase):
    def _many(self, n):
        chat_db(
            self.home / "profiles" / "work" / "state.db",
            [(f"s{i:03d}", "cli", "u", ASK, "dm") for i in range(n)],
        )
        db = sqlite3.connect(self.home / "profiles" / "work" / "state.db")
        db.execute("UPDATE sessions SET last_activity_at = ? + rowid", (time.time() - 3600,))
        db.commit()
        db.close()

    def test_a_summary_for_a_chat_past_the_scan_limit_is_kept(self):
        """01-086 (class H, prune unobserved state): omission must not delete the cached summary."""
        self._many(3)
        item = next(i for i in needs.build_queue(self.home)["items"] if i["session_id"] == "s000")
        needs.save_bluf(item["id"], item["version"], "Say yes", "why", self.home)
        with patch.object(needs, "CHAT_SESSION_CAP", 2):
            queue = needs.build_queue(self.home)
        self.assertEqual(queue["coverage"]["chat"]["sessions_omitted"], 1)
        self.assertNotIn(item["id"], [i["id"] for i in queue["items"]])
        self.assertIn(item["id"], needs.load_blufs(self.home))
        back = next(i for i in needs.build_queue(self.home)["items"] if i["id"] == item["id"])
        self.assertEqual(back["bluf"], "Say yes")

    def test_a_summary_for_a_chat_that_was_read_and_answered_is_still_pruned(self):
        self._many(3)
        needs.save_bluf("chat:work:gone", "1:abc", "Old", "why", self.home)
        with patch.object(needs, "CHAT_SESSION_CAP", 2):
            needs.build_queue(self.home)
        # the omitted profile keeps every chat summary it has; the stale one is dropped only
        # once the profile is read in full
        self.assertIn("chat:work:gone", needs.load_blufs(self.home))
        needs.build_queue(self.home)
        self.assertNotIn("chat:work:gone", needs.load_blufs(self.home))


class OmissionIsNeverAllClear(QueueCase):
    """01-020 (class E, false-complete coverage): a queue that left chat sessions unread must not
    be reported as empty by any surface."""

    def _queue_with_omission(self):
        chat_db(
            self.home / "state.db",
            [(f"s{i}", "cli", "u", "thanks", "dm") for i in range(3)] + [("old", "cli", "u", ASK, "dm")],
        )
        db = sqlite3.connect(self.home / "state.db")
        db.execute("UPDATE sessions SET last_activity_at = ? WHERE id = 'old'", (time.time() - 3600 * 5,))
        db.execute("UPDATE sessions SET last_activity_at = ? WHERE id != 'old'", (time.time() - 60,))
        db.commit()
        db.close()
        patcher = patch.object(needs, "CHAT_SESSION_CAP", 3)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _surfaces(self):
        import test_outside

        plugin = test_outside.plugin
        real = needs.build_queue
        with patch.object(plugin, "_needs", lambda: needs), patch.object(
            needs, "build_queue", lambda *a, **k: real(self.home)
        ):
            return plugin.slash_unbl0ck(platform="cli"), plugin.slash_unbl0ck(platform="telegram")

    def test_coverage_gaps_names_unread_sessions_and_is_silent_when_complete(self):
        self._queue_with_omission()
        q = needs.build_queue(self.home)
        self.assertEqual(q["items"], [])
        gaps = needs.coverage_gaps(q)
        self.assertEqual(len(gaps), 1)
        self.assertIn("1 older chat session", gaps[0])
        self.assertEqual(needs.coverage_gaps({"coverage": {"chat": {"sessions_omitted": 0}}}), [])
        self.assertEqual(needs.coverage_gaps({}), [])
        self.assertEqual(needs.coverage_gaps({"coverage": {"chat": {"sessions_omitted": "x"}}}), [])

    def test_the_cli_and_slash_summaries_do_not_say_nothing_is_blocked(self):
        self._queue_with_omission()
        local, remote = self._surfaces()
        for text in (local, remote):
            self.assertNotIn("Nothing is blocked on you", text)
            self.assertIn("incomplete", text)
        self.assertIn("1 older chat session", local)

    def test_a_complete_empty_queue_still_says_nothing_is_blocked(self):
        chat_db(self.home / "state.db", [("a", "cli", "u", "thanks", "dm")])
        local, remote = self._surfaces()
        self.assertEqual(local, "Nothing is blocked on you.")
        self.assertEqual(remote, "Nothing is blocked on you.")


class DesktopOmission(unittest.TestCase):
    def test_the_page_is_not_all_clear_and_opens_the_coverage_when_sessions_were_not_read(self):
        from test_r4_stack01 import PRELUDE, run_views

        body = r"""
const ctx = {rest: async () => ({})}, refresh = async () => {};
env.useValue = key => key === 'queue' ? q : false;
function render(q0) { globalThis.q = q0; state = []; cursor = 0; return Page({ctx, refresh}); }
const base = {items:[], count:0, counts:{asks:0,stuck:0,answered:0}, errors:[], at:Date.now()};
const cov = n => ({chat:{window_days:14, sessions_omitted:n}, not_covered:['Detection phrases are English.']});
let tree = render({...base, coverage: cov(7)});
let page = text(tree);
assert.ok(page.includes('NOT ALL CLEAR'), 'the page says it is not all clear');
assert.ok(page.includes('7 older chat session'), 'the count is shown on the page, not only in details');
assert.ok(!page.replace('NOT ALL CLEAR', '').includes('ALL CLEAR'), 'plain ALL CLEAR is gone');
const det = all(tree).find(n => n && n.type === 'details');
assert.ok(det && det.props.open === true, 'the coverage details are open');
const whole = render({...base, coverage: cov(0)});
page = text(whole);
assert.ok(page.includes('● ALL CLEAR') && !page.includes('NOT ALL CLEAR'), 'a complete read stays all clear');
const det0 = all(whole).find(n => n && n.type === 'details');
assert.ok(det0 && !det0.props.open, 'details stay collapsed when nothing is omitted');
"""
        for name, code, err in run_views(PRELUDE + body):
            with self.subTest(file=name):
                self.assertEqual(code, 0, err)

    def test_the_chip_reports_an_incomplete_queue_when_sessions_were_not_read(self):
        from test_r4_stack01 import run_views

        script = r"""
const fs = require('fs'), assert = require('node:assert/strict');
const src = fs.readFileSync(process.argv[1], 'utf8');
const body = src.slice(src.indexOf('function omittedChats('), src.indexOf('function coverageNote(')) +
  src.slice(src.indexOf('function Chip('), src.indexOf('export default'));
let q = null;
const env = {jsx: (t, p) => ({type: t, props: p}), useValue: () => q, askCount: x => x.count || 0,
  host: {navigate() {}}, PATH: '/x', $queue: 'queue'};
const {Chip} = new Function('env', `with(env){${body};return {Chip};}`)(env);
q = {count: 0, errors: [], coverage: {chat: {sessions_omitted: 4}}};
const chip = Chip();
assert.ok(chip, 'a chip is shown');
assert.ok(String(chip.props.children).includes('QUEUE INCOMPLETE'));
q = {count: 0, errors: [], coverage: {chat: {sessions_omitted: 0}}};
assert.equal(Chip(), null);
"""
        for name, code, err in run_views(script):
            with self.subTest(file=name):
                self.assertEqual(code, 0, err)


class WidenedWindowIsBackfill(QueueCase):
    """11-007 (class I/G, a promise the code did not keep): an ask found only because the window was
    widened is backfill, even when it is younger than the default 14 days."""

    def _home(self, rows):
        from test_ask_coverage import make_chats

        return make_chats(Path(tempfile.mkdtemp(dir=self.home)), rows)

    def _queue(self, home, days):
        from test_ask_coverage import settings

        with patch.object(needs, "_settings", settings(chat_window_days=days)):
            return {i["session_id"]: i for i in needs.build_queue(home)["items"] if i["source"] == "chat"}

    def test_an_ask_a_widened_window_reaches_is_flagged_and_stays_flagged(self):
        home = self._home([("recent", "cli", "dm", "u", 0.2, ASK), ("three", "cli", "dm", "u", 3, ASK)])
        first = self._queue(home, 1)
        self.assertEqual(sorted(first), ["recent"])
        self.assertFalse(first["recent"].get("backfilled"))
        wide = self._queue(home, 7)
        self.assertTrue(wide["three"].get("backfilled"), "reached only by the wider window")
        self.assertFalse(wide["recent"].get("backfilled"))
        again = self._queue(home, 7)
        self.assertTrue(again["three"].get("backfilled"), "the flag survives the next refresh")
        wider = self._queue(home, 30)
        self.assertTrue(wider["three"].get("backfilled"), "and a further widening")

    def test_a_window_that_was_always_wide_is_not_backfill(self):
        home = self._home([("three", "cli", "dm", "u", 3, ASK)])
        self.assertFalse(self._queue(home, 7)["three"].get("backfilled"))
        self.assertFalse(self._queue(home, 7)["three"].get("backfilled"))

    def test_narrowing_and_widening_back_does_not_flag_what_was_already_read(self):
        home = self._home([("three", "cli", "dm", "u", 3, ASK)])
        self._queue(home, 7)
        self._queue(home, 1)
        self.assertFalse(self._queue(home, 7)["three"].get("backfilled"))

    def test_a_flagged_chat_is_not_sent_to_a_model_by_the_dashboard(self):
        import test_bluf

        api = test_bluf.BlufTest().load_api_without_fastapi()
        home = self._home([("three", "cli", "dm", "u", 3, ASK)])
        self._queue(home, 1)
        sent = []
        real = needs.build_queue

        def build(*a, **k):
            from test_ask_coverage import settings

            with patch.object(needs, "_settings", settings(chat_window_days=7)):
                q = real(home)
            q["bluf_enabled"] = True
            return q

        n = types.SimpleNamespace(build_queue=build, NO_BLUF_SOURCES=())
        with patch.object(api, "_needs", lambda: n), patch.object(
            api, "_loaded_ctx", lambda: types.SimpleNamespace(llm=object())
        ), patch.object(api, "_queue_bluf", lambda n_, ctx, it: sent.append(it["id"])), patch.object(
            api._ASK_JOBS, "housekeeping", lambda: None
        ):
            api.queue()
        self.assertEqual(sent, [])

    def test_searching_reads_the_marks_but_never_writes_them(self):
        home = self._home([("three", "cli", "dm", "u", 3, ASK)])
        needs.chat_items(home)
        self.assertFalse((home / "plugin-data" / "unbl0ck" / needs.BACKFILL_FILE).exists())

    def test_the_marks_hold_a_time_and_ids_only_and_are_bounded(self):
        home = self._home([("three", "cli", "dm", "u", 3, ASK)])
        self._queue(home, 1)
        self._queue(home, 7)
        data = json.loads((home / "plugin-data" / "unbl0ck" / needs.BACKFILL_FILE).read_text())
        self.assertEqual(sorted(data), ["floor", "ids"])
        self.assertEqual(data["ids"], ["chat:work:three"])
        self.assertNotIn("Should I open", json.dumps(data))
        self.assertLessEqual(needs.BACKFILL_IDS_MAX, 1000)

    def test_the_documented_promise_matches_the_behavior(self):
        root = Path(__file__).resolve().parents[1]
        yaml_text = " ".join((root / "plugin.yaml").read_text().split())
        guide = " ".join((root / "docs" / "operator-guide.md").read_text().split())
        self.assertIn("1 to 90; anything else uses 14", yaml_text)
        self.assertIn("older than any earlier read", yaml_text)
        self.assertIn("older than any earlier read", guide)
        self.assertIn(needs.BACKFILL_FILE, guide)
