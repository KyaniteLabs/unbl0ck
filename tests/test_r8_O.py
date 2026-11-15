"""Round-8 lane O: expiry of dropped live-prompt faults and dismissal-order recovery and width."""

import json
import unittest
from unittest.mock import patch

from r4c_common import HomeCase, needs


class OrderRecovery(HomeCase):
    def test_a_lost_counter_refuses_a_done_with_a_malformed_order(self):
        """R8-010: rebuilding a lost counter from a Done whose done_order is not an order refuses
        the dismissal instead of falling back to its time and reissuing an order."""
        outside = needs._outside_dir(self.home)
        (outside / "damaged.json").write_text(json.dumps({"done_order": "damaged", "done_at": 1000}))
        with patch.object(needs.time, "time", lambda: 1000.0):
            with self.assertRaisesRegex(RuntimeError, needs.ORDER_FILE):
                needs.hide_ordered("kanban:b:t", "1", self.home)
        self.assertFalse(needs.load_hidden(self.home))
        # Rewriting that Done (a clock step, an identical refile) keeps the bad order, so it still refuses.
        self.assertEqual(needs._done_marks({"done_order": "damaged", "done_at": 1000})["done_order"], "damaged")

    def test_an_order_too_wide_for_a_token_is_refused(self):
        """R8-011: an order past 13 hex digits would make a 33-character token that the stored
        entry parser rejects (the card stays shown, Undo fails): the dismissal is refused."""
        path = needs.data_dir(self.home) / needs.ORDER_FILE
        path.write_text(json.dumps({"last": 16**13 - 1}))
        with self.assertRaisesRegex(RuntimeError, needs.ORDER_FILE):
            needs.hide_ordered("kanban:b:t", "1", self.home)
        self.assertFalse(needs.load_hidden(self.home))


if __name__ == "__main__":
    unittest.main()
