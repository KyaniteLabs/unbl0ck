"""Round-4 findings 01-026 .. 01-050 owned by the first stack branch: state files, summaries,
Kanban versions and bot titles. Each test is the finding's reproducer on the real code path."""

import importlib
import importlib.util
import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_outside import OutsideFixture, needs
from _redaction import assert_redacted

ROOT = Path(__file__).resolve().parents[1]

try:
    kanban_db = importlib.import_module("hermes_cli.kanban_db")
    kanban_db_connect = importlib.import_module("hermes_cli.kanban_db_connect")
    HERMES_ERROR = None
except ImportError as exc:  # pragma: no cover
    HERMES_ERROR = str(exc)


def run_with_timeout(fn, seconds=3):
    """Run fn in a daemon thread; return (finished, result_or_exception)."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    return (not t.is_alive()), box


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.data = needs.data_dir(self.home)


class StateFileSafety(Home):
    def test_a_symlinked_data_directory_is_refused(self):
        """01-026 (class C): plugin-data/unbl0ck pointing at another plugin's folder is never written."""
        other = Path(self.tmp.name) / "other-plugin"
        other.mkdir()
        (other / "pocket-pack.json").write_text("{\"theirs\": true}")
        home = Path(self.tmp.name) / "h2"
        (home / "plugin-data").mkdir(parents=True)
        (home / "plugin-data" / "unbl0ck").symlink_to(other, target_is_directory=True)
        with self.assertRaises(OSError):
            needs.write_pocket_pack({}, home, pack={"x": 1})
        self.assertEqual((other / "pocket-pack.json").read_text(), "{\"theirs\": true}")
        self.assertEqual(sorted(p.name for p in other.iterdir()), ["pocket-pack.json"])

    def test_a_planted_temp_symlink_is_not_written_through(self):
        """01-029 (class C): a predictable temp name that is a symlink never clobbers its target."""
        victim = Path(self.tmp.name) / "victim.txt"
        victim.write_text("keep me")
        target = self.data / "hidden.json"
        tmp = target.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.symlink_to(victim)
        needs.hide("chat:p:s", "1", self.home)
        self.assertEqual(victim.read_text(), "keep me")
        self.assertFalse(target.is_symlink())
        self.assertEqual(needs.load_hidden(self.home), {"chat:p:s": "1"})

    def test_a_leftover_temp_file_does_not_loosen_the_permissions(self):
        """01-029: an existing 0644 temp file is replaced, not reused with its old mode."""
        target = self.data / "hidden.json"
        tmp = target.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text("old")
        tmp.chmod(0o644)
        needs.hide("chat:p:s", "1", self.home)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_a_dangling_lock_symlink_is_not_followed(self):
        """01-030 (class C): .state.lock as a dangling link creates nothing outside the data dir."""
        outside = Path(self.tmp.name) / "outside-lock"
        (self.data / ".state.lock").symlink_to(outside)
        with self.assertRaises(OSError):
            with needs._state_lock(self.home):
                pass
        self.assertFalse(outside.exists())
        with self.assertRaises(OSError):
            needs.hide("chat:p:s", "1", self.home)
        self.assertFalse(outside.exists())

    def test_a_replace_blocked_by_a_reader_is_retried(self):
        """01-035 (class M, Windows reader/writer): a transient PermissionError on replace is retried."""
        real = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise PermissionError("sharing violation")
            return real(src, dst)

        with patch.object(os, "replace", flaky):
            needs.hide("chat:p:s", "1", self.home)
        self.assertGreaterEqual(calls["n"], 4)
        self.assertEqual(needs.load_hidden(self.home), {"chat:p:s": "1"})
        self.assertEqual([p.name for p in self.data.glob("*.tmp")], [])

    def test_a_replace_that_never_succeeds_still_fails_and_cleans_up(self):
        with patch.object(os, "replace", side_effect=PermissionError("locked")), patch.object(
            needs, "_REPLACE_ATTEMPTS", 3
        ):
            with self.assertRaises(PermissionError):
                needs.hide("chat:p:s", "1", self.home)
        self.assertEqual([p.name for p in self.data.glob("*.tmp")], [])


