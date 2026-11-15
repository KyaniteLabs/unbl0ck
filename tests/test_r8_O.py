"""Round-8 lane O: expiry of dropped live-prompt faults and dismissal-order recovery and width."""

import json
import unittest
from unittest.mock import patch

from r4c_common import HomeCase, needs


class WaitCase(HomeCase):
    def setUp(self):
        super().setUp()
        for name in ("_WAIT_FAULTS", "_WAIT_FAULTS_LOST", "_WAIT_FAULTS_DROPPED"):
            state = getattr(needs, name)
            state.clear()
            self.addCleanup(state.clear)
        p = patch.object(needs, "WAIT_LOST_LIMIT", 5)
        p.start()
        self.addCleanup(p.stop)

    def overflow(self, home, prefix, at):
        """Fail N+2 records past both lists at time *at*: two faults are dropped to the count."""
        rids = [f"{prefix}{n:08x}" for n in range(needs.WAIT_FAULT_LIMIT + needs.WAIT_LOST_LIMIT + 2)]
        with patch.object(needs.time, "time", lambda: at), patch.object(
            needs, "_write_json", side_effect=OSError("disk full")
        ):
            for rid in rids:
                needs.record_wait(home, request_id=rid, kind="clarify", prompt="Pick?", platform="cli")
        return rids

    def lost(self, at):
        errors: list[str] = []
        with patch.object(needs.time, "time", lambda: at):
            needs._reconcile_waits(self.home, errors)
        return sum(int(e.split()[1]) for e in errors if "no longer retried" in e)


class DroppedFaultsExpire(WaitCase):
    def test_dropped_faults_past_the_max_age_are_not_counted_again_or_kept(self):
        """R8-006: a drop older than WAIT_MAX_AGE is forgotten, not added to a later overflow's
        count, and a home whose drops all expired leaves no bookkeeping behind."""
        other = self.base / "other"
        other.mkdir()
        self.overflow(other, "bb", 1000.0)
        for rid in self.overflow(self.home, "aa", 1000.0):
            needs.clear_wait(self.home, request_id=rid)  # storage is back: every prompt settles
        later = 1000.0 + needs.WAIT_MAX_AGE + 1
        self.overflow(self.home, "cc", later)
        # 5 retained lost faults plus the 2 just dropped; the 2 expired drops are not added back.
        self.assertEqual(self.lost(later), needs.WAIT_LOST_LIMIT + 2)
        self.assertNotIn(str(other), needs._WAIT_FAULTS_DROPPED)


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
