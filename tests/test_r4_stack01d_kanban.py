"""Round-4 findings 01-079 .. 01-082 and 01-087: answering a Kanban card and releasing it.
Each test is the finding's reproducer on the real Hermes Kanban code path."""

import importlib
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from test_outside import needs

try:
    kanban_db = importlib.import_module("hermes_cli.kanban_db")
    kanban_db_connect = importlib.import_module("hermes_cli.kanban_db_connect")
    HERMES_ERROR = None
except ImportError as exc:  # pragma: no cover
    kanban_db = kanban_db_connect = None  # importers still load; their Kanban cases skip
    HERMES_ERROR = str(exc)


@unittest.skipIf(HERMES_ERROR, f"Hermes unavailable: {HERMES_ERROR}")
class KanbanAnswerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.dict(
            os.environ, {"HERMES_HOME": self.tmp.name, "HERMES_KANBAN_HOME": self.tmp.name}
        )
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(
            needs, "_settings", lambda: {"principal_name": "CEO", "comment_author": "operator"}
        )
        p.start()
        self.addCleanup(p.stop)
        self.home = Path(self.tmp.name)

    def conn(self):
        return kanban_db_connect.connect(board="default")

    def card(self, conn=None):
        def make(c):
            task = kanban_db.create_task(c, title="Choose a database", body="", assignee="worker")
            task = getattr(task, "id", task)
            kanban_db.block_task(c, task, reason="Needs CEO approval", kind="needs_input")
            return task

        if conn is not None:
            return make(conn)
        with self.conn() as c:
            return make(c)

    def version(self, task):
        with self.conn() as c:
            return str(needs._latest_block(c, kanban_db, task).id)

    def reblock(self, task):
        with self.conn() as c:
            kanban_db.block_task(c, task, reason="Needs a new decision", kind="capability")

    def bodies(self, task):
        with self.conn() as c:
            return [x.body for x in kanban_db.list_comments(c, task)]

    def status(self, task):
        with self.conn() as c:
            return str(getattr(kanban_db.get_task(c, task).status, "value", kanban_db.get_task(c, task).status))

    def answer(self, task, text, *, release, version=None):
        return needs.answer_card(
            "default", task, text, release=release,
            version=version if version is not None else self.version(task), home=self.home,
        )


class HistoricalAnswersAreNotDuplicates(KanbanAnswerCase):
    def test_an_earlier_blocks_identical_answer_does_not_suppress_the_new_comment(self):
        """01-079 (class J): dedupe looks only at comments made since the current block."""
        task = self.card()
        self.answer(task, "A", release=True)  # block 1 answered "A" and released
        self.reblock(task)
        self.answer(task, "B", release=False)  # block 2 answered "B", kept blocked
        result = self.answer(task, "A", release=True)  # block 2 now answered "A" and released
        self.assertTrue(result["released"])
        self.assertIsNotNone(result["comment_id"])
        self.assertEqual(self.bodies(task), ["A", "B", "A"])

    def test_a_retry_after_a_failed_release_still_posts_once(self):
        task = self.card()
        version = self.version(task)
        with patch.object(needs, "_unblock_task_at_version", return_value=False):
            with self.assertRaises(needs.UnblockFailed):
                self.answer(task, "Go", release=True, version=version)
        result = self.answer(task, "Go", release=True, version=version)
        self.assertTrue(result["released"])
        self.assertIsNone(result["comment_id"])
        self.assertEqual(self.bodies(task), ["Go"])

    def test_the_same_second_comment_before_a_reblock_is_not_this_blocks_answer(self):
        task = self.card()
        with self.conn() as c:
            kanban_db.add_comment(c, task, author="operator", body="A")  # before the re-block
            kanban_db.unblock_task(c, task)
        self.reblock(task)
        self.answer(task, "B", release=False)
        result = self.answer(task, "A", release=True)
        self.assertTrue(result["released"])
        self.assertEqual(self.bodies(task), ["A", "B", "A"])