class BoundedStateReads(Home):
    def fifo(self, path):
        os.mkfifo(path)

    def test_a_fifo_wait_record_does_not_block_the_prompt_source(self):
        """01-028 (class D): a FIFO named <rid>.json in waits is reported, never read blockingly."""
        self.fifo(needs._waits_dir(self.home) / ("0123456789abcdef" + ".json"))
        errors = []
        done, box = run_with_timeout(lambda: needs.wait_items(self.home, errors=errors))
        self.assertTrue(done, "reading a FIFO wait record blocked")
        self.assertEqual(box.get("value"), [])
        self.assertTrue(errors and "could not read" in errors[0])

    def test_a_fifo_hidden_file_does_not_block_hide(self):
        self.fifo(self.data / "hidden.json")
        done, box = run_with_timeout(lambda: needs.hide("chat:p:s", "1", self.home))
        self.assertTrue(done, "hide blocked on a FIFO")
        self.assertIsInstance(box.get("error"), RuntimeError)

    def test_a_fifo_filed_record_does_not_block_a_dedupe_scan(self):
        outside = needs._outside_dir(self.home)
        self.fifo(outside / ("a" * 32 + ".json"))
        done, _ = run_with_timeout(
            lambda: needs.file_outside(
                {"kind": "draft", "title": "t", "body": "b", "act_where": "w", "dedupe_key": "k"}, self.home
            )
        )
        self.assertTrue(done, "filing blocked on a FIFO record")

    def test_a_fifo_filed_record_does_not_block_outside_items(self):
        outside = needs._outside_dir(self.home)
        self.fifo(outside / ("b" * 32 + ".json"))
        done, _ = run_with_timeout(lambda: needs.outside_items(self.home, errors=[]))
        self.assertTrue(done, "the outside scan blocked on a FIFO record")

    def test_an_oversized_state_file_is_refused_before_it_is_parsed(self):
        path = self.data / "hidden.json"
        path.write_text(json.dumps({"k": "v" * 400}))
        with patch.object(needs, "MAX_STATE_BYTES", 100):
            errors = []
            self.assertEqual(needs.load_hidden(self.home, errors=errors), {})
            self.assertTrue(errors)
            with self.assertRaises(RuntimeError):
                needs.load_hidden(self.home, strict=True)

    def test_a_symlinked_state_file_is_not_read(self):
        real = Path(self.tmp.name) / "real.json"
        real.write_text(json.dumps({"k": "1"}))
        (self.data / "hidden.json").symlink_to(real)
        with self.assertRaises(RuntimeError):
            needs.load_hidden(self.home, strict=True)


