"""Round 4 for the Windows state lock (issue #43).

The Windows branch cannot run on a real Windows host here. ``StubMsvcrt`` raises the errors the
documented ``msvcrt.locking`` raises (EACCES for a held byte under LK_NBLCK, EDEADLOCK for an
exhausted blocking lock, EBADF/EINVAL for a bad descriptor or argument) and the real
``needs._state_lock`` runs against it on real files. That checks our classification and release
logic, not the Windows API. No test here creates a card or touches a Kanban home.
"""

import errno
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from test_needs import needs  # noqa: E402
from test_state_lock_platforms import run_workers  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class StubMsvcrt(types.SimpleNamespace):
    """``locking`` raises ``failure`` when set, else behaves like a byte-range lock."""

    LK_NBLCK, LK_UNLCK = 2, 0

    def __init__(self, failure=None):
        super().__init__(held={}, calls=0, failure=failure)

    def locking(self, fd, mode, nbytes):
        self.calls += 1
        key = os.fstat(fd).st_ino
        if mode == self.LK_NBLCK:
            if self.failure is not None:
                raise self.failure
            if self.held.get(key) not in (None, fd):
                err = OSError(errno.EACCES, "Permission denied")
                err.winerror = 33  # ERROR_LOCK_VIOLATION
                raise err
            self.held[key] = fd
        elif self.held.get(key) == fd:
            del self.held[key]


class WindowsLockErrors(unittest.TestCase):
    """Class G (a failure reported as something else): only contention may wait and time out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def run_with(self, stub):
        with patch.object(needs, "fcntl", None), patch.object(needs, "msvcrt", stub):
            with patch.object(needs, "STATE_LOCK_TIMEOUT", 1.0):
                t0 = time.monotonic()
                try:
                    with needs._state_lock(self.home):
                        self.fail("the lock must not be entered")
                finally:
                    self.elapsed = time.monotonic() - t0

    def test_permanent_failures_surface_at_once_with_their_own_error(self):
        for code in (errno.EINVAL, errno.EBADF, errno.ENOENT, errno.EIO):
            with self.subTest(errno=code):
                stub = StubMsvcrt(OSError(code, "permanent"))
                with self.assertRaises(OSError) as cm:
                    self.run_with(stub)
                self.assertNotIsInstance(cm.exception, TimeoutError)
                self.assertEqual(cm.exception.errno, code)
                self.assertEqual(stub.calls, 1, "a permanent failure must not be retried")
                self.assertLess(self.elapsed, 0.5)

    def test_contention_is_still_retried_until_the_deadline(self):
        for exc in (
            OSError(errno.EACCES, "held"),
            OSError(errno.EDEADLOCK, "deadlock"),
            OSError(errno.EDEADLK, "deadlock"),
        ):
            with self.subTest(errno=exc.errno):
                stub = StubMsvcrt(exc)
                with self.assertRaises(TimeoutError) as cm:
                    self.run_with(stub)
                self.assertIn("another process holds it", str(cm.exception))
                self.assertGreater(stub.calls, 2)

    def test_a_lock_violation_without_a_posix_errno_is_contention(self):
        err = OSError()
        err.winerror = 33
        stub = StubMsvcrt(err)
        with self.assertRaises(TimeoutError):
            self.run_with(stub)

    def test_the_posix_lock_surfaces_permanent_failures_too(self):
        class Bad:
            LOCK_EX, LOCK_NB = 2, 4

            @staticmethod
            def flock(fd, op):
                raise OSError(errno.EINVAL, "bad")

        with patch.object(needs, "fcntl", Bad), patch.object(needs, "msvcrt", None):
            with self.assertRaises(OSError) as cm, needs._state_lock(self.home):
                pass
        self.assertNotIsInstance(cm.exception, TimeoutError)
        # and nothing is left held: the thread lock was released
        self.assertTrue(needs._STATE_LOCK.acquire(timeout=1))
        needs._STATE_LOCK.release()


class WindowsLockPermissionsAreStatedHonestly(unittest.TestCase):
    """Class I: ``0o600`` is a POSIX mode; on Windows it installs no owner-only ACL."""

    def text(self, *parts):
        return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")

    def test_docs_say_windows_inherits_the_folder_acl(self):
        arch = " ".join(self.text("docs", "architecture.md").split())
        readme = " ".join(self.text("README.md").split())
        guide = " ".join(self.text("docs", "operator-guide.md").split())
        for name, text in (("architecture", arch), ("README", readme), ("operator guide", guide)):
            self.assertIn("inherit", text, name)
            self.assertIn("ACL", text, name)
        self.assertIn("owner-only ACL", arch)
        self.assertIn("never installs an owner-only ACL", arch)

    def test_the_changelog_does_not_claim_equal_permissions_on_windows(self):
        log = " ".join(self.text("CHANGELOG.md").split())
        self.assertNotIn("same file, five-second timeout and private permissions as", log)

    def test_the_lock_file_mode_assertion_is_not_a_windows_skip_in_the_docs_claim(self):
        # The mode test is POSIX-only by design; the docs say what Windows gets instead.
        arch = " ".join(self.text("docs", "architecture.md").split())
        self.assertIn("The mode test runs on macOS and Linux only", arch)


WORKER_SAME_KEY = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import needs
home, go, keys = Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4])
while not go.exists():
    time.sleep(0.001)
for i in range(keys):
    needs.file_outside(
        {"kind": "draft", "title": "T", "body": "B", "act_where": "Mail", "dedupe_key": f"same-{i}"},
        home, session_id="s", profile="one",
    )
"""