class AnswerAndPublicReleaseTransactions(KanbanAnswerCase):
    def test_reblock_after_comment_commit_is_reported_as_failed_release(self):
        """A post-commit reblock keeps the answer, but cannot claim release."""
        task = self.card()
        version = self.version(task)
        original = kanban_db.write_txn
        state = {"top": 0, "done": False}

        @contextmanager
        def interleave(conn, *, allow_nested=False):
            top = not conn.in_transaction
            if top:
                state["top"] += 1
            with original(conn, allow_nested=allow_nested):
                yield conn
            if top and state["top"] == 1 and not state["done"]:
                state["done"] = True
                with self.conn() as other:
                    self.assertTrue(kanban_db.unblock_task(other, task))
                self.reblock(task)

        with patch.object(kanban_db, "write_txn", interleave):
            with self.assertRaises(needs.UnblockFailed) as failure:
                self.answer(task, "Use the first", release=True, version=version)
        self.assertTrue(state["done"])
        self.assertIsInstance(failure.exception.__cause__, needs.StaleAnswer)
        self.assertEqual(self.bodies(task), ["Use the first"])
        self.assertEqual(needs.load_pending_releases(self.home), {f"kanban:default:{task}": version})
        self.assertEqual(self.status(task), "blocked")

    def test_a_stale_version_writes_nothing(self):
        task = self.card()
        old = self.version(task)
        with self.conn() as c:
            kanban_db.unblock_task(c, task)
        self.reblock(task)
        with self.assertRaises(needs.StaleAnswer):
            self.answer(task, "Late", release=True, version=old)
        self.assertEqual(self.bodies(task), [])


class ReleaseFailuresAreReconciled(KanbanAnswerCase):
    def test_a_release_that_raises_is_a_failed_release_with_a_marker(self):
        """01-081 (class G): an exception in the unblock is reconciled like a False return."""
        task = self.card()
        version = self.version(task)
        item = f"kanban:default:{task}"
        with patch.object(
            needs, "_unblock_task_at_version", side_effect=sqlite3.OperationalError("database is locked")
        ):
            with self.assertRaises(needs.UnblockFailed) as ctx:
                self.answer(task, "Go", release=True, version=version)
        self.assertIn("still blocked", str(ctx.exception))
        self.assertEqual(needs.load_pending_releases(self.home), {item: version})
        self.assertEqual(self.bodies(task), ["Go"])
        (card,) = [i for i in needs.kanban_items(boards=["default"]) if i["task_id"] == task]
        self.assertEqual(card["tier"], "release_failed")

    def test_a_broken_native_helper_is_a_failed_release_not_a_generic_error(self):
        task = self.card()
        version = self.version(task)
        with patch.object(kanban_db, "unblock_task", side_effect=AttributeError("gone")):
            with self.assertRaises(needs.UnblockFailed):
                self.answer(task, "Go", release=True, version=version)
        self.assertEqual(self.status(task), "blocked")
        self.assertEqual(self.bodies(task), ["Go"])
        (card,) = [i for i in needs.kanban_items(boards=["default"]) if i["task_id"] == task]
        self.assertEqual(card["tier"], "release_failed")
        # and the retry releases without a second comment
        result = self.answer(task, "Go", release=True, version=version)
        self.assertTrue(result["released"])
        self.assertEqual(self.bodies(task), ["Go"])


class RecoveryBookkeepingAfterSuccess(KanbanAnswerCase):
    def test_a_cleanup_failure_after_a_successful_release_still_reports_success(self):
        """01-082 (class G): the card is released; a marker cleanup error must not hide the receipt."""
        task = self.card()
        version = self.version(task)
        real = needs._set_pending_release

        def flaky(item_id, ver, home=None, **kw):
            if ver is None:
                raise TimeoutError("state lock busy")
            return real(item_id, ver, home, **kw)

        with patch.object(needs, "_set_pending_release", flaky):
            result = self.answer(task, "Go", release=True, version=version)
        self.assertTrue(result["released"])
        self.assertIsNotNone(result["comment_id"])
        self.assertNotEqual(self.status(task), "blocked")

    def test_an_oserror_on_cleanup_is_also_survived(self):
        task = self.card()
        version = self.version(task)
        real = needs._set_pending_release

        def flaky(item_id, ver, home=None, **kw):
            if ver is None:
                raise OSError("disk full")
            return real(item_id, ver, home, **kw)

        with patch.object(needs, "_set_pending_release", flaky):
            self.assertTrue(self.answer(task, "Go", release=True, version=version)["released"])


