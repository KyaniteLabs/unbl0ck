"""Round-5 regressions for the operator recovery tool (issue #48): a directory checked once stays
the directory used, enumeration and reads stay inside their budgets, every outcome is counted or
named, and the documents say what the code does."""

import importlib.util
import json
import os
import stat
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("unbl0ck_needs_r5s13r", ROOT / "needs.py")
needs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(needs)
_rspec = importlib.util.spec_from_file_location("unbl0ck_recovery_r5s13r", ROOT / "recovery.py")
rec = importlib.util.module_from_spec(_rspec)
_rspec.loader.exec_module(rec)

GOOD = '{"a": "v"}'  # ten bytes, a valid hidden.json


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.d = self.home / "plugin-data" / "unbl0ck"
        self.d.mkdir(parents=True)
        self.other = Path(self.tmp.name) / "other-install"  # a second installation a link could aim at
        self.other_d = self.other / "plugin-data" / "unbl0ck"
        self.other_d.mkdir(parents=True)

    def put(self, name, content, mode=0o600, where=None):
        p = (where or self.d) / name
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
        os.chmod(p, mode)
        return p

    def recovery(self):
        r = self.d / "recovery"
        r.mkdir(exist_ok=True, mode=0o700)
        return r

    def backup(self, name, content=GOOD, mode=0o600):
        p = self.recovery() / name
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
        os.chmod(p, mode)
        return name

    def tree(self, root):
        return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


class Anchoring(Base):
    """R5-jobs-006 (class C): a directory checked once is the directory every later read, backup
    and replacement uses; a swap after the check is refused, not followed."""

    def test_a_data_directory_swapped_for_a_link_after_the_check_is_not_read_or_written(self):
        self.put("hidden.json", GOOD)
        self.put("hidden.json", '{"foreign": "v"}', where=self.other_d)
        before = self.tree(self.other)
        real_dir = rec._dir

        def swapped(home=None):
            d = real_dir(home)
            os.rename(d, self.home / "moved")
            os.symlink(self.other_d, d)  # the check passed; now the name is a link
            return d

        with mock.patch.object(rec, "_dir", swapped):
            try:
                out = rec.preserve("hidden", self.home)
            except (RuntimeError, OSError):
                out = {"status": "refused"}
        self.assertNotEqual(out["status"], "preserved")
        self.assertEqual(self.tree(self.other), before, "a backup was created in the other installation")

    def test_a_restore_after_the_same_swap_changes_nothing_in_the_other_installation(self):
        self.put("hidden.json", "{")
        self.backup("hidden-ok.bak")
        foreign = self.put("hidden.json", '{"foreign": "v"}', where=self.other_d)
        (self.other_d / "recovery").mkdir(mode=0o700)
        self.put("hidden-ok.bak", GOOD, where=self.other_d / "recovery")  # a backup the link would find
        before = self.tree(self.other)
        real_dir = rec._dir

        def swapped(home=None):
            d = real_dir(home)
            os.rename(d, self.home / "moved")
            os.symlink(self.other_d, d)
            return d

        with mock.patch.object(rec, "_dir", swapped):
            try:
                out = rec.restore("hidden", "hidden-ok.bak", self.home, apply=True)
            except (RuntimeError, OSError):
                out = {"status": "refused"}
        self.assertNotEqual(out["status"], "restored")
        self.assertEqual(foreign.read_bytes(), b'{"foreign": "v"}')
        self.assertEqual(self.tree(self.other), before)

    def test_a_recovery_directory_swapped_for_a_link_after_its_check_receives_no_backup(self):
        self.put("hidden.json", GOOD)
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        real = rec._recovery_dir

        def swapped(d, create):
            r = real(d, create)
            os.rename(r, self.home / "moved-recovery")
            os.symlink(elsewhere, r)
            return r

        with mock.patch.object(rec, "_recovery_dir", swapped):
            try:
                out = rec.preserve("hidden", self.home)
            except (RuntimeError, OSError):
                out = {"status": "refused"}
        self.assertNotEqual(out["status"], "preserved")
        self.assertEqual(list(elsewhere.iterdir()), [], "a backup was written through the swapped link")


