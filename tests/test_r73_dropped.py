"""#73: each dropped live-prompt fault expires on its own; a later drop does not keep older ones alive."""

from unittest.mock import patch

from r4c_common import needs
from test_r8_O import WaitCase


class EachDropExpires(WaitCase):
    def fail(self, at, rids):
        """Fail to record *rids* at time *at*; past both lists each further fault is dropped to the count."""
        with patch.object(needs.time, "time", lambda: at), patch.object(needs, "WAIT_FAULT_LIMIT", 1), patch.object(
            needs, "_apply_wait", side_effect=OSError("disk full")
        ), patch.object(needs, "_journal_fault"):
            for rid in rids:
                with self.assertRaises(OSError):
                    needs._settle_wait(self.home, rid, "record", {})

    def dropped(self, at):
        with patch.object(needs.time, "time", lambda: at):
            return needs._dropped(str(self.home))

    def test_a_later_drop_does_not_keep_an_expired_drop_counted(self):
        self.fail(1000.0, [f"a{n}" for n in range(1 + needs.WAIT_LOST_LIMIT + 2)])  # two dropped at 1000
        self.assertEqual(self.dropped(1000.0), 2)
        late = 1000.0 + needs.WAIT_MAX_AGE - 10
        self.fail(late, ["b0"])  # one more dropped before the first two expire
        self.assertEqual(self.dropped(late), 3)
        self.assertEqual(self.dropped(1000.0 + needs.WAIT_MAX_AGE + 1), 1)  # the first two expired
        self.assertEqual(self.dropped(late + needs.WAIT_MAX_AGE), 0)


if __name__ == "__main__":
    import unittest

    unittest.main()