@unittest.skipIf(needs.state_lock_mode() == "thread-only", "no cross-process lock on this platform")
class SameKeyAcrossProcesses(unittest.TestCase):
    """Class L: the lock is what keeps one dedupe key to one record. The workers all file the
    SAME profile and keys, released together, so removing the lock produces duplicates."""

    PROCS, KEYS = 6, 25

    def test_one_record_per_key_when_processes_race_on_the_same_keys(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            go = home / "go"
            needs.data_dir(home)
            argvs = [
                [sys.executable, "-c", WORKER_SAME_KEY, str(ROOT), str(home), str(go), str(self.KEYS)]
                for _ in range(self.PROCS)
            ]
            started = threading.Thread(target=lambda: (time.sleep(1.0), go.write_text("go")))
            started.start()
            try:
                results = run_workers(argvs, 120)
            finally:
                started.join()
            for code, err in results:
                self.assertEqual(code, 0, err)
            recs = [json.loads(f.read_text()) for f in (needs.data_dir(home) / "outside").glob("*.json")]
            keys = sorted(r["dedupe_key"] for r in recs)
            self.assertEqual(keys, sorted(f"same-{i}" for i in range(self.KEYS)))


class CorruptStoreMutationUnderTheLock(unittest.TestCase):
    """A mutation that enters the lock on a corrupt store is refused, leaves the bytes alone and
    releases the lock, on the POSIX lock and on the Windows branch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.store = needs.data_dir(self.home) / "hidden.json"
        self.store.write_bytes(b"{not json")

    def check(self):
        entered = []
        real = needs._state_lock

        def spy(home):
            entered.append(1)
            return real(home)

        # The card lookup before the lock is a queue read with its own lock; this test counts
        # only the mutation's lock.
        with patch.object(needs, "_state_lock", spy), patch.object(needs, "_current_version", lambda *a: None):
            with self.assertRaises(RuntimeError):
                needs.hide("a", "1", self.home)
        self.assertEqual(entered, [1], "the corrupt store must be met inside the lock")
        self.assertEqual(self.store.read_bytes(), b"{not json")
        self.assertEqual(sorted(p.name for p in needs.data_dir(self.home).iterdir() if p.name != ".state.lock"), ["hidden.json"])
        with needs._state_lock(self.home):  # released
            pass

    def test_on_the_native_lock(self):
        self.check()

    def test_on_the_windows_branch(self):
        stub = StubMsvcrt()
        with patch.object(needs, "fcntl", None), patch.object(needs, "msvcrt", stub):
            self.check()
            self.assertEqual(stub.held, {})


class WorkerLeak(unittest.TestCase):
    """Class L: a failing or hanging worker must not leave the others running."""

    def test_a_hang_and_a_failure_leave_no_running_child(self):
        hang = [sys.executable, "-c", "import time; time.sleep(600)"]
        spawned = []
        real = subprocess.Popen

        def tracking(*a, **k):
            p = real(*a, **k)
            spawned.append(p)
            return p

        with patch.object(subprocess, "Popen", tracking):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_workers([hang, hang, [sys.executable, "-c", "pass"]], 1)
        self.assertEqual(len(spawned), 3)
        self.assertTrue(all(p.poll() is not None for p in spawned))


if __name__ == "__main__":
    unittest.main()
