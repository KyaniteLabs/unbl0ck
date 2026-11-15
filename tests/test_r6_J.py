"""Round-6 regression tests for the Ask restart journal, the operator recovery tool and its guide
(lane J). One test per finding; each fails on the code before its fix for the behavioural reason."""

import fcntl
import importlib.util
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


jobs = _load("ub_jobs_r6j", "jobs.py")
rec = _load("ub_recovery_r6j", "recovery.py")
J1 = "a" * 32


def hold(path):
    """An exclusive flock on ``path`` through its own descriptor, as another process would hold it."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


class Journal(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "ask"
        self.dir.mkdir(mode=0o700)
        self.journal = jobs.AskJournal(lambda: self.dir)

    def test_a_job_whose_claim_waited_out_another_writer_is_not_admitted(self):
        """R6-004: while another worker holds the journal lock (about to publish the same id), a
        create whose claim times out is refused instead of admitted without ownership."""
        table = jobs.AskJobs(journal=self.journal)
        lock = hold(self.dir / ".lock")
        self.addCleanup(os.close, lock)
        with patch.object(jobs.AskJournal, "LOCK_WAIT_S", 0.2):
            with self.assertRaises(jobs.ScopeUnavailable):
                table.create(J1, card="c1")
        self.assertEqual(len(table), 0)

    def test_a_deletion_that_waited_for_a_writer_still_leaves_a_tombstone(self):
        """R6-005: a record published while a deleter waited for the lock is removed and fenced,
        so its owner cannot write it again."""
        lock = hold(self.dir / ".lock")
        out = []
        t = threading.Thread(target=lambda: out.append(self.journal.delete(J1, tombstone=True)))
        t.start()
        time.sleep(0.3)  # the deleter is waiting for the lock
        writer = jobs.AskJournal(lambda: self.dir)
        dfd = os.open(self.dir, os.O_RDONLY)
        try:  # the lock holder publishes a new record
            self.assertTrue(writer._publish(dfd, J1, writer._encode(J1, {"state": "done", "started": time.time(), "card": "c1"})))
        finally:
            os.close(dfd)
        os.close(lock)
        t.join(10)
        self.assertEqual(out, [True])
        self.assertTrue((self.dir / f"{J1}.gone").exists(), "deleted without a tombstone")
        self.assertEqual(writer.write(J1, {"state": "done", "started": time.time(), "card": "c1"}), False)
        self.assertFalse((self.dir / f"{J1}.json").exists())


class Recovery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.d = self.home / "plugin-data" / "unbl0ck"
        (self.d / "recovery").mkdir(parents=True, mode=0o700)

    def put(self, path, content):
        path.write_bytes(content)
        os.chmod(path, 0o600)

    def test_restore_locks_the_directory_it_opened_not_a_new_one_at_its_path(self):
        """R6-006: the original installation's writer holds its state lock; the directory is
        renamed and a new one made at its path after the tool opened it. Restore refuses instead of
        locking the new directory and overwriting the original through its descriptor."""
        self.put(self.d / "hidden.json", b'{"a": "current"}')
        self.put(self.d / "recovery" / "hidden-ok.bak", b'{"b": "backup"}')
        lock = hold(self.d / ".state.lock")
        self.addCleanup(os.close, lock)
        moved = self.home / "plugin-data" / "unbl0ck.orig"
        real_plan = rec._plan

        def plan(*a, **k):
            if not moved.exists():
                self.d.rename(moved)
                self.d.mkdir(mode=0o700)
            return real_plan(*a, **k)

        with patch.object(rec, "_plan", plan), patch.object(rec._needs(), "STATE_LOCK_TIMEOUT", 0.3):
            out = rec.restore("hidden", "hidden-ok.bak", self.home, apply=True)
        self.assertEqual(out["status"], "writer_active")
        self.assertEqual((moved / "hidden.json").read_bytes(), b'{"a": "current"}')

    def test_a_refused_directory_does_not_leak_descriptors(self):
        """R6-022: a linked unbl0ck directory is refused without leaving the ancestors it opened."""
        self.d.rename(self.home / "real")
        self.d.symlink_to(self.home / "real", target_is_directory=True)
        before = len(os.listdir("/dev/fd"))
        for _ in range(20):
            with self.assertRaises(RuntimeError):
                rec.diagnose(self.home)
        self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_the_guide_says_a_linked_store_only_affects_the_command_that_reads_it(self):
        """R6-025: a linked bluf.json does not stop `preserve hidden`, and the guide no longer says
        a linked store or backup makes every command refuse."""
        self.put(self.d / "hidden.json", b'{"a": "v"}')
        os.symlink(self.d / "hidden.json", self.d / "bluf.json")
        self.assertEqual(rec.preserve("hidden", self.home)["status"], "preserved")
        guide = " ".join((ROOT / "docs" / "operator-guide.md").read_text().split())
        self.assertNotIn("or a store or backup file makes every command refuse", guide)
        self.assertIn("matters only to the command that reads it", guide)


if __name__ == "__main__":
    unittest.main()
