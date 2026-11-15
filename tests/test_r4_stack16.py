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

    def __init__(self, failure=None, script=()):
        # ``script`` is consumed one entry per LK_NBLCK call (an exception is raised, None
        # lets the call through); after it runs out ``failure`` and the real byte lock apply.
        super().__init__(held={}, calls=0, failure=failure, script=list(script))

    def locking(self, fd, mode, nbytes):
        self.calls += 1
        key = os.fstat(fd).st_ino
        if mode == self.LK_NBLCK:
            if self.script:
                step = self.script.pop(0)
                if step is not None:
                    raise step
            elif self.failure is not None:
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

    def contention_errors(self):
        winonly = OSError()
        winonly.winerror = 33  # ERROR_LOCK_VIOLATION with no POSIX errno
        return [
            OSError(errno.EACCES, "held"),
            OSError(errno.EDEADLOCK, "deadlock"),
            OSError(errno.EDEADLK, "deadlock"),
            winonly,
        ]

    def test_contention_is_retried_but_a_permanent_failure_after_it_surfaces_at_once(self):
        # A classifier that retries everything times out here; one that retries nothing never
        # reaches the second call. Only "wait on contention, raise the rest" gets the EINVAL.
        for held in self.contention_errors():
            with self.subTest(errno=held.errno, winerror=getattr(held, "winerror", None)):
                stub = StubMsvcrt(script=[held, held, OSError(errno.EINVAL, "permanent")])
                with self.assertRaises(OSError) as cm:
                    self.run_with(stub)
                self.assertNotIsInstance(cm.exception, TimeoutError)
                self.assertEqual(cm.exception.errno, errno.EINVAL)
                self.assertEqual(stub.calls, 3)
                self.assertLess(self.elapsed, 0.5)

    def test_a_windows_error_with_a_permanent_code_is_not_contention(self):
        bad = OSError(errno.EBADF, "invalid handle")
        bad.winerror = 6  # ERROR_INVALID_HANDLE
        stub = StubMsvcrt(script=[OSError(errno.EACCES, "held"), bad])
        with self.assertRaises(OSError) as cm:
            self.run_with(stub)
        self.assertNotIsInstance(cm.exception, TimeoutError)
        self.assertEqual(cm.exception.winerror, 6)
        self.assertEqual(stub.calls, 2)

    def test_the_lock_is_taken_once_contention_clears(self):
        for held in self.contention_errors():
            with self.subTest(errno=held.errno, winerror=getattr(held, "winerror", None)):
                stub = StubMsvcrt(script=[held, held])
                with patch.object(needs, "fcntl", None), patch.object(needs, "msvcrt", stub):
                    with needs._state_lock(self.home):
                        self.assertEqual(len(stub.held), 1)
                self.assertEqual(stub.held, {})
                self.assertEqual(stub.calls, 4)  # two refusals, the lock, the unlock

    def test_a_held_byte_that_never_clears_times_out_with_the_busy_message(self):
        stub = StubMsvcrt(OSError(errno.EACCES, "held"))
        with self.assertRaises(TimeoutError) as cm:
            self.run_with(stub)
        self.assertIn("another process holds it", str(cm.exception))
        self.assertGreater(stub.calls, 2)

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

    def test_no_state_file_is_called_owner_only_without_the_windows_qualifier(self):
        # Every description of a state file's protection must carry the platform it holds on.
        lines = []
        for parts in (("README.md",), ("docs", "architecture.md"), ("docs", "operator-guide.md")):
            items = []  # one entry per table row, bullet or wrapped paragraph
            for ln in self.text(*parts).splitlines():
                new_item = not items or not ln.strip() or ln.lstrip().startswith(("|", "- ", "* "))
                if new_item or not items[-1]:
                    items.append(ln)
                else:
                    items[-1] += " " + ln
            lines += [
                (parts[-1], ln)
                for ln in items
                if ("outside/" in ln or "waits/" in ln or "ask/<profile>" in ln) and "owner-only" in ln.lower()
            ]
        self.assertTrue(lines)
        for name, ln in lines:
            self.assertIn("Windows", ln, f"{name}: owner-only claim without the Windows ACL note")

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


