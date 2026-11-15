"""Round-5 lane N regression tests: one per finding, each driven through the entry point the
pre-round-5 code already had, so it fails there for the behavioural reason."""

import contextlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_r4_stack11 as s11
from r4c_common import ASK
from test_outside import OutsideFixture, needs
from test_r4_stack01d_kanban import HERMES_ERROR, KanbanAnswerCase, kanban_db
from _redaction import assert_redacted

STEM = "0123456789abcdef0123456789abcdef"


class Empty:
    def list_pending(self):
        return []


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.data = needs.data_dir(self.home)


class Backfill(s11.QueueCase):
    def _home(self, rows):
        from test_ask_coverage import make_chats

        return make_chats(Path(tempfile.mkdtemp(dir=self.home)), rows)

    def _queue(self, home, days):
        from test_ask_coverage import settings

        with patch.object(needs, "_settings", settings(chat_window_days=days)):
            return {i["session_id"]: i for i in needs.build_queue(home)["items"] if i["source"] == "chat"}

    def test_a_backfilled_chat_stays_flagged_after_a_narrower_refresh(self):
        """R5-needs-005: a narrow refresh that no longer lists a backfilled chat must not unflag it."""
        home = self._home([("recent", "cli", "dm", "u", 0.2, ASK), ("three", "cli", "dm", "u", 3, ASK)])
        self._queue(home, 1)
        self.assertTrue(self._queue(home, 7)["three"].get("backfilled"))
        self._queue(home, 1)
        self.assertTrue(self._queue(home, 7)["three"].get("backfilled"))

    def test_every_backfilled_chat_keeps_its_flag_however_many_there_are(self):
        """R5-needs-006: no backfilled chat is evicted by a cap on the remembered set."""
        home = self._home([("two", "cli", "dm", "u", 2, ASK), ("three", "cli", "dm", "u", 3, ASK)])
        with patch.object(needs, "BACKFILL_IDS_MAX", 1, create=True):  # the old cap, made small
            self._queue(home, 1)
            self._queue(home, 7)
            again = self._queue(home, 7)
        self.assertEqual(sorted(k for k, v in again.items() if v.get("backfilled")), ["three", "two"])

    def test_the_backfill_marks_are_written_under_the_state_lock(self):
        """R5-needs-007: the backfill read-modify-write runs inside the state lock."""
        home = self._home([("three", "cli", "dm", "u", 3, ASK)])
        real_lock, real_write = needs._state_lock, needs._write_json
        depth = [0]
        writes = []

        @contextlib.contextmanager
        def counting(h):
            with real_lock(h):
                depth[0] += 1
                try:
                    yield
                finally:
                    depth[0] -= 1

        def spy(p, *a, **kw):
            writes.append((Path(p).name, depth[0] > 0))
            return real_write(p, *a, **kw)

        with patch.object(needs, "_state_lock", counting), patch.object(needs, "_write_json", spy):
            self._queue(home, 1)
            self._queue(home, 7)
        marks = [held for name, held in writes if name == needs.BACKFILL_FILE]
        self.assertTrue(marks)
        self.assertTrue(all(marks), writes)


class ReadPathsDoNotWrite(s11.QueueCase):
    def test_hiding_a_card_keeps_the_summaries_of_other_cards(self):
        """R5-needs-018: the hide lookup builds the queue without pruning the summary cache."""
        needs.hide("kanban:b:t", "0", self.home)
        needs.save_bluf("kanban:b:other", "1", "Decide", "Stuck", self.home)
        needs.hide("kanban:b:t", "1", self.home)  # a dismissal for another version looks the card up
        self.assertIn("kanban:b:other", needs.load_blufs(self.home))


class CredentialDigits(unittest.TestCase):
    def test_a_token_key_keeps_its_digits_private(self):
        """R5-needs-003: numeric credentials under a token or auth key are judged by the exact-key classifier."""
        for key in ("auth_token", "refresh_token", "session_token"):
            with self.subTest(key=key):
                assert_redacted(needs._ask_redact(f"{key}=483920"), "483920")
                self.assertNotIn("483920", json.dumps(needs._ask_card_view({key: "483920"})))