class Enumeration(Base):
    """R5-jobs-019 (class D): the listing is bounded while it enumerates, not after."""

    def make_backups(self, n):
        for i in range(n):
            self.backup(f"hidden-2026{i:04d}.bak")

    def test_a_slow_enumeration_stops_at_the_time_budget_and_says_it_is_partial(self):
        self.make_backups(40)
        clock = [0.0]
        real_scandir = os.scandir

        @contextmanager
        def slow(path):
            with real_scandir(path) as it:
                def gen():
                    for e in it:
                        clock[0] += 1.0  # each entry costs a simulated second
                        yield e
                yield gen()

        with mock.patch.object(os, "scandir", slow), mock.patch.object(rec.time, "monotonic", lambda: clock[0]):
            listing = rec._list_backups(self.d)
        self.assertFalse(listing["complete"])
        self.assertLessEqual(listing["total"], 7, "the enumeration ran far past its 5 second budget")
        self.assertGreaterEqual(listing["total"], 1)

    def test_an_enormous_directory_is_not_enumerated_past_the_entry_cap(self):
        self.make_backups(60)
        with mock.patch.object(rec, "MAX_SCANNED_ENTRIES", 25):
            listing = rec._list_backups(self.d)
        self.assertFalse(listing["complete"])
        self.assertEqual(listing["total"], 25)
        self.assertEqual(len(listing["items"]) + listing["omitted"], listing["total"])

    def test_a_complete_listing_is_newest_label_first_and_says_complete(self):
        self.make_backups(5)
        listing = rec._list_backups(self.d)
        self.assertTrue(listing["complete"])
        names = [r["name"] for r in listing["items"]]
        self.assertEqual(names, sorted(names, reverse=True))

    def test_diagnose_surfaces_an_incomplete_scan(self):
        self.make_backups(10)
        with mock.patch.object(rec, "MAX_SCANNED_ENTRIES", 4):
            out = rec.diagnose(self.home)
        self.assertFalse(out["backups_complete"])


class ReadBudget(Base):
    """R5-jobs-020, R5-jobs-021 (classes D, E)."""

    def test_the_aggregate_budget_is_enforced_on_the_bytes_actually_read(self):
        for n in ("hidden-20260103.bak", "hidden-20260102.bak", "hidden-20260101.bak"):
            self.backup(n)  # three files of ten bytes
        def understate(real):
            def wrapper(path, *a, **k):  # a size report that understates what a read returns
                st = real(path, *a, **k)
                if str(path).endswith(".bak"):
                    return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
                                           1, 0, 0, 0))
                return st
            return wrapper

        with mock.patch.object(rec, "BACKUP_READ_BUDGET", 25), \
                mock.patch.object(os, "lstat", understate(os.lstat)), mock.patch.object(os, "stat", understate(os.stat)):
            items = rec._list_backups(self.d)["items"]
        self.assertEqual([r["status"] for r in items], ["healthy", "healthy", "not_checked"])

    def test_a_read_is_limited_to_what_is_left_of_the_budget(self):
        self.backup("hidden-20260101.bak", b'{"a": "' + b"x" * 200 + b'"}')
        with mock.patch.object(rec, "BACKUP_READ_BUDGET", 50):
            row = rec._list_backups(self.d)["items"][0]
        self.assertEqual(row["status"], "not_checked")  # over this diagnosis's budget, not damage

    def test_an_unstatable_backup_is_a_row_and_is_counted(self):
        self.backup("hidden-20260101.bak")
        r = self.d / "recovery"
        os.chmod(r, 0o400)  # names can be listed; nothing inside can be opened or stat-ed
        self.addCleanup(os.chmod, r, 0o700)
        listing = rec._list_backups(self.d)
        self.assertEqual(listing["total"], 1)
        self.assertEqual(len(listing["items"]) + listing["omitted"], listing["total"])
        self.assertEqual(listing["items"][0]["name"], "hidden-20260101.bak")
        self.assertIn(listing["items"][0]["status"], ("unsafe", "not_checked"))


