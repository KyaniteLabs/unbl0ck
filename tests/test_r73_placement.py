"""#73: an answer is placed by pairing each comment with its own event, not by length or a row cap."""

import time
import unittest
from unittest.mock import patch

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
            for i in range(501):
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

    def test_a_same_length_comment_before_the_block_is_not_the_answer(self):
        task = self.card()
        with self.conn() as c:
            kanban_db.add_comment(c, task, "operator", "Yes")  # before the current block
            kanban_db.unblock_task(c, task)
        self.reblock(task)
        block = self.block(task)
        with self.conn() as c:
            kanban_db.add_comment(c, task, "operator", "Nop")
        with self.conn() as c:
            self.assertTrue(needs._answered_with(c, kanban_db, task, "operator", block.id, "Nop"))
            self.assertFalse(needs._answered_with(c, kanban_db, task, "operator", block.id, "Yes"))

    def test_an_unlogged_triage_note_does_not_unplace_the_answer(self):
        """#73 review: Kanban writes a triage card's "Specified" note with no ``commented`` event."""
        task = self.card()
        block = self.block(task)
        with self.conn() as c:
            kanban_db._insert_comment(
                c, task, "operator", "Specified \u2014 updated title and promoted to todo.", int(time.time()))
            c.commit()
            kanban_db.add_comment(c, task, "operator", "Approved")
            for i in range(501):
                kanban_db.add_comment(c, task, "worker", f"progress {i}")
        with self.conn() as c:
            self.assertTrue(needs._answered_since(c, kanban_db, task, "operator", block.id))
            self.assertTrue(needs._answered_with(c, kanban_db, task, "operator", block.id, "Approved"))
            self.assertFalse(needs._answered_with(c, kanban_db, task, "operator", block.id, "Rejected"))

    def test_a_card_past_the_work_budget_reports_incomplete_coverage(self):
        """#73 review: a placement check reads at most PLACEMENT_WORK rows and says when it stopped."""
        task = self.card()
        block = self.block(task)
        with self.conn() as c:
            for i in range(5):
                kanban_db.add_comment(c, task, "worker", f"progress {i}")
            kanban_db.add_comment(c, task, "operator", "Approved")
        with self.conn() as c, patch.object(needs, "PLACEMENT_WORK", 3):
            with self.assertRaises(needs.PlacementIncomplete):
                needs._answered_since(c, kanban_db, task, "operator", block.id)
            with self.assertRaises(needs.PlacementIncomplete):
                needs._answered_with(c, kanban_db, task, "operator", block.id, "Approved")

    def test_a_logged_comment_shaped_like_a_kanban_note_is_still_paired(self):
        """#73 review: an operator comment that starts like a note Kanban writes unlogged is logged,
        and an answer from before the block is not this block's answer."""
        task = self.card()
        with self.conn() as c:
            kanban_db.add_comment(c, task, "operator", "Approved")  # before the current block
            kanban_db.unblock_task(c, task)
        self.reblock(task)
        block = self.block(task)
        with self.conn() as c:
            kanban_db.add_comment(c, task, "operator", "Decomposed into two options; choose B")
        with self.conn() as c:
            self.assertFalse(needs._answered_with(c, kanban_db, task, "operator", block.id, "Approved"))
            self.assertTrue(needs._answered_with(
                c, kanban_db, task, "operator", block.id, "Decomposed into two options; choose B"))

    def test_unlogged_notes_after_the_answer_neither_hide_it_nor_pair_an_older_answer(self):
        """#73 review: every note Kanban writes with no event, written after the answer, leaves the
        answer placed and an answer from before the block unplaced."""
        task = self.card()
        with self.conn() as c:
            kanban_db.add_comment(c, task, "operator", "Rejected")  # before the current block
            kanban_db.unblock_task(c, task)
        self.reblock(task)
        block = self.block(task)
        notes = (
            "Specified \u2014 updated title and promoted to todo.",
            "Decomposed into 2 child card(s).",
            "reconciliation: card was 'running' with no valid claim; reset to ready.",
            "Invalidated: ancestor card was reopened.",
        )
        with self.conn() as c:
            kanban_db.add_comment(c, task, "operator", "Approved")
            for note in notes:
                kanban_db._insert_comment(c, task, "operator", note, int(time.time()))
            c.commit()
        with self.conn() as c:
            self.assertTrue(needs._answered_since(c, kanban_db, task, "operator", block.id))
            self.assertTrue(needs._answered_with(c, kanban_db, task, "operator", block.id, "Approved"))
            self.assertFalse(needs._answered_with(c, kanban_db, task, "operator", block.id, "Rejected"))


if __name__ == "__main__":
    unittest.main()
