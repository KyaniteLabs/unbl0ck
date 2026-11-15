"""Round-4 regressions for the operator recovery tool (issue #48): containment of the data
directory, durable and honest results, bounded plain reads, a plan that matches what was
installed, and documentation that matches behavior."""

import errno
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from conftest import checks_see_a_regular_file

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("unbl0ck_needs_r4s13r", ROOT / "needs.py")
needs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(needs)
_rspec = importlib.util.spec_from_file_location("unbl0ck_recovery_r4s13r", ROOT / "recovery.py")
rec = importlib.util.module_from_spec(_rspec)
_rspec.loader.exec_module(rec)


def run_cli(home, *args):
    return subprocess.run([sys.executable, str(ROOT / "recovery.py"), "--home", str(home), *args],
                          capture_output=True, text=True, timeout=60)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.d = self.home / "plugin-data" / "unbl0ck"
        self.d.mkdir(parents=True)

    def put(self, name, content, mode=0o600):
        p = self.d / name
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
        os.chmod(p, mode)
        return p

    def backup(self, name, content, mode=0o600):
        r = self.d / "recovery"
        r.mkdir(exist_ok=True, mode=0o700)
        p = r / name
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
        os.chmod(p, mode)
        return name

    def swap_to_link(self, name_suffix):
        """An os.open wrapper that, on the first open of a path ending in *name_suffix*, replaces it
        with a link to a foreign file (the race between a check and the open)."""
        foreign = self.home / "foreign.json"
        foreign.write_text('{"foreign": "v"}')
        os.chmod(foreign, 0o600)
        real, done = os.open, []

        def opener(path, flags, *a, **k):
            if str(path).endswith(name_suffix) and not done and not (flags & os.O_CREAT):
                done.append(1)
                os.unlink(path, dir_fd=k.get("dir_fd"))
                os.symlink(foreign, path, dir_fd=k.get("dir_fd"))
            return real(path, flags, *a, **k)

        return opener


class Containment(Base):
    """13-005 (class C): the data directory path itself must not be a link."""

    def test_a_linked_data_directory_is_refused_by_every_command(self):
        real = self.home / "other-install" / "unbl0ck"
        real.mkdir(parents=True)
        (real / "hidden.json").write_text('{"a": "v"}')
        os.chmod(real / "hidden.json", 0o600)
        self.d.rmdir()
        self.d.symlink_to(real, target_is_directory=True)
        for call in (lambda: rec.diagnose(self.home), lambda: rec.preserve("hidden", self.home),
                     lambda: rec.restore("hidden", "hidden-x.bak", self.home, apply=True)):
            with self.assertRaises(RuntimeError):
                call()
        self.assertEqual(sorted(x.name for x in real.iterdir()), ["hidden.json"])

    def test_a_linked_plugin_data_directory_is_refused(self):
        other = self.home / "elsewhere"
        (other / "unbl0ck").mkdir(parents=True)
        self.d.rmdir()
        (self.home / "plugin-data").rmdir()
        (self.home / "plugin-data").symlink_to(other, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            rec.diagnose(self.home)

    def test_the_command_line_refuses_with_a_fixed_sentence(self):
        real = self.home / "other"
        real.mkdir()
        self.d.rmdir()
        self.d.symlink_to(real, target_is_directory=True)
        r = run_cli(self.home, "diagnose")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["status"], "refused")
        self.assertNotIn(str(self.home), r.stdout + r.stderr)


class LockProbe(Base):
    """13-006 (class G): a lock probe that cannot run is unknown, never an exception."""

    @unittest.skipIf(rec.fcntl is None, "needs flock")
    def test_an_unsupported_lock_is_reported_as_unknown_with_the_diagnosis_intact(self):
        self.put("hidden.json", "{}")
        self.put(".state.lock", "")
        with mock.patch.object(rec.fcntl, "flock", side_effect=OSError(errno.EOPNOTSUPP, "no locks here")):
            out = rec.diagnose(self.home)
        self.assertIsNone(out["writer_active"])
        self.assertEqual(out["lock_check"], "could not be checked here")
        self.assertEqual(out["stores"][0]["status"], "healthy")