class Outcomes(Base):
    """R5-jobs-022, R5-jobs-023 (classes G, B)."""

    def test_a_store_that_cannot_be_read_after_a_failure_is_unknown_not_unchanged(self):
        self.backup("hidden-ok.bak")  # the store is absent
        real = rec._read_store
        calls = []

        def read_store(d, store):
            calls.append(1)
            return real(d, store) if len(calls) == 1 else (None, "unsafe")

        with mock.patch.object(rec, "_read_store", read_store), \
                mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            out = rec.restore("hidden", "hidden-ok.bak", self.home, apply=True)
        self.assertEqual(out["status"], "failed")
        self.assertIsNone(out["store_unchanged"])

    def test_an_absent_store_after_a_failure_is_unchanged(self):
        self.backup("hidden-ok.bak")
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            out = rec.restore("hidden", "hidden-ok.bak", self.home, apply=True)
        self.assertEqual(out["status"], "failed")
        self.assertIs(out["store_unchanged"], True)

    def _fail_sync_of_recovery(self):
        real = rec._fsync_dir

        def fsync(p):
            if Path(p).name == "recovery":
                raise OSError("sync failed")
            return real(p)

        return mock.patch.object(rec, "_fsync_dir", fsync)

    def test_preserve_names_the_copy_it_created_when_the_directory_sync_fails(self):
        self.put("hidden.json", "{")
        self.recovery()
        with self._fail_sync_of_recovery():
            out = rec.preserve("hidden", self.home)
        self.assertEqual(out["status"], "failed")
        made = sorted(p.name for p in (self.d / "recovery").iterdir())
        self.assertEqual(len(made), 1)
        self.assertEqual(out["backup"], f"recovery/{made[0]}")
        self.assertEqual((self.d / "recovery" / made[0]).read_bytes(), b"{")

    def test_restore_keeps_the_reference_to_the_copy_it_created_when_the_directory_sync_fails(self):
        self.put("hidden.json", "{")
        self.backup("hidden-ok.bak")
        with self._fail_sync_of_recovery():
            out = rec.restore("hidden", "hidden-ok.bak", self.home, apply=True)
        self.assertEqual(out["status"], "failed")
        made = sorted(p.name for p in (self.d / "recovery").iterdir() if p.name != "hidden-ok.bak")
        self.assertEqual(len(made), 1)
        self.assertEqual(out["preserved_current"], f"recovery/{made[0]}")
        self.assertEqual((self.d / "hidden.json").read_bytes(), b"{")  # the store was not replaced


class Permissions(Base):
    """R5-jobs-025 (class I): preserve only adds a file, and creates the directory only when absent."""

    def test_an_existing_recovery_directory_keeps_its_mode(self):
        self.put("hidden.json", "{")
        r = self.recovery()
        os.chmod(r, 0o755)
        out = rec.preserve("hidden", self.home)
        self.assertEqual(out["status"], "preserved")
        self.assertEqual(stat.S_IMODE(os.stat(r).st_mode), 0o755)

    def test_a_directory_it_creates_is_owner_only(self):
        self.put("hidden.json", "{")
        self.assertEqual(rec.preserve("hidden", self.home)["status"], "preserved")
        self.assertEqual(stat.S_IMODE(os.stat(self.d / "recovery").st_mode), 0o700)


class UnrelatedLinks(Base):
    """R5-desk-031 (class I): only the path components a command uses are inspected."""

    def test_a_link_elsewhere_in_the_data_directory_does_not_stop_diagnose_or_preserve(self):
        self.put("hidden.json", GOOD)
        (self.d / "waits").mkdir()
        os.symlink(self.other, self.d / "waits" / "unrelated")
        self.assertEqual(rec.diagnose(self.home)["stores"][0]["status"], "healthy")
        self.assertEqual(rec.preserve("hidden", self.home)["status"], "preserved")