class StateSchemaAndRetention(Home):
    def test_a_hidden_entry_of_the_wrong_shape_is_a_strict_failure(self):
        """01-031 (class G): unhide must not rewrite a record it cannot read as supported."""
        path = self.data / "hidden.json"
        path.write_text(json.dumps({"chat:p:s": {"future": "format"}}))
        before = path.read_text()
        with self.assertRaises(RuntimeError):
            needs.unhide("chat:p:s", self.home)
        with self.assertRaises(RuntimeError):
            needs.hide("chat:other:s", "1", self.home)
        self.assertEqual(path.read_text(), before)

    def test_a_stale_tab_cannot_replace_a_newer_dismissal(self):
        """01-032 (class A): hiding version 1 after version 2 was hidden is refused."""
        needs.hide("kanban:b:t", "2", self.home)
        queue = {"items": [{"id": "kanban:b:t", "version": "2"}]}
        with patch.object(needs, "build_queue", lambda *a, **kw: queue):
            with self.assertRaises(needs.StaleHide):
                needs.hide("kanban:b:t", "1", self.home)
            self.assertEqual(needs.load_hidden(self.home), {"kanban:b:t": "2"})
            needs.hide("kanban:b:t", "2", self.home)  # the same version is a no-op repeat
        # The card moved on to version 3 and the person hides what they see now: allowed.
        queue = {"items": [{"id": "kanban:b:t", "version": "3"}]}
        with patch.object(needs, "build_queue", lambda *a, **kw: queue):
            needs.hide("kanban:b:t", "3", self.home)
        self.assertEqual(needs.load_hidden(self.home), {"kanban:b:t": "3"})

    def test_hiding_still_works_when_the_item_cannot_be_looked_up(self):
        needs.hide("kanban:b:t", "2", self.home)
        with patch.object(needs, "build_queue", side_effect=OSError("boom")):
            needs.hide("kanban:b:t", "1", self.home)
        self.assertEqual(needs.load_hidden(self.home), {"kanban:b:t": "1"})

    def test_legacy_null_and_empty_hidden_values_are_still_supported(self):
        (self.data / "hidden.json").write_text(json.dumps({"a": None, "b": ""}))
        self.assertTrue(needs.unhide("a", self.home))

    def test_a_pending_release_of_the_wrong_shape_is_a_strict_failure(self):
        """01-039 (class G): the answer preflight must refuse a store recovery would call unsupported."""
        (self.data / "pending-release.json").write_text(json.dumps({"kanban:b:t": {"invalid": "shape"}}))
        with self.assertRaises(RuntimeError):
            needs.load_pending_releases(self.home, strict=True)

    def test_hidden_eviction_follows_dismissal_order_not_key_order(self):
        """01-033 (class H): the cap evicts the oldest dismissal, not the smallest key."""
        with patch.object(needs, "_HIDDEN_KEEP", 2):
            for item in ("z", "a", "y"):
                needs.hide(item, "1", self.home)
        self.assertEqual(sorted(json.loads((self.data / "hidden.json").read_text())), ["a", "y"])

    def test_pending_release_eviction_follows_insertion_order(self):
        with patch.object(needs, "_HIDDEN_KEEP", 2):
            for item in ("z", "a"):
                needs._set_pending_release(item, "1", self.home)
            with self.assertRaises(needs.PendingReleaseFull):
                needs._set_pending_release("y", "1", self.home)
        self.assertEqual(list(needs.load_pending_releases(self.home)), ["z", "a"])

    def test_updating_a_pending_release_refreshes_its_position(self):
        """01-036 (class H): an updated marker is the newest, not the oldest."""
        needs._set_pending_release("A", "1", self.home)
        needs._set_pending_release("B", "1", self.home)
        needs._set_pending_release("A", "2", self.home)
        self.assertEqual(list(needs.load_pending_releases(self.home)), ["B", "A"])

    def test_a_full_recovery_store_refuses_a_new_marker_instead_of_evicting(self):
        """01-040 (class H): an unfinished release is never silently dropped."""
        with patch.object(needs, "_HIDDEN_KEEP", 3):
            for item in ("a", "b", "c"):
                needs._set_pending_release(item, "1", self.home)
            with self.assertRaises(needs.PendingReleaseFull):
                needs._set_pending_release("d", "1", self.home)
            needs._set_pending_release("a", "2", self.home)  # an existing marker may update
        data = needs.load_pending_releases(self.home)
        self.assertEqual(sorted(data), ["a", "b", "c"])
        self.assertEqual(data["a"], "2")

    def test_an_older_marker_write_does_not_replace_a_newer_one(self):
        """01-034 (class A): a delayed marker for version 11 cannot replace version 12's."""
        needs._set_pending_release("kanban:b:t", "12", self.home)
        needs._set_pending_release("kanban:b:t", "11", self.home)
        self.assertEqual(needs.load_pending_releases(self.home), {"kanban:b:t": "12"})

    def test_a_cleanup_only_removes_the_marker_it_belongs_to(self):
        needs._set_pending_release("kanban:b:t", "12", self.home)
        needs._set_pending_release("kanban:b:t", None, self.home, expected="11")
        self.assertEqual(needs.load_pending_releases(self.home), {"kanban:b:t": "12"})
        needs._set_pending_release("kanban:b:t", None, self.home, expected="12")
        self.assertEqual(needs.load_pending_releases(self.home), {})


