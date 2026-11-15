"""Cross-process state safety by OS (issue #43).

The Windows branch is exercised here against a fake ``msvcrt`` that behaves like a byte-range
lock (non-blocking lock fails while another descriptor holds it). That checks our logic, not the
real Windows API: see docs/architecture.md for what still needs a Windows run.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from test_needs import needs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class FakeMsvcrt(types.SimpleNamespace):
    """LK_NBLCK fails with OSError while another descriptor holds the byte; LK_UNLCK frees it."""

    LK_NBLCK, LK_UNLCK = 2, 0

    def __init__(self):
        super().__init__(held={}, calls=[])

    def locking(self, fd, mode, nbytes):
        key = os.fstat(fd).st_ino
        self.calls.append((mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
        if mode == self.LK_NBLCK:
            if self.held.get(key) not in (None, fd):
                raise OSError(13, "Permission denied")
            self.held[key] = fd
        else:
            if self.held.get(key) == fd:
                del self.held[key]


class WindowsBranch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.fake = FakeMsvcrt()
        for name, value in (("fcntl", None), ("msvcrt", self.fake)):
            p = patch.object(needs, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_mode_names_the_guarantee(self):
        self.assertEqual(needs.state_lock_mode(), "msvcrt")
        with patch.object(needs, "msvcrt", None):
            self.assertEqual(needs.state_lock_mode(), "thread-only")

    def test_the_lock_is_taken_and_released_on_the_first_byte(self):
        with needs._state_lock(self.home):
            self.assertEqual(len(self.fake.held), 1)
        self.assertEqual(self.fake.held, {})
        self.assertEqual([(m, n, pos) for m, n, pos in self.fake.calls], [(2, 1, 0), (0, 1, 0)])

    def test_another_holder_makes_us_time_out_and_nothing_is_deleted(self):
        d = needs.data_dir(self.home)
        other = os.open(d / ".state.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.fake.locking(other, FakeMsvcrt.LK_NBLCK, 1)  # a second process holds it
        try:
            with patch.object(needs, "STATE_LOCK_TIMEOUT", 0.2):
                t0 = time.monotonic()
                with self.assertRaises(TimeoutError), needs._state_lock(self.home):
                    self.fail("must not enter while another process holds the lock")
                self.assertLess(time.monotonic() - t0, 2)
            self.assertTrue((d / ".state.lock").exists())  # an active lock is never removed
        finally:
            os.close(other)
        self.fake.held.clear()
        with needs._state_lock(self.home):  # free again
            pass

    def test_an_exception_inside_the_lock_still_releases_it(self):
        with self.assertRaises(RuntimeError), needs._state_lock(self.home):
            raise RuntimeError("boom")
        self.assertEqual(self.fake.held, {})
        with needs._state_lock(self.home):
            pass

    def test_the_lock_file_stays_private_and_state_stays_correct(self):
        needs.hide("a", "1", self.home)
        needs.hide("b", "2", self.home)
        self.assertEqual(needs.load_hidden(self.home), {"a": "1", "b": "2"})
        if os.name == "posix":
            lock = needs.data_dir(self.home) / ".state.lock"
            self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_a_corrupt_store_is_still_refused_under_this_lock(self):
        (needs.data_dir(self.home) / "hidden.json").write_text("{not json")
        with self.assertRaises(RuntimeError):
            needs.load_hidden(self.home, strict=True)
        self.assertEqual(self.fake.held, {})


WORKER = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import needs
home, tag, count = Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
for i in range(count):
    needs.hide(f"hid-{tag}-{i}", "1", home)
    needs.save_bluf(f"blf-{tag}-{i}", "1", "b", "w", home)
    needs._set_pending_release(f"rel-{tag}-{i}", "1", home)
    needs.file_outside(
        {"kind": "draft", "title": "T", "body": "B", "act_where": "Mail", "dedupe_key": f"k-{tag}-{i}"},
        home, session_id="s", profile=tag,
    )
"""


@unittest.skipIf(needs.state_lock_mode() == "thread-only", "no cross-process lock on this platform")
class ConcurrentProcesses(unittest.TestCase):
    """Real processes on this OS, through the lock this OS provides: no update is lost."""

    PROCS, EACH = 4, 12

    def test_hidden_cache_releases_and_filed_records_lose_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            procs = [
                subprocess.Popen([sys.executable, "-c", WORKER, str(ROOT), str(home), f"p{n}", str(self.EACH)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for n in range(self.PROCS)
            ]
            for p in procs:
                _, err = p.communicate(timeout=120)
                self.assertEqual(p.returncode, 0, err.decode()[-400:])
            want = self.PROCS * self.EACH
            self.assertEqual(len(needs.load_hidden(home)), want)
            self.assertEqual(len(needs.load_blufs(home)), want)
            self.assertEqual(len(needs.load_pending_releases(home)), want)
            filed = list((needs.data_dir(home) / "outside").glob("*.json"))
            self.assertEqual(len(filed), want)
            self.assertTrue(all(isinstance(json.loads(f.read_text()), dict) for f in filed))


if __name__ == "__main__":
    unittest.main()