class Completeness(OutsideFixture):
    def test_a_pack_with_unread_older_chats_is_not_complete(self):
        """R5-needs-009: completeness counts the coverage gaps too."""
        queue = {"items": [], "errors": [], "coverage": {"chat": {"sessions_omitted": 101}}}
        self.assertFalse(needs.pocket_pack(queue)["complete"])

    def test_an_unreadable_file_counts_as_an_unread_source_whatever_its_name(self):
        """R5-needs-032: a failure is classified by its kind, never by words in a file name."""
        self.watch()
        f = self.folder / "could not remove.txt"
        f.write_text("approve it")
        f.chmod(0)
        self.addCleanup(f.chmod, 0o600)
        queue = needs.build_queue()
        self.assertGreaterEqual(needs.unread_sources(queue["errors"]), 1, queue["errors"])
        self.assertFalse(needs.pocket_pack(queue)["complete"])


class Dismissals(OutsideFixture):
    def test_resolving_one_filed_item_keeps_the_others_undo_tokens(self):
        """R5-needs-010: the resolve drops one marker without rewriting the rest through a lossy reader."""
        a = self.file(dedupe_key="a", title="A")
        b = self.file(dedupe_key="b", title="B")
        items = {i["id"]: i for i in needs.build_queue()["items"]}
        needs.hide(a["id"], items[a["id"]]["version"])
        token_b = needs.hide(b["id"], items[b["id"]]["version"])
        needs.file_outside({"action": "resolve", "id": a["id"], "version": a["version"]}, home=self.home)
        self.assertTrue(needs.unhide(b["id"], token=token_b))

    def test_an_undo_whose_second_write_fails_can_be_retried_with_the_same_token(self):
        """R5-needs-011: the store the token names is written last."""
        result = self.file(dedupe_key="a")
        (it,) = needs.build_queue()["items"]
        needs.done_outside(it["id"], it["version"])
        token = needs.hide(it["id"], it["version"])
        record, real = self.record(result), needs._write_json

        def failing(p, *a, **kw):
            if Path(p) == record:
                raise OSError("disk full")
            return real(p, *a, **kw)

        with patch.object(needs, "_write_json", failing), self.assertRaises(OSError):
            needs.unhide(it["id"], token=token)
        self.assertTrue(needs.unhide(it["id"], token=token))
        self.assertEqual([i["id"] for i in needs.build_queue()["items"]], [it["id"]])

    def test_a_folder_dismissal_without_a_content_digest_hides_nothing(self):
        """R5-needs-019: time and size alone never suppress a watched file."""
        self.watch()
        f = self.folder / "a.txt"
        f.write_text("delete all")
        st = f.stat()
        (it,) = needs.outside_items(with_hidden=True)
        (needs.data_dir(self.home) / "hidden.json").write_text(json.dumps({it["id"]: f"{st.st_mtime_ns}:{st.st_size}"}))
        self.assertEqual([i["id"] for i in needs.outside_items(with_hidden=False)], [it["id"]])

    def test_undo_never_rewrites_a_record_this_plugin_did_not_file(self):
        """R5-needs-024: a filed record is loaded by id only through the ownership check."""
        f = needs._outside_dir(self.home) / f"{STEM}.json"
        f.write_text(json.dumps({"version": "v", "done_at": 1, "done_token": "t"}))
        before = f.read_text()
        with contextlib.suppress(needs.SourceUnavailable):
            self.assertFalse(needs.unhide(f"outside:filed:{STEM}", token="t"))
        self.assertEqual(f.read_text(), before)

    def test_the_filed_scan_charges_the_read_budget(self):
        """R5-needs-025: every filed record read is charged to the read budget."""
        for key in "abc":
            self.file(dedupe_key=key, title=key)
        unread = [0]
        items = needs.outside_items(read_budget=1, unread=unread)
        self.assertEqual((len(items), unread[0]), (1, 2))

    def test_retention_cleanup_charges_entries_that_are_not_records(self):
        """R5-needs-026: the purge bound counts every directory entry, not only .json ones."""
        self.file(dedupe_key="real")
        d = needs.data_dir(self.home) / "outside"
        for n in range(40):
            (d / f"note-{n:03d}.txt").write_text("not a record")
        seen = [0]
        real = os.scandir

        class Counting:
            def __init__(self, it):
                self.it = it

            def __enter__(self):
                self.it.__enter__()
                return self

            def __exit__(self, *a):
                return self.it.__exit__(*a)

            def __iter__(self):
                return self

            def __next__(self):
                entry = next(self.it)
                seen[0] += 1
                return entry

        errors = []
        with patch.object(needs, "_PURGE_SCAN_LIMIT", 5), patch.object(needs.os, "scandir", lambda p: Counting(real(p))):
            needs._purge_done(d, errors=errors)
        self.assertLessEqual(seen[0], 6)