class PostSwapReporting(Base):
    """R5-jobs-026 (class I): what store_unchanged means after the swap."""

    def test_restoring_identical_bytes_then_failing_the_sync_reports_unchanged_true(self):
        self.put("hidden.json", GOOD)
        self.backup("hidden-same.bak", GOOD)
        real = rec._fsync_dir
        state = {"replaced": False}
        real_replace = os.replace

        def replace(*a, **k):
            state["replaced"] = True
            return real_replace(*a, **k)

        def fsync(p):
            if state["replaced"] and Path(p).name == "unbl0ck":
                raise OSError("sync failed")
            return real(p)

        with mock.patch.object(os, "replace", replace), mock.patch.object(rec, "_fsync_dir", fsync):
            out = rec.restore("hidden", "hidden-same.bak", self.home, apply=True)
        self.assertEqual(out["status"], "failed")
        self.assertIs(out["store_unchanged"], True)

    def test_a_failed_reread_after_the_swap_reports_unknown(self):
        self.put("hidden.json", "{")
        self.backup("hidden-ok.bak")
        real_read = rec._read_store
        state = {"replaced": False}
        real_replace = os.replace

        def replace(*a, **k):
            state["replaced"] = True
            return real_replace(*a, **k)

        def read_store(d, store):
            if state["replaced"]:
                raise OSError("unreadable")
            return real_read(d, store)

        real = rec._fsync_dir

        def fsync(p):
            if state["replaced"] and Path(p).name == "unbl0ck":
                raise OSError("sync failed")
            return real(p)

        with mock.patch.object(os, "replace", replace), mock.patch.object(rec, "_fsync_dir", fsync), \
                mock.patch.object(rec, "_read_store", read_store):
            out = rec.restore("hidden", "hidden-ok.bak", self.home, apply=True)
        self.assertEqual(out["status"], "failed")
        self.assertIsNone(out["store_unchanged"])


class ReaderIsTheDefense(Base):
    """R5-jobs-032 (class L): the FIFO tests used to pass on the old lstat checks alone. Here the
    regular file becomes a FIFO between every name check and the open, so only the reader's own
    non-blocking, fstat-based open can refuse it."""

    def swap_to_fifo(self, suffix):
        real, done = os.open, []

        def opener(path, flags, *a, **k):
            name = str(path)
            if name.endswith(suffix) and not done and not (flags & os.O_CREAT):
                done.append(1)
                base = k.get("dir_fd")
                target = Path(path) if base is None else self._find(name)
                os.unlink(target)
                os.mkfifo(target)
            return real(path, flags, *a, **k)

        return opener

    def _find(self, name):
        for c in (self.d / name, self.d / "recovery" / name):
            if c.exists() or c.is_symlink():
                return c
        raise AssertionError(name)

    def run_bounded(self, fn):
        out = []
        t = threading.Thread(target=lambda: out.append(fn()), daemon=True)
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive(), "a FIFO blocked the reader")
        return out[0]

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs mkfifo")
    def test_a_store_that_becomes_a_fifo_before_the_open_is_unsafe_not_a_hang(self):
        self.put("hidden.json", GOOD)
        with mock.patch.object(os, "open", self.swap_to_fifo("hidden.json")):
            row = self.run_bounded(lambda: rec._inspect(self.d, "hidden"))
        self.assertEqual(row["status"], "unsafe")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs mkfifo")
    def test_a_backup_that_becomes_a_fifo_before_the_open_is_refused_and_listed_unsafe(self):
        name = self.backup("hidden-ok.bak")
        with mock.patch.object(os, "open", self.swap_to_fifo(name)):
            raw, check = self.run_bounded(lambda: rec._check_backup(self.d, "hidden", name))
        self.assertIsNone(raw)
        self.assertEqual(check["status"], "refused")
        name2 = self.backup("hidden-two.bak")
        with mock.patch.object(os, "open", self.swap_to_fifo(name2)):
            listing = self.run_bounded(lambda: rec._list_backups(self.d))
        self.assertIn("unsafe", [r["status"] for r in listing["items"] if r["name"] == name2])