class SummaryCache(Home):
    def test_a_corrupt_summary_cache_is_not_overwritten_by_a_save(self):
        """01-038 (class G): save_bluf over damaged JSON would destroy the recoverable bytes."""
        path = self.data / "bluf.json"
        path.write_text('{"important":')
        with self.assertRaises(RuntimeError):
            needs.save_bluf("kanban:b:t", "1", "Pick one.", "Because.", self.home)
        with self.assertRaises(RuntimeError):
            needs.prune_blufs([], self.home)
        self.assertEqual(path.read_text(), '{"important":')

    def test_a_corrupt_summary_cache_does_not_break_the_queue(self):
        (self.data / "bluf.json").write_text('{"important":')
        with patch.object(needs, "hermes_home", lambda: self.home), patch.object(
            needs, "kanban_items", lambda **kw: []
        ), patch.object(needs, "chat_items", lambda *a, **kw: []), patch.object(
            needs, "wait_items", lambda *a, **kw: []
        ), patch.object(needs, "pairing_items", lambda *a, **kw: []), patch.object(
            needs, "_settings", lambda: {}
        ):
            q = needs.build_queue(self.home)
        self.assertEqual(q["items"], [])
        self.assertEqual((self.data / "bluf.json").read_text(), '{"important":')

    def test_a_summary_that_is_not_text_is_not_attached(self):
        """01-041 (class G): an object-valued summary would crash the desktop render."""
        (self.data / "bluf.json").write_text(
            json.dumps({"kanban:b:t": {"version": "1", "bluf": {"bad": "shape"}, "why": ["x"]}})
        )
        (item,) = needs.attach_blufs([{"id": "kanban:b:t", "version": "1"}], self.home)
        self.assertNotIn("bluf", item)
        self.assertNotIn("bluf_why", item)

    def test_a_summary_with_a_bad_why_keeps_only_the_good_part(self):
        (self.data / "bluf.json").write_text(
            json.dumps({"kanban:b:t": {"version": "1", "bluf": "Pick one.", "why": {"x": 1}}})
        )
        (item,) = needs.attach_blufs([{"id": "kanban:b:t", "version": "1"}], self.home)
        self.assertEqual((item["bluf"], item["bluf_why"]), ("Pick one.", ""))

    def test_a_changed_card_is_summarized_again(self):
        """01-047 (class A, summary half): the cache is tied to the text it summarized."""
        item = {"id": "kanban:b:t", "version": "1", "title": "Old title", "ask": "Pick.", "owner": "o",
                "comments_sig": "0:0"}
        needs.save_bluf(item["id"], "1", "Pick one.", "Because.", self.home, fp=needs.summary_fingerprint(item))
        (hit,) = needs.attach_blufs([dict(item)], self.home)
        self.assertEqual(hit["bluf"], "Pick one.")
        for change in ({"title": "New title"}, {"ask": "Pick two."}, {"comments_sig": "1:5"}, {"owner": "p"}):
            (miss,) = needs.attach_blufs([{**item, **change}], self.home)
            self.assertNotIn("bluf", miss, change)

    def test_a_summary_cached_before_fingerprints_still_attaches(self):
        needs.save_bluf("kanban:b:t", "1", "Pick one.", "Because.", self.home)
        (hit,) = needs.attach_blufs(
            [{"id": "kanban:b:t", "version": "1", "title": "t", "ask": "a", "owner": "o"}], self.home
        )
        self.assertEqual(hit["bluf"], "Pick one.")

    def test_summary_input_fields_are_bounded(self):
        """01-042 (class D): title, owner and comment authors are cut like the body."""
        big = "x" * 1_000_000
        msgs = needs.bluf_messages(
            {"title": big, "ask": "short", "owner": big},
            [{"author": big, "body": "hi"}],
        )
        content = msgs[1]["content"]
        self.assertLess(len(content), 6000 + 600 * 6 + 2000)

    def test_a_secret_split_by_the_summary_cut_is_redacted_first(self):
        """01-043 (class K): redaction runs on the whole text, then the text is cut."""
        pem = "-----BEGIN PRIVATE KEY-----\n" + "QUJDREVGR0hJSktMTU5PUA" * 120 + "\n-----END PRIVATE KEY-----"
        ask = "x" * 5800 + " " + pem
        content = needs.bluf_messages({"title": "t", "ask": ask, "owner": "o"})[1]["content"]
        self.assertNotIn("QUJDREVGR0hJSktMTU5PUA", content)
        view = needs._ask_card_view({"t": "y" * 100 + pem}, _text_budget=[400])
        self.assertNotIn("QUJDREVGR0hJSktMTU5PUA", json.dumps(view))

    def test_a_long_chat_secret_keeps_no_bare_value_in_the_excerpt(self):
        secret = "S3cr3tV4lue" * 60
        text = "Please use this: password=" + secret + " . Should I go ahead?"
        redacted_first = needs._excerpt(needs._ask_redact(text))
        assert_redacted(redacted_first, "S3cr3tV4lue")
        db = self.home / "state.db"
        con = sqlite3.connect(db)
        con.execute(
            "CREATE TABLE sessions(id TEXT, title TEXT, source TEXT, chat_type TEXT, started_at REAL,"
            " last_activity_at REAL, user_id TEXT, display_name TEXT)"
        )
        con.execute(
            "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,"
            " timestamp REAL, display_kind TEXT, active INT)"
        )
        now = time.time()
        con.execute("INSERT INTO sessions VALUES('s','','cli','dm',?,?,'u','n')", (now - 60, now - 30))
        con.execute(
            "INSERT INTO messages(session_id, role, content, timestamp) VALUES('s','assistant',?,?)",
            (text, now - 30),
        )
        con.commit()
        con.close()
        with patch.object(needs, "_settings", lambda: {"principal_name": "Ada"}):
            (item,) = needs.chat_items(self.home)
        self.assertNotIn("S3cr3tV4lue", item["ask"])


