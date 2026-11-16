"""#43: on a host without directory fds or POSIX owners the Ask journal reports "unavailable"."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_ask_job_recovery import jobs


class NonPosixHost(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = jobs.AskJournal(lambda: Path(self.tmp.name) / "ask")

    def test_no_posix_owner_is_unavailable_not_a_crash(self):
        real = os.geteuid
        del os.geteuid
        try:
            fd, status = self.journal._acquire(True, None)
        finally:
            os.geteuid = real
        self.assertEqual((fd, status), (None, "unavailable"))
        self.assertEqual(self.journal.failures, 1)

    def test_no_directory_fds_is_unavailable(self):
        with patch.object(jobs, "_DIR_FD", False, create=True):
            fd, status = self.journal._acquire(True, None)
        self.assertEqual((fd, status), (None, "unavailable"))


if __name__ == "__main__":
    unittest.main()