class Durability(Base):
    """13-007, 13-010 (classes B, G): a created directory's parent and every synced directory are
    made durable, and a failed sync is a failure, not a success."""

    def test_the_first_preserve_syncs_the_directory_that_holds_the_new_entry(self):
        self.put("hidden.json", "{}")
        synced = []
        real = rec._fsync_dir
        with mock.patch.object(rec, "_fsync_dir", lambda fd: (synced.append(os.fstat(fd)), real(fd))[1]):
            self.assertEqual(rec.preserve("hidden", self.home)["status"], "preserved")
        for d in (self.d, self.d / "recovery"):  # the parent of the new recovery directory, and it
            self.assertTrue(any(os.path.samestat(st, d.stat()) for st in synced), d.name)

    def _fail_dir_sync(self, only):
        real = os.fsync

        def fsync(fd):
            st = os.fstat(fd)
            if stat.S_ISDIR(st.st_mode) and os.path.samestat(st, only.stat()):
                raise OSError(errno.EIO, "io")
            return real(fd)

        return mock.patch.object(os, "fsync", fsync)

    @unittest.skipIf(rec.fcntl is None, "needs flock")
    def test_a_failed_backup_directory_sync_is_a_failure_and_restore_does_not_proceed(self):
        cur = self.put("hidden.json", b'{"a": ')
        name = self.backup("hidden-good.bak", '{"a": "v"}')
        with self._fail_dir_sync(self.d / "recovery"):
            self.assertEqual(rec.preserve("hidden", self.home)["status"], "failed")
            r = rec.restore("hidden", name, self.home, apply=True)
        self.assertEqual(r["status"], "failed")
        self.assertTrue(r["store_unchanged"])
        self.assertEqual(cur.read_bytes(), b'{"a": ')

    @unittest.skipIf(rec.fcntl is None, "needs flock")
    def test_a_failed_store_directory_sync_after_the_swap_is_not_reported_as_restored(self):
        cur = self.put("hidden.json", b'{"a": ')
        name = self.backup("hidden-good.bak", '{"a": "v"}')
        with self._fail_dir_sync(self.d):
            r = rec.restore("hidden", name, self.home, apply=True)
        self.assertEqual(r["status"], "failed")
        self.assertFalse(r["store_unchanged"])  # the swap had happened: said plainly
        self.assertTrue(r["preserved_current"])
        self.assertEqual(cur.read_bytes(), b'{"a": "v"}')


class Listing(Base):
    """13-008, 13-009 (classes E, D): an unsafe archive is not an empty one, and a diagnosis reads a
    bounded amount."""

    def test_a_file_or_link_where_the_recovery_directory_belongs_is_reported(self):
        self.put("hidden.json", "{}")
        (self.d / "recovery").write_text("not a directory")
        self.assertEqual(rec.diagnose(self.home)["recovery_dir"], "unsafe")
        (self.d / "recovery").unlink()
        (self.d / "recovery").symlink_to(self.home, target_is_directory=True)
        self.assertEqual(rec.diagnose(self.home)["recovery_dir"], "unsafe")
        self.assertEqual(run_cli(self.home, "diagnose").returncode, 1)

    def test_an_absent_archive_is_absent_not_unsafe(self):
        self.assertEqual(rec.diagnose(self.home)["recovery_dir"], "absent")

    def test_a_large_archive_is_listed_within_a_count_and_the_rest_is_counted(self):
        for i in range(130):
            self.backup(f"hidden-{i:03d}.bak", "{}")
        reads = []
        real = rec._read_file
        with mock.patch.object(rec, "_read_file", lambda p, *a: (reads.append(p), real(p, *a))[1]):
            out = rec.diagnose(self.home)
        self.assertEqual((len(out["backups"]), out["backups_total"], out["backups_omitted"]), (100, 130, 30))
        self.assertLessEqual(len(reads), 100 + 3)  # the three stores plus at most the listed backups

    def test_the_bytes_read_are_budgeted_and_unchecked_backups_are_marked(self):
        for i in range(5):
            self.backup(f"hidden-{i}.bak", '{"a": "v"}' + " " * 90)  # 100 bytes each
        with mock.patch.object(rec, "BACKUP_READ_BUDGET", 250):
            out = rec.diagnose(self.home)
        statuses = sorted(b["status"] for b in out["backups"])
        self.assertEqual(statuses, ["healthy", "healthy", "not_checked", "not_checked", "not_checked"])