class FullTextWhitespace(unittest.TestCase):
    def test_the_first_line_keeps_its_indentation(self):
        """01-044 (class K): only blank lines and trailing space are trimmed."""
        text, cut = needs._full_text("    print(1)\n  print(2)")
        self.assertEqual(text, "    print(1)\n  print(2)")
        self.assertFalse(cut)
        self.assertEqual(needs._full_text("\n\n  \n\tx  \n\n")[0], "\tx")
        self.assertEqual(needs._full_text("  \n ")[0], "")


@unittest.skipIf(HERMES_ERROR, f"Hermes unavailable: {HERMES_ERROR}")
class KanbanVersions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.dict(
            os.environ, {"HERMES_HOME": self.tmp.name, "HERMES_KANBAN_HOME": self.tmp.name}
        )
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(needs, "_settings", lambda: {"principal_name": "CEO", "comment_author": "operator"})
        p.start()
        self.addCleanup(p.stop)
        self.home = Path(self.tmp.name)

    def conn(self):
        return kanban_db_connect.connect(board="default")

    def card(self, conn, title="Choose a database"):
        task = kanban_db.create_task(conn, title=title, body="", assignee="worker")
        task = getattr(task, "id", task)
        kanban_db.block_task(conn, task, reason="Needs CEO approval", kind="needs_input")
        return task

    def test_the_latest_block_follows_the_event_order_not_the_clock(self):
        """01-045 (class A): a clock correction must not make an older block the latest."""
        with self.conn() as conn:
            task = self.card(conn)
            kanban_db.unblock_task(conn, task)
            kanban_db.block_task(conn, task, reason="Needs CEO approval again", kind="needs_input")
            rows = conn.execute(
                "SELECT id FROM task_events WHERE task_id=? AND kind IN ('blocked','block_loop_detected') ORDER BY id", (task,)
            ).fetchall()
            first, second = rows[0][0], rows[1][0]
            conn.execute("UPDATE task_events SET created_at=100 WHERE id=?", (first,))
            conn.execute("UPDATE task_events SET created_at=99 WHERE id=?", (second,))
            conn.commit()
            self.assertEqual(needs._latest_block(conn, kanban_db, task).id, second)

    def test_a_comment_before_a_same_second_reblock_does_not_answer_the_new_block(self):
        """01-046 (class A): v1's answer is not v2's answer when both fall in one second."""
        with self.conn() as conn:
            task = self.card(conn)
            kanban_db.add_comment(conn, task, author="operator", body="Use the first option")
            kanban_db.unblock_task(conn, task)
            kanban_db.block_task(conn, task, reason="Needs CEO approval again", kind="needs_input")
            block = needs._latest_block(conn, kanban_db, task)
            conn.execute("UPDATE task_comments SET created_at=? WHERE task_id=?", (block.created_at, task))
            conn.commit()
            self.assertFalse(
                needs._answered_since(conn, kanban_db, task, "operator", block.id)
            )
            (item,) = needs.kanban_items(boards=["default"])
            self.assertEqual(item["tier"], "ask")
            kanban_db.add_comment(conn, task, author="operator", body="Now the second")
            conn.execute("UPDATE task_comments SET created_at=? WHERE task_id=?", (block.created_at, task))
            conn.commit()
            self.assertTrue(
                needs._answered_since(conn, kanban_db, task, "operator", block.id)
            )

    def test_a_kanban_item_carries_a_comment_signature(self):
        """01-047: a new comment changes the item's summary fingerprint without a new block."""
        with self.conn() as conn:
            task = self.card(conn)
        (before,) = needs.kanban_items(boards=["default"])
        with self.conn() as conn:
            kanban_db.add_comment(conn, task, author="worker", body="More detail")
        (after,) = needs.kanban_items(boards=["default"])
        self.assertEqual(before["version"], after["version"])
        self.assertNotEqual(needs.summary_fingerprint(before), needs.summary_fingerprint(after))

    def test_releasing_never_clears_a_newer_failed_release_marker(self):
        """01-034 (class A): v1's successful release leaves the marker recorded for a newer block."""
        with self.conn() as conn:
            task = self.card(conn)
            version = str(needs._latest_block(conn, kanban_db, task).id)
        item = f"kanban:default:{task}"
        needs._set_pending_release(item, "999999", self.home)
        result = needs.answer_card("default", task, "Go", release=True, version=version, home=self.home)
        self.assertTrue(result["released"])
        self.assertEqual(needs.load_pending_releases(self.home), {item: "999999"})

    def test_a_full_marker_store_still_reports_the_failed_release(self):
        """01-040: the release failure is reported even when its retry marker cannot be stored."""
        with self.conn() as conn:
            task = self.card(conn)
            version = str(needs._latest_block(conn, kanban_db, task).id)
        with patch.object(needs, "_unblock_task_at_version", return_value=False), patch.object(
            needs, "_set_pending_release", side_effect=needs.PendingReleaseFull("full")
        ):
            with self.assertRaises(needs.UnblockFailed) as ctx:
                needs.answer_card("default", task, "Go", release=True, version=version, home=self.home)
        self.assertIn("unfinished", str(ctx.exception))


