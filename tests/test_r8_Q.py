"""Round-8 lane Q: the bounded comment and chat reads, where an answer falls, and the comment signature."""

import json
import unittest

from test_r4_stack01d_kanban import HERMES_ERROR, KanbanAnswerCase, kanban_db, needs


@unittest.skipIf(HERMES_ERROR, f"Hermes unavailable: {HERMES_ERROR}")
class Placement(KanbanAnswerCase):
    def test_an_unlogged_comment_from_before_the_block_is_not_this_blocks_answer(self):
        """R8-004: an answer is a comment whose own logged event follows the block; comments are not
        counted against events, so an unlogged earlier comment is never taken for the answer."""
        with self.conn() as c:
            task = kanban_db.create_task(c, title="Choose", body="", assignee="worker")
            task = getattr(task, "id", task)
            kanban_db.add_comment(c, task, "operator", "Old")
            c.execute(
                "INSERT INTO task_comments(task_id, author, body, created_at) VALUES(?, 'operator', 'No', 1)", (task,)
            )
            c.commit()
            kanban_db.block_task(c, task, reason="Needs CEO approval", kind="needs_input")
            block = needs._latest_block(c, kanban_db, task)
            kanban_db.add_comment(c, task, "operator", "Approved")
            self.assertFalse(needs._answered_with(c, kanban_db, task, "operator", block.id, "No"))
            kanban_db.add_comment(c, task, "operator", "Ok")  # logged with the same length as "No"
            self.assertFalse(needs._answered_with(c, kanban_db, task, "operator", block.id, "No"))
            self.assertTrue(needs._answered_with(c, kanban_db, task, "operator", block.id, "Ok"))
            self.assertTrue(needs._answered_with(c, kanban_db, task, "operator", block.id, "Approved"))

    def test_a_queue_poll_does_not_read_the_comment_event_history(self):
        """R8-005: commented events from before the block, with large payloads, are not read."""
        with self.conn() as c:
            task = kanban_db.create_task(c, title="Choose", body="", assignee="worker")
            task = getattr(task, "id", task)
            c.executemany(
                "INSERT INTO task_events(task_id, kind, payload, created_at) VALUES(?, 'commented', ?, 1)",
                [(task, json.dumps({"author": "w" * 100_000, "len": 1})) for _ in range(200)],
            )
            c.commit()
            kanban_db.block_task(c, task, reason="Needs CEO approval", kind="needs_input")
            block = needs._latest_block(c, kanban_db, task)
            read = [0]

            def measure(cursor, row):
                read[0] += sum(len(v) for v in row if isinstance(v, (str, bytes)))
                return tuple(row)

            c.row_factory = measure
            self.assertFalse(needs._answered_since(c, kanban_db, task, "operator", block.id))
        self.assertLess(read[0], 1_000_000)


if __name__ == "__main__":
    unittest.main()