class RecoveryMarkerIsReservedFirst(KanbanAnswerCase):
    def test_a_marker_that_cannot_be_reserved_refuses_before_anything_is_written(self):
        """01-087 (class G): the recovery marker is reserved before the comment, so a lock held
        by another writer cannot leave an answered card without its recovery state."""
        task = self.card()
        version = self.version(task)
        held, go = threading.Event(), threading.Event()

        def holder():
            with needs._state_lock(self.home):
                held.set()
                go.wait(5)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        self.assertTrue(held.wait(5))
        try:
            with patch.object(needs, "STATE_LOCK_TIMEOUT", 0.2):
                with self.assertRaises(TimeoutError):
                    self.answer(task, "Go", release=True, version=version)
        finally:
            go.set()
            t.join(5)
        self.assertEqual(self.bodies(task), [])
        self.assertEqual(self.status(task), "blocked")
        (card,) = [i for i in needs.kanban_items(boards=["default"]) if i["task_id"] == task]
        self.assertEqual(card["tier"], "ask")

    def test_a_failed_release_keeps_the_reserved_marker(self):
        task = self.card()
        version = self.version(task)
        with patch.object(needs, "_unblock_task_at_version", return_value=False):
            with self.assertRaises(needs.UnblockFailed):
                self.answer(task, "Go", release=True, version=version)
        self.assertEqual(needs.load_pending_releases(self.home), {f"kanban:default:{task}": version})

    def test_a_successful_release_leaves_no_marker(self):
        task = self.card()
        self.answer(task, "Go", release=True)
        self.assertEqual(needs.load_pending_releases(self.home), {})

    def test_a_release_refused_after_posting_keeps_answer_and_marker(self):
        task = self.card()
        version = self.version(task)
        with patch.object(needs, "_unblock_task_at_version", side_effect=needs.StaleAnswer("newer")):
            with self.assertRaises(needs.UnblockFailed):
                self.answer(task, "Go", release=True, version=version)
        self.assertEqual(needs.load_pending_releases(self.home), {f"kanban:default:{task}": version})
        self.assertEqual(self.bodies(task), ["Go"])

    def test_a_refused_retry_keeps_the_marker_of_the_earlier_failed_release(self):
        task = self.card()
        version = self.version(task)
        with patch.object(needs, "_unblock_task_at_version", return_value=False):
            with self.assertRaises(needs.UnblockFailed):
                self.answer(task, "Go", release=True, version=version)
        with patch.object(needs, "_unblock_task_at_version", side_effect=needs.StaleAnswer("x")):
            with self.assertRaises(needs.UnblockFailed):
                self.answer(task, "Go", release=True, version=version)
        self.assertEqual(needs.load_pending_releases(self.home), {f"kanban:default:{task}": version})


class AnswerDocs(unittest.TestCase):
    def test_the_architecture_guide_describes_public_release_race(self):
        """01-080/01-081/01-087 (class I): the documented contract is the implemented one."""
        text = (Path(__file__).resolve().parents[1] / "docs" / "architecture.md").read_text()
        self.assertIn("one Kanban write transaction", text)
        self.assertIn("narrow race", text)
        self.assertIn("not an", text)
        self.assertIn("with no comment", text)
        self.assertIn("reserves its marker before the answer is written", text)
        self.assertIn("only at comments made since the current block", text)


if __name__ == "__main__":
    unittest.main()