class StaleTokens(Home):
    def test_a_newer_dismissal_that_lands_before_the_lock_is_kept(self):
        """R5-needs-012: the stored value is judged under the lock that guards the write."""
        real_lock = needs._state_lock
        raced = [False]

        @contextlib.contextmanager
        def racing(home):
            if not raced[0]:
                raced[0] = True
                needs.hide("kanban:b:t", "2", home)  # another tab, between the lookup and the lock
            with real_lock(home):
                yield

        queue = lambda *a, **kw: {"items": [{"id": "kanban:b:t", "version": "2" if raced[0] else "1"}]}  # noqa: E731
        with patch.object(needs, "_state_lock", racing), patch.object(needs, "build_queue", queue):
            with contextlib.suppress(needs.StaleHide):
                needs.hide("kanban:b:t", "1", self.home)
        self.assertEqual(needs.load_hidden(self.home), {"kanban:b:t": "2"})

    def test_an_older_request_for_the_same_version_does_not_replace_the_newer_summary(self):
        """R5-needs-014: start order decides even when the version is equal."""
        needs.save_bluf("a", "1", "newer", "w", self.home, started_at=20.0, fp="new")
        needs.save_bluf("a", "1", "older", "w", self.home, started_at=10.0, fp="old")
        self.assertEqual(needs.load_blufs(self.home)["a"]["bluf"], "newer")


class MalformedRecords(Home):
    def test_an_overflowing_time_is_a_malformed_record_not_an_exception(self):
        """R5-needs-021: a JSON integer past the float range is not a time."""
        self.assertIsNone(needs._done_at({"done_at": 10**400}))
        self.assertEqual(needs._num(10**400), 0.0)

    def test_a_deeply_nested_state_file_is_damage_not_a_crash(self):
        """R5-needs-022: RecursionError from the parser is one damaged record."""
        folder = self.home / "platforms" / "pairing"
        folder.mkdir(parents=True)
        (folder / "telegram-pending.json").write_text("[" * 100_000 + "]" * 100_000)
        errors = []
        self.assertEqual(needs.pairing_items(self.home, errors=errors, store=lambda p: Empty()), [])
        self.assertTrue(any(str(e).startswith("pairing") for e in errors), errors)

    def test_pruning_never_drops_a_summary_entry_of_another_shape(self):
        """R5-needs-023: the summary store reader checks every entry, so prune refuses to rewrite it."""
        path = self.data / "bluf.json"
        path.write_text(json.dumps({"x": 42}))
        with contextlib.suppress(RuntimeError):
            needs.prune_blufs([], self.home)
        self.assertEqual(json.loads(path.read_text()), {"x": 42})


class Links(Home):
    def test_the_lock_is_never_created_through_a_link_without_o_nofollow(self):
        """R5-needs-017: a dangling lock link creates nothing at its target."""
        outside = Path(self.tmp.name) / "outside-lock"
        (self.data / ".state.lock").symlink_to(outside)
        with patch.object(needs, "_NOFOLLOW", 0), contextlib.suppress(OSError, RuntimeError):
            needs.save_bluf("a", "1", "b", "w", self.home)
        self.assertFalse(outside.exists())


class Budgets(unittest.TestCase):
    def test_a_cut_card_field_with_its_marker_stays_within_the_budget(self):
        """R5-needs-045: the cut marker is counted in the room it is cut to."""
        budget = [5]
        view = needs._ask_card_view({"title": "x" * 100}, _text_budget=budget)
        self.assertLessEqual(len(view["title"]), 5)
        self.assertGreaterEqual(budget[0], 0)