WORKER_WINDOWS_BRANCH = r"""
import errno, fcntl, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import needs
home, go, keys, inject = Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4]), sys.argv[5] == "1"


class Shim:
    # msvcrt.locking semantics over a real cross-process byte lock: LK_NBLCK on a held range raises
    # EACCES; the optional first call fails with a permanent error (a bad descriptor).
    LK_NBLCK, LK_UNLCK = 2, 0
    calls = refused = 0

    def locking(self, fd, mode, nbytes):
        if mode == self.LK_UNLCK:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return
        self.calls += 1
        if inject and self.calls == 1:
            raise OSError(errno.EBADF, "injected bad descriptor")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.refused += 1
            raise OSError(errno.EACCES, "held")


shim = Shim()
needs.fcntl, needs.msvcrt = None, shim
while not go.exists():
    time.sleep(0.001)
try:
    for i in range(keys):
        needs.file_outside(
            {"kind": "draft", "title": "T", "body": "B", "act_where": "Mail", "dedupe_key": f"win-{i}"},
            home, session_id="s", profile="one",
        )
finally:
    print("refused", shim.refused, file=sys.stderr)
"""


@unittest.skipUnless(os.name == "posix", "the shim stands the Windows lock on a POSIX byte lock")
class WindowsBranchAcrossProcesses(unittest.TestCase):
    """Class L: real processes through the real ``needs._state_lock`` Windows branch. The shim keeps
    the cross-process exclusion (and EACCES on a held byte), so contention is real and the
    classification is what decides whether a worker waits or fails."""

    PROCS, KEYS = 5, 20

    def run_race(self, inject_in):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            go = home / "go"
            needs.data_dir(home)
            argvs = [
                [sys.executable, "-c", WORKER_WINDOWS_BRANCH, str(ROOT), str(home), str(go),
                 str(self.KEYS), "1" if n == inject_in else "0"]
                for n in range(self.PROCS)
            ]
            starter = threading.Thread(target=lambda: (time.sleep(1.0), go.write_text("go")))
            starter.start()
            try:
                results = run_workers(argvs, 120)
            finally:
                starter.join()
            recs = [json.loads(f.read_text()) for f in (needs.data_dir(home) / "outside").glob("*.json")]
            return results, sorted(r["dedupe_key"] for r in recs)

    def test_contention_is_waited_out_and_nothing_is_duplicated(self):
        results, keys = self.run_race(inject_in=None)
        for code, err in results:
            self.assertEqual(code, 0, err)
        refused = sum(int(err.split("refused")[-1]) for _, err in results)
        self.assertGreater(refused, 0, "the workers never contended, so nothing was waited out")
        self.assertEqual(keys, sorted(f"win-{i}" for i in range(self.KEYS)))

    def test_a_permanent_failure_in_one_process_surfaces_and_blocks_no_one(self):
        results, keys = self.run_race(inject_in=0)
        code, err = results[0]
        self.assertNotEqual(code, 0, "the injected bad descriptor must fail that worker")
        self.assertIn("injected bad descriptor", err)
        self.assertNotIn("state lock busy", err)
        for code, err in results[1:]:
            self.assertEqual(code, 0, err)
        self.assertEqual(keys, sorted(f"win-{i}" for i in range(self.KEYS)))


class CorruptStoreMutationUnderTheLock(unittest.TestCase):
    """A mutation that enters the lock on a corrupt store is refused, leaves the bytes alone and
    releases the lock, on the POSIX lock and on the Windows branch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.store = needs.data_dir(self.home) / "hidden.json"
        self.store.write_bytes(b"{not json")

    def check(self, held):
        """``held()`` says whether another descriptor is currently locked out of the state lock."""
        entered, held_during_read = [], []
        real_lock, real_read = needs._state_lock, needs._load_hidden_raw

        def spy_lock(home):
            entered.append(1)
            return real_lock(home)

        def spy_read(home, *a, **k):
            held_during_read.append(held())
            return real_read(home, *a, **k)

        with patch.object(needs, "_state_lock", spy_lock), patch.object(
            needs, "_load_hidden_raw", spy_read
        ), patch.object(needs, "_current_version", lambda *a, **k: None):
            with self.assertRaises(RuntimeError):
                needs.hide("a", "1", self.home)
        self.assertEqual(entered, [1], "the corrupt store must be met inside the lock")
        self.assertEqual(held_during_read, [True], "the store must be read while the lock is held")
        self.assertEqual(self.store.read_bytes(), b"{not json")
        self.assertEqual(sorted(p.name for p in needs.data_dir(self.home).iterdir() if p.name != ".state.lock"), ["hidden.json"])
        with needs._state_lock(self.home):  # released
            pass

    def test_on_the_native_lock(self):
        import fcntl

        def held():
            fd = os.open(needs.data_dir(self.home) / ".state.lock", os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return True
            finally:
                os.close(fd)

        self.check(held)

    def test_on_the_windows_branch(self):
        stub = StubMsvcrt()
        with patch.object(needs, "fcntl", None), patch.object(needs, "msvcrt", stub):
            self.check(lambda: len(stub.held) == 1)
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