class SweepInstances(Base):
    """Class C and M instances found by the sweep over the same module."""

    def swapped_dir(self):
        real_dir = rec._dir

        def swapped(home=None):
            d = real_dir(home)
            os.rename(d, self.home / "moved")
            os.symlink(self.other_d, d)
            return d

        return swapped

    @unittest.skipIf(rec.fcntl is None, "needs flock")
    def test_the_writer_probe_never_reports_the_lock_of_another_installation(self):
        import fcntl
        self.put("hidden.json", GOOD)
        lock = self.other_d / ".state.lock"
        lock.write_bytes(b"")
        os.chmod(lock, 0o600)
        fd = os.open(lock, os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)  # a writer in the other installation
        with mock.patch.object(rec, "_dir", self.swapped_dir()):
            try:
                out = rec.diagnose(self.home)
            except (RuntimeError, OSError):
                return  # refused: the probe did not look through the swapped name
        self.assertIsNot(out["writer_active"], True)

    def test_the_size_of_an_oversized_store_is_not_read_from_a_swapped_directory(self):
        self.put("hidden.json", b"x" * 3000)
        self.put("hidden.json", b"y" * 1234, where=self.other_d)
        real_read = rec._read_file

        def read_file(p, limit=rec.MAX_STORE_BYTES):
            out = real_read(p, 100)  # the file is over the (lowered) limit
            os.rename(self.d, self.home / "moved")
            os.symlink(self.other_d, self.d)
            return out

        with mock.patch.object(rec, "_read_file", read_file):
            try:
                row = rec._inspect(self.d, "hidden")
            except (RuntimeError, OSError):
                return
        self.assertNotEqual(row.get("bytes"), 1234)

    def test_a_recovery_directory_recreated_between_two_commands_is_trusted_again(self):
        self.backup("hidden-20260101.bak")
        self.assertEqual(rec.diagnose(self.home)["recovery_dir"], "ok")
        os.rename(self.d / "recovery", self.home / "moved-recovery")
        self.backup("hidden-20260102.bak")  # a new, real directory under the same name
        out = rec.diagnose(self.home)  # a long-lived process (a test run, a tool) must not refuse this
        self.assertEqual([b["name"] for b in out["backups"]], ["hidden-20260102.bak"])


class DocsMatchBehavior(unittest.TestCase):
    guide = " ".join((ROOT / "docs" / "operator-guide.md").read_text().split())

    def test_the_link_statement_names_the_paths_that_are_inspected(self):
        self.assertNotIn("A link anywhere in `plugin-data/unbl0ck` makes every command refuse.", self.guide)
        self.assertIn("links elsewhere under the data directory", self.guide)

    def test_the_guide_says_what_store_unchanged_reports_after_the_swap(self):
        self.assertNotIn("`failed` with `store_unchanged` false", self.guide)
        for phrase in ("`false` when it differs", "`true` when it is byte-for-byte", "`null` when it cannot be read back"):
            self.assertIn(phrase, self.guide)
        self.assertNotIn("reported with ``store_unchanged`` false", rec.__doc__ + (rec.restore.__doc__ or ""))

    def test_the_guide_says_an_existing_recovery_directory_keeps_its_mode(self):
        self.assertIn("an existing directory keeps its mode", self.guide)

    def test_the_recovery_directory_mode_is_described_as_created_not_enforced(self):
        self.assertNotIn("(directory 0700, files 0600)", rec.__doc__)
        self.assertIn("created 0700", rec.__doc__)
        arch = " ".join((ROOT / "docs" / "architecture.md").read_text().split())
        self.assertNotIn("(directory 0700, files 0600)", arch)
        self.assertIn("directory created 0700 and then left as it is", arch)

    def test_the_guide_says_a_scan_can_be_partial(self):
        self.assertIn("`backups_complete`", self.guide)

    def test_the_guide_says_a_failed_directory_sync_names_the_copy(self):
        self.assertIn("names the copy it created", self.guide)


if __name__ == "__main__":
    unittest.main()
