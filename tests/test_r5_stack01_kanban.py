"""Round-5 findings for answering a Kanban card (stack 01), on the real Hermes Kanban code path."""

import threading
import unittest
import unittest.mock

from test_r4_stack01d_kanban import HERMES_ERROR, KanbanAnswerCase, kanban_db, needs


@unittest.skipIf(HERMES_ERROR, f"Hermes unavailable: {HERMES_ERROR}")
class ACommentIsPlacedByItsEventNotByTheClock(KanbanAnswerCase):
    def set_comment_time(self, task, when):
        with self.conn() as c:
            c.execute("UPDATE task_comments SET created_at = ? WHERE task_id = ?", (when, task))
            c.commit()

    def block(self, task):
        with self.conn() as c:
            return needs._latest_block(c, kanban_db, task)

    def test_an_earlier_comment_with_a_later_clock_does_not_answer_the_new_block(self):
        """R5-needs-015 (class A): clock rollback must not turn an old answer into a new one."""
        task = self.card()
        with self.conn() as c:
            kanban_db.add_comment(c, task, "operator", "old answer")
            kanban_db.unblock_task(c, task)
        self.reblock(task)
        block = self.block(task)
        self.set_comment_time(task, int(block.created_at) + 1)  # the clock stepped back
        with self.conn() as c:
            self.assertFalse(
                needs._answered_since(c, kanban_db, task, "operator", block.created_at, block.id)
            )
            self.assertEqual(
                needs._comments_since_block(c, kanban_db, task, block.created_at, block.id), []
            )

    def test_a_later_comment_with_an_earlier_clock_still_answers_the_block(self):
        task = self.card()
        block = self.block(task)
        with self.conn() as c:
            kanban_db.add_comment(c, task, "operator", "new answer")
        self.set_comment_time(task, int(block.created_at) - 5)
        with self.conn() as c:
            self.assertTrue(
                needs._answered_since(c, kanban_db, task, "operator", block.created_at, block.id)
            )
            self.assertEqual(
                needs._comments_since_block(c, kanban_db, task, block.created_at, block.id),
                [("operator", "new answer")],
            )

    def test_a_comment_with_no_event_keeps_the_time_rule(self):
        task = self.card()
        block = self.block(task)
        with self.conn() as c:
            c.execute(
                "INSERT INTO task_comments(task_id, author, body, created_at) VALUES(?,?,?,?)",
                (task, "operator", "imported", int(block.created_at) + 10),
            )
            c.commit()
            self.assertTrue(
                needs._answered_since(c, kanban_db, task, "operator", block.created_at, block.id)
            )


@unittest.skipIf(HERMES_ERROR, f"Hermes unavailable: {HERMES_ERROR}")
class AnotherCallersRecoveryMarkerSurvivesThisCallsCleanup(KanbanAnswerCase):
    def test_a_failed_first_caller_does_not_delete_the_marker_a_second_caller_needs(self):
        """R5-needs-013 (class B): the owner of a reservation removes it only when no one else
        relies on it."""
        task = self.card()
        version = self.version(task)
        from hermes_cli import kanban_db_connect as kbc  # type: ignore

        real_connect = kbc.connect
        first_in = threading.Event()
        second_done = threading.Event()
        outcome = {}

        def connect(*args, **kwargs):
            if threading.current_thread().name == "first":
                first_in.set()
                second_done.wait(10)
                raise OSError("first caller cannot connect")
            return real_connect(*args, **kwargs)

        def first():
            try:
                needs.answer_card("default", task, "A", release=True, version=version, home=self.home)
            except Exception as exc:  # noqa: BLE001
                outcome["first"] = type(exc).__name__

        # Second caller posts its answer, then its release fails: the marker is what retries it.
        with unittest.mock.patch.object(kbc, "connect", connect), unittest.mock.patch.object(
            needs, "_unblock_task_at_version", lambda *a, **kw: False
        ):
            t = threading.Thread(target=first, name="first")
            t.start()
            self.assertTrue(first_in.wait(10))
            try:
                needs.answer_card("default", task, "B", release=True, version=version, home=self.home)
            except Exception as exc:  # noqa: BLE001
                outcome["second"] = type(exc).__name__
            second_done.set()
            t.join(10)
        pending = needs.load_pending_releases(self.home)
        self.assertEqual(pending.get(f"kanban:default:{task}"), version, outcome)


if __name__ == "__main__":
    unittest.main()
