"""Round-7 lane F: faults evicted from the live-prompt retry list, and legacy journal entries."""

import json
import os
import unittest
from unittest.mock import patch

from r4c_common import HomeCase, needs


class WaitCase(HomeCase):
    def setUp(self):
        super().setUp()
        for name in ("_WAIT_FAULTS", "_WAIT_FAULTS_LOST", "_WAIT_FAULTS_DROPPED"):
            state = getattr(needs, name, {})
            state.clear()
            self.addCleanup(state.clear)

    def record(self, rid):
        needs.record_wait(self.home, request_id=rid, kind="clarify", prompt="Pick?", platform="cli")


class EvictedClearsAreRetried(WaitCase):
    def test_a_clear_evicted_from_the_full_list_is_retried_once_storage_is_back(self):
        """R7-004: a failed clear pushed out of the full retry list is still retried, so its
        resolved prompt disappears and nothing is reported once storage recovers."""
        rids = [f"{n:016x}" for n in range(needs.WAIT_FAULT_LIMIT + 1)]
        for rid in rids:
            self.record(rid)
        with patch.object(needs, "_unlink_wait", side_effect=OSError("read-only")):
            for rid in rids:
                needs.clear_wait(self.home, request_id=rid)
        errors: list[str] = []
        self.assertEqual(needs.wait_items(self.home, errors=errors), [])
        self.assertEqual(errors, [])
        self.assertFalse(any(needs._WAIT_FAULTS_LOST.values()))
        self.assertEqual(needs._read_json(needs._fault_path(self.home)), {})

    def test_evicted_faults_are_bounded_and_the_overflow_is_reported(self):
        """R7-004: the evicted-fault set has a fixed bound; what goes past it is reported, not lost."""
        with patch.object(needs, "WAIT_LOST_LIMIT", 5, create=True), patch.object(
            needs, "_write_json", side_effect=OSError("disk full")
        ):
            for n in range(needs.WAIT_FAULT_LIMIT + 10):
                self.record(f"{n:016x}")
        held = sum(len(v) if isinstance(v, set) else 1 for v in needs._WAIT_FAULTS_LOST.values())
        self.assertLessEqual(held, 5)
        errors: list[str] = []
        needs.wait_items(self.home, errors=errors)
        counts = [int(e.split()[1]) for e in errors if "no longer retried" in e]
        self.assertEqual(sum(counts), 10, errors)


class LegacyLostEntriesAreReported(WaitCase):
    def test_a_live_older_agent_lost_count_is_reported(self):
        """R7-013: an older agent still running wrote its dropped faults as one "+lost" count;
        the reader must still report them while that agent lives."""
        pid = os.getppid()
        needs._fault_path(self.home).parent.mkdir(parents=True, exist_ok=True)
        needs._fault_path(self.home).write_text(
            json.dumps({"+lost": {"count": 3, "pid": pid, "proc_start": needs._proc_start(pid)}})
        )
        errors: list[str] = []
        needs.wait_items(self.home, errors=errors)
        self.assertTrue(any(e.startswith("prompt: 3 ") for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