class BotTitles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.profile_dir = self.home / "profiles" / "p"
        self.profile_dir.mkdir(parents=True)
        self.path = self.profile_dir / "profile.yaml"
        needs._BOT_NAME_CACHE.clear()
        self.addCleanup(needs._BOT_NAME_CACHE.clear)

    def yaml(self, title):
        return f"ui_meta:\n  hermes-bots:\n    title: {title}\n"

    def test_an_atomic_replace_with_the_same_size_and_mtime_is_noticed(self):
        """01-048 (class A): the cache signature includes the file's identity."""
        self.path.write_text(self.yaml("Alice"))
        self.assertEqual(needs._bot_name(self.home, "p"), "Alice")
        st = self.path.stat()
        other = self.profile_dir / "profile.new"
        other.write_text(self.yaml("Brian"))
        os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(other, self.path)
        self.assertEqual(self.path.stat().st_size, st.st_size)
        self.assertEqual(self.path.stat().st_mtime_ns, st.st_mtime_ns)
        self.assertEqual(needs._bot_name(self.home, "p"), "Brian")

    def test_invalid_utf8_does_not_discard_the_chat_collection(self):
        self.path.write_bytes(b"\xff\xfe\x00 not yaml")
        self.assertEqual(needs._bot_name(self.home, "p"), "p")

    def test_the_title_must_sit_under_ui_meta(self):
        """01-050: a hermes-bots section under another parent is not the bot title."""
        self.path.write_text(
            "plugins:\n  hermes-bots:\n    title: Wrong\nui_meta:\n  hermes-bots:\n    title: Right\n"
        )
        self.assertEqual(needs._bot_name(self.home, "p"), "Right")
        self.path.write_text("plugins:\n  hermes-bots:\n    title: Wrong\n")
        needs._BOT_NAME_CACHE.clear()
        self.assertEqual(needs._bot_name(self.home, "p"), "p")


