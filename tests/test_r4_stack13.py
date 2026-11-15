"""Round-4 regressions for the corrupt-state branch (issue #48): plain-file reads that hold on every
platform, a write whose temporary file is the one that is installed, and the recovery tool's
containment, durability, bounded reads, honest results and documented contract."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("unbl0ck_needs_r4s13", ROOT / "needs.py")
needs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(needs)
_rspec = importlib.util.spec_from_file_location("unbl0ck_recovery_r4s13", ROOT / "recovery.py")
rec = importlib.util.module_from_spec(_rspec)
_rspec.loader.exec_module(rec)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.d = self.home / "plugin-data" / "unbl0ck"
        self.d.mkdir(parents=True)


class PlainReadsOnEveryPlatform(Tmp):
    """13-002, 13-003 (class C / E): the refusal of a link and the size bound do not depend on O_NOFOLLOW."""

    def test_a_link_is_refused_where_there_is_no_nofollow(self):
        target = self.home / "other.json"
        target.write_text('{"foreign": "x"}')
        link = self.d / "hidden.json"
        link.symlink_to(target)
        with mock.patch.object(needs, "_NOFOLLOW", 0):
            with self.assertRaises(OSError):
                needs._read_plain_text(link)
            with self.assertRaises(RuntimeError):
                needs.load_hidden(self.home, strict=True)
        self.assertEqual(target.read_text(), '{"foreign": "x"}')

    def test_the_state_lock_is_not_taken_through_a_link_where_there_is_no_nofollow(self):
        target = self.home / "elsewhere.lock"
        target.write_text("")
        (self.d / ".state.lock").symlink_to(target)
        with mock.patch.object(needs, "_NOFOLLOW", 0):
            with self.assertRaises(OSError):
                with needs._state_lock(self.home):
                    pass

    def test_a_file_that_grew_after_the_size_check_is_refused_not_cut_to_a_valid_prefix(self):
        p = self.d / "hidden.json"
        p.write_bytes(b"{}" + b" " * (needs.MAX_STATE_BYTES) + b"\xff garbage")
        real = os.fstat

        def small(fd):
            st = real(fd)
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid, 2, 0, 0, 0))

        with mock.patch.object(needs.os, "fstat", small):
            with self.assertRaises(OSError):
                needs._read_plain_text(p)
            with self.assertRaises(RuntimeError):
                needs.load_hidden(self.home, strict=True)

    def test_the_limit_counts_bytes_not_characters(self):
        p = self.d / "x.json"
        p.write_text('{"a": "' + "é" * 40 + '"}', encoding="utf-8")
        with self.assertRaises(OSError):
            needs._read_plain_text(p, limit=60)  # about 50 characters, over 60 bytes
        self.assertTrue(needs._read_plain_text(p, limit=200))


class WriteInstallsTheFileItWrote(Tmp):
    """13-015 (class A, unbound temporary): a substituted temporary pathname is not installed."""

    def test_a_swapped_temporary_name_is_not_installed(self):
        p = self.d / "hidden.json"
        p.write_text('{"keep": "v"}')
        real_fdopen = os.fdopen
        planted = self.home / "planted.json"
        planted.write_text('{"planted": "yes"}')

        class Swap:
            def __init__(self, fh, name):
                self._fh, self._name = fh, name

            def write(self, b):
                n = self._fh.write(b)
                self._fh.flush()
                os.unlink(self._name)
                os.symlink(planted, self._name)
                return n

            def __getattr__(self, a):
                return getattr(self._fh, a)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return self._fh.__exit__(*a)

        def fdopen(fd, mode="r", *a, **k):
            fh = real_fdopen(fd, mode, *a, **k)
            name = self.d / f"hidden.{os.getpid()}.{__import__('threading').get_ident()}.tmp"
            return Swap(fh, name) if "w" in mode else fh

        with mock.patch.object(needs.os, "fdopen", fdopen):
            with self.assertRaises(OSError):
                needs._write_json(p, {"new": "v"})
        self.assertEqual(json.loads(p.read_text()), {"keep": "v"})
        self.assertEqual(planted.read_text(), '{"planted": "yes"}')
        self.assertEqual(list(self.d.glob("*.tmp")), [])


class JournalInstallsTheFileItWrote(Tmp):
    """Class sweep (A, unbound temporary) for the Ask journal's publish step."""

    def test_a_swapped_temporary_record_is_not_installed(self):
        jspec = importlib.util.spec_from_file_location("unbl0ck_jobs_r4s13", ROOT / "jobs.py")
        jobs = importlib.util.module_from_spec(jspec)
        import sys

        sys.modules["unbl0ck_jobs_r4s13"] = jobs
        jspec.loader.exec_module(jobs)
        ask = self.home / "ask"
        journal = jobs.AskJournal(lambda: ask, lambda: False, lambda: 1000.0)
        job = "a" * 32
        self.assertTrue(journal.write(job, {"state": "queued", "started": 1.0}))
        before = (ask / f"{job}.json").read_bytes()
        planted = self.home / "planted.json"
        planted.write_text("{}")
        real_fsync = os.fsync

        def fsync(fd):
            real_fsync(fd)
            tmp = next(ask.glob(".*.tmp"))
            tmp.unlink()
            tmp.symlink_to(planted)

        with mock.patch.object(jobs.os, "fsync", fsync):
            self.assertFalse(journal.write(job, {"state": "running", "started": 2.0}))
        self.assertEqual((ask / f"{job}.json").read_bytes(), before)
        self.assertEqual(list(ask.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