@unittest.skipIf(HERMES_ERROR, f"Hermes unavailable: {HERMES_ERROR}")
class KanbanOrder(KanbanAnswerCase):
    def block(self, task):
        with self.conn() as c:
            return needs._latest_block(c, kanban_db, task)

    def test_a_comment_from_before_the_block_never_answers_it_whatever_its_clock(self):
        """R5-needs-015: event order places a comment, not its timestamp."""
        task = self.card()
        with self.conn() as c:
            kanban_db.add_comment(c, task, "operator", "old answer")
            kanban_db.unblock_task(c, task)
        self.reblock(task)
        block = self.block(task)
        with self.conn() as c:
            c.execute("UPDATE task_comments SET created_at = ? WHERE task_id = ?", (int(block.created_at) + 1, task))
            c.commit()
            self.assertFalse(needs._answered_since(c, kanban_db, task, "operator", block.id))

    def test_a_failed_callers_cleanup_keeps_the_marker_another_caller_stamped(self):
        """R5-needs-013: a marker is removed by its owner token, never by its value."""
        task = self.card()
        version = self.version(task)
        from hermes_cli import kanban_db_connect as kbc  # type: ignore

        real_connect = kbc.connect
        first_in, second_done = threading.Event(), threading.Event()

        def connect(*args, **kwargs):
            if threading.current_thread().name == "first":
                first_in.set()
                second_done.wait(10)
                raise OSError("first caller cannot connect")
            return real_connect(*args, **kwargs)

        def first():
            with contextlib.suppress(Exception):
                needs.answer_card("default", task, "A", release=True, version=version, home=self.home)

        with patch.object(kbc, "connect", connect), patch.object(needs, "_unblock_task_at_version", lambda *a, **kw: False):
            t = threading.Thread(target=first, name="first")
            t.start()
            self.assertTrue(first_in.wait(10))
            with contextlib.suppress(Exception):
                needs.answer_card("default", task, "B", release=True, version=version, home=self.home)
            second_done.set()
            t.join(10)
        self.assertEqual(needs.load_pending_releases(self.home).get(f"kanban:default:{task}"), version)


class StrengthenedTests(Home):
    def test_a_doubly_quoted_record_in_the_unscanned_remainder_is_withheld(self):
        """R5-needs-047: the input leaks unless the unscanned remainder is withheld."""
        text = "{x" * 129 + json.dumps(json.dumps({"name": "DB_PASSWORD", "value": "Zq81LmK4vP"}))
        assert_redacted(needs._ask_redact(text), "Zq81LmK4vP")

    def test_a_state_write_creates_nothing_through_a_link_where_o_nofollow_is_missing(self):
        """R5-needs-048: the write path's create is exclusive, so a planted link is refused, not followed."""
        victim = Path(self.tmp.name) / "victim"
        (self.data / ".state.lock").symlink_to(victim)
        with patch.object(needs, "_NOFOLLOW", 0), contextlib.suppress(OSError, RuntimeError):
            needs.hide("chat:p:s", "1", self.home)
        self.assertFalse(victim.exists())
        self.assertFalse((self.data / "hidden.json").exists())

    def test_a_linked_pairing_store_is_reported_not_read_through(self):
        """R5-needs-049: every state read goes through the pinned open, the pairing check too."""
        folder = self.home / "platforms" / "pairing"
        folder.mkdir(parents=True)
        real = Path(self.tmp.name) / "elsewhere.json"
        real.write_text("{}")
        (folder / "telegram-pending.json").symlink_to(real)

        errors = []
        needs.pairing_items(self.home, errors=errors, store=lambda p: Empty())
        self.assertTrue(any(str(e).startswith("pairing") for e in errors), errors)


class OversizedKey(OutsideFixture):
    def test_an_oversized_key_is_refused_by_the_one_field_check_for_both_actions(self):
        """R5-needs-050: the one field check refuses a long key; a cut key would resolve its prefix's record."""
        self.assertTrue(self.file(dedupe_key="x" * 120)["ok"])
        self.assertIn("error", self.file(dedupe_key="x" * 120 + "B"))
        with self.assertRaises(ValueError):
            needs.file_outside({"action": "resolve", "dedupe_key": "x" * 120 + "A"}, self.home)
        self.assertEqual(needs.build_queue()["count"], 1)


if __name__ == "__main__":
    unittest.main()