class PlainReads(Base):
    """13-012, 13-013 (classes C, D): every recovery read opens once, never follows a link, never
    blocks on a FIFO and is bounded by what is read."""

    def test_a_backup_swapped_for_a_link_after_the_name_was_checked_is_refused(self):
        name = self.backup("hidden-ok.bak", '{"a": "v"}')
        self.put("hidden.json", b"{")
        with rec._Data(self.home) as d, mock.patch.object(os, "open", self.swap_to_link(name)):
            raw, check = rec._check_backup(d, "hidden", name)
        self.assertIsNone(raw)
        self.assertEqual(check["status"], "refused")
        self.assertEqual((self.d / "hidden.json").read_bytes(), b"{")

    def test_a_platform_that_cannot_open_relative_to_a_directory_is_refused(self):
        self.backup("hidden-ok.bak", '{"a": "v"}')
        with mock.patch.object(rec, "_DIR_FD", False):
            for call in (lambda: rec.diagnose(self.home), lambda: rec.preserve("hidden", self.home)):
                with self.assertRaises(RuntimeError):
                    call()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs mkfifo")
    def test_a_fifo_in_place_of_a_store_or_backup_does_not_hang(self):
        """The FIFOs appear after every type check, so only a reader that judges what it opened
        (without blocking) refuses them."""
        os.mkfifo(self.d / "hidden.json")
        r = self.d / "recovery"
        r.mkdir(mode=0o700)
        os.mkfifo(r / "hidden-fifo.bak")
        regular = Path(self.tmp.name) / "regular"
        regular.write_text("{}")
        out = []

        def run():
            with checks_see_a_regular_file({"hidden.json", "hidden-fifo.bak"}, regular):
                out.append(rec.diagnose(self.home))
                out.append(rec.restore("hidden", "hidden-fifo.bak", self.home))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(10)
        if t.is_alive():  # release the blocked reader so the test process can exit
            for f in (self.d / "hidden.json", r / "hidden-fifo.bak"):
                try:
                    os.close(os.open(f, os.O_WRONLY | os.O_NONBLOCK))
                except OSError:  # nobody is reading this one
                    pass
        self.assertFalse(t.is_alive(), "a FIFO blocked the reader")
        self.assertEqual(out[0]["stores"][0]["status"], "unsafe")
        self.assertEqual(out[0]["backups"][0]["status"], "unsafe")
        self.assertEqual(out[1]["status"], "refused")

    def test_a_file_that_grows_after_the_size_check_is_bounded_by_the_read_everywhere(self):
        big = b'{"a": "' + b"x" * 300 + b'"}'
        self.put("hidden.json", big)
        name = self.backup("hidden-big.bak", big)
        real = os.fstat

        def small(fd):
            st = real(fd)
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid, 2, 0, 0, 0))

        real_read = rec._read_file
        with rec._Data(self.home) as d, \
                mock.patch.object(rec, "_read_file", lambda dfd, n, limit=0, budget=None: real_read(dfd, n, 100, budget)), \
                mock.patch.object(os, "fstat", small):
            self.assertEqual(rec._check_backup(d, "hidden", name)[1]["status"], "refused")
            self.assertEqual(rec._read_store(d, "hidden"), (None, "too_large"))
            self.assertEqual(rec._inspect(d, "hidden")["status"], "corrupt")
            listed = rec._list_backups(d)["items"]
        self.assertEqual(listed[0]["status"], "corrupt")