class FiledRecordsAndAsk(OutsideFixture):
    def args(self, **extra):
        return {"kind": "draft", "title": "t", "body": "b", "act_where": "w", "dedupe_key": "k", **extra}

    def test_an_unreadable_filed_record_is_not_treated_as_absent(self):
        """Sweep extra (class G): a corrupt record must not let a duplicate be filed."""
        d = needs._outside_dir(self.home)
        (d / ("a" * 32 + ".json")).write_text("{not json")
        with self.assertRaises(RuntimeError):
            needs.file_outside(self.args(), self.home)
        self.assertEqual(len(list(d.glob("*.json"))), 1)

    def test_an_unreadable_matched_record_is_not_overwritten(self):
        first = needs.file_outside(self.args(), self.home)
        record = needs._outside_dir(self.home) / (first["id"].rsplit(":", 1)[1] + ".json")
        before = record.read_text()
        with patch.object(needs, "MAX_STATE_BYTES", 10):
            with self.assertRaises(RuntimeError):
                needs.file_outside(self.args(body="b2"), self.home)
        self.assertEqual(record.read_text(), before)

    def test_an_ask_turn_is_cut_at_the_api_limit_not_before(self):
        """Sweep extra (class E): a turn the API accepts is not cut to 8000 characters."""
        spec = importlib.util.spec_from_file_location("ub_api_r4b", ROOT / "dashboard/plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        self.assertEqual(needs.ASK_TURN_LIMIT, api.MAX_TEXT)
        question = "q" * 15_000
        msgs = needs.build_ask_messages({"title": "t"}, [{"role": "user", "content": question}], turns=6)
        self.assertEqual(msgs[-1]["content"], question)


class StateDocs(unittest.TestCase):
    def test_docs_describe_the_state_rules(self):
        arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("never follow a link", arch)
        self.assertIn("16 MiB", arch)
        self.assertIn("a new marker is refused and reported when full", arch)
        self.assertIn("new comment", readme)


if __name__ == "__main__":
    unittest.main()
