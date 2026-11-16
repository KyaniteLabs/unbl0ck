"""#73: an answer is placed by pairing each comment with its own event, not by length or a row cap."""

import unittest

from test_r4_stack01d_kanban import HERMES_ERROR, KanbanAnswerCase, kanban_db, needs


@unittest.skipIf(HERMES_ERROR, f"Hermes unavailable: {HERMES_ERROR}")
class AnAnswerIsPairedWithItsEvent(KanbanAnswerCase):
    def block(self, task):
        with self.conn() as c:
            return needs._latest_block(c, kanban_db, task)

    def test_an_answer_stays_answered_after_many_later_worker_comments(self):
        task = self.card()
        block = self.block(task)
        with self.conn() as c:
            kanban_db.add_comment(c, task, "operator", "Approved")
            for i in range(needs.PLACEMENT_ROWS + 1):
                kanban_db.add_comment(c, task, "worker", f"progress {i}")
        with self.conn() as c:
            self.assertTrue(needs._answered_since(c, kanban_db, task, "operator", block.id))
            self.assertTrue(needs._answered_with(c, kanban_db, task, "operator", block.id, "Approved"))

    def test_a_padded_answer_is_found_although_kanban_logged_its_unstripped_length(self):
        task = self.card()
        block = self.block(task)
        with self.conn() as c:
            kanban_db.add_comment(c, task, "operator", "  Approved  ")
        with self.conn() as c:
            self.assertTrue(needs._answered_with(c, kanban_db, task, "operator", block.id, "  Approved  "))


if __name__ == "__main__":
    unittest.main()