class PlanAndInstall(Base):
    """13-014, 13-015, 13-016, 13-018 (classes A, G, L)."""

    def setUp(self):
        super().setUp()
        self.cur = self.put("hidden.json", b'{"a": ')
        self.name = self.backup("hidden-good.bak", json.dumps({"k1": "v"}))

    @unittest.skipIf(rec.fcntl is None, "needs flock")
    def test_the_reported_plan_is_the_one_that_was_installed(self):
        n = rec._needs()
        real_lock = n._state_lock
        two = json.dumps({"k1": "v", "k2": "v"})

        @contextmanager
        def lock(home):
            with real_lock(home):
                (self.d / "recovery" / self.name).write_text(two)  # replaced while we waited
                self.cur.write_text(json.dumps({"healthy": "now"}))
                yield

        with mock.patch.object(n, "_state_lock", lock):
            r = rec.restore("hidden", self.name, self.home, apply=True)
        self.assertEqual(r["status"], "restored")
        self.assertEqual(r["backup_entries"], 2)
        self.assertEqual(r["current_status"], "healthy")
        self.assertTrue(any("healthy" in c for c in r["consequences"]))

    @unittest.skipIf(rec.fcntl is None, "needs flock")
    def test_a_swapped_temporary_name_is_never_installed(self):
        planted = self.home / "planted.json"
        planted.write_text('{"planted": "v"}')
        real = rec._read_back

        def read_back(fd, n):
            data = real(fd, n)
            tmp = next(self.d.glob(".restore.*.tmp"))
            tmp.unlink()
            tmp.symlink_to(planted)
            return data

        with mock.patch.object(rec, "_read_back", read_back):
            r = rec.restore("hidden", self.name, self.home, apply=True)
        self.assertEqual(r["status"], "failed")
        self.assertTrue(r["store_unchanged"])
        self.assertEqual(self.cur.read_bytes(), b'{"a": ')
        self.assertEqual(list(self.d.glob(".restore.*")), [])

    @unittest.skipIf(rec.fcntl is None, "needs flock")
    def test_a_substitute_that_wins_the_rename_is_reported_not_called_restored(self):
        planted = self.home / "planted.json"
        planted.write_text('{"planted": "other"}')
        os.chmod(planted, 0o600)
        real = os.replace

        def replace(src, dst, **k):
            if ".restore." in str(src) and str(src).endswith(".tmp"):
                os.unlink(src, dir_fd=k.get("src_dir_fd"))
                os.symlink(planted, src, dir_fd=k.get("src_dir_fd"))
            return real(src, dst, **k)

        with mock.patch.object(os, "replace", replace):
            r = rec.restore("hidden", self.name, self.home, apply=True)
        self.assertEqual(r["status"], "failed")
        self.assertFalse(r["store_unchanged"])
        self.assertTrue(r["preserved_current"])

    @unittest.skipIf(rec.fcntl is None, "needs flock")
    def test_a_failed_reread_after_a_failed_write_still_reports_the_preserved_copy(self):
        real_store = rec._read_store
        calls = []

        def read_store(d, store):
            calls.append(1)
            if len(calls) >= 2:
                raise OSError(errno.EIO, "io")
            return real_store(d, store)

        real_write = rec._write_new

        def write_new(dfd, name, raw):
            if ".restore." in name:
                raise OSError(errno.ENOSPC, "full")
            return real_write(dfd, name, raw)

        with mock.patch.object(rec, "_write_new", write_new), mock.patch.object(rec, "_read_store", read_store):
            r = rec.restore("hidden", self.name, self.home, apply=True)
        self.assertEqual(r["status"], "failed")
        self.assertTrue(r["preserved_current"])
        self.assertIsNone(r["store_unchanged"])

    def test_a_copy_that_reads_back_differently_is_removed_and_never_kept(self):
        real = rec._read_back
        with mock.patch.object(rec, "_read_back", lambda fd, n: real(fd, n)[:-1] + b"X"):
            dfd = os.open(self.d, os.O_RDONLY)
            self.addCleanup(os.close, dfd)
            with self.assertRaises(OSError):
                rec._write_new(dfd, "copy.tmp", b'{"abc": "v"}')
        self.assertFalse((self.d / "copy.tmp").exists())

    @unittest.skipIf(rec.fcntl is None, "needs flock")
    def test_an_apply_whose_readback_differs_never_installs_the_file(self):
        real = rec._read_back

        def torn(fd, n):
            return real(fd, n)[:-1] + b"X"  # same length, different byte

        with mock.patch.object(rec, "_read_back", torn):
            r = rec.restore("hidden", self.name, self.home, apply=True)
        self.assertEqual(r["status"], "failed")
        self.assertTrue(r["store_unchanged"])
        self.assertEqual(self.cur.read_bytes(), b'{"a": ')

    def test_a_longer_file_behind_the_descriptor_is_not_equal(self):
        fd = os.open(self.d / "x", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        os.write(fd, b"abcdef")
        self.assertEqual(rec._read_back(fd, 3), b"abcd")  # one byte more than expected: unequal


class DocsMatchBehavior(unittest.TestCase):
    """13-001, 13-004, 13-011 and the Ask marker location (class I)."""

    guide = (ROOT / "docs" / "operator-guide.md").read_text()

    def test_the_failure_promise_is_scoped_to_before_the_swap(self):
        self.assertNotIn("If anything fails, the store is left as it was.", self.guide)
        self.assertIn("A failure before the swap leaves the store as it was", self.guide)
        self.assertIn("store_unchanged", self.guide)

    def test_the_module_does_not_claim_preserve_changes_nothing(self):
        doc = rec.__doc__
        self.assertNotIn("* changes nothing unless", doc)  # the blanket claim preserve contradicts
        self.assertIn("``restore`` changes nothing unless ``apply`` is set", doc)
        self.assertIn("``preserve`` adds a backup file (creating the recovery directory on first use), sets that directory", doc)
        self.assertIn("creates the recovery directory", self.guide)

    def test_windows_dry_runs_are_documented(self):
        self.assertIn("dry runs and `preserve` still work", self.guide)

    def test_ask_markers_are_documented_where_the_code_puts_them(self):
        self.assertNotIn("profile's own data directory", self.guide)
        self.assertIn("shared by every profile", self.guide)
        with tempfile.TemporaryDirectory() as home:
            path = needs.ask_journal_dir(Path(home))
            self.assertEqual(path.parent.name, "ask")
            self.assertEqual(path.parent.parent, Path(home) / "plugin-data" / "unbl0ck")  # the organization home


if __name__ == "__main__":
    unittest.main()
