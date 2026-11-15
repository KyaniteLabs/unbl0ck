"""Safe recovery for corrupt plugin state (issue #48): diagnose, preserve original bytes, verified
restore, and never clear a lock, substitute defaults or print private data."""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("unbl0ck_recovery_t", ROOT / "recovery.py")
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)

SECRET = "SECRET-card-title-9f3a"
# Applying needs the cross-process state lock; where the platform has none the tool refuses (by
# design), so a test of the applied path states that expectation instead of requiring "restored".
APPLIES = rec.fcntl is not None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.d = self.home / "plugin-data" / "unbl0ck"
        self.d.mkdir(parents=True)

    def put(self, name, content, mode=0o600):
        p = self.d / name
        data = content if isinstance(content, bytes) else content.encode()
        p.write_bytes(data)
        os.chmod(p, mode)
        return p

    def backup(self, name, content, mode=0o600):
        r = self.d / "recovery"
        r.mkdir(exist_ok=True, mode=0o700)
        p = r / name
        p.write_bytes(content.encode())
        os.chmod(p, mode)
        return name

    def row(self, store):
        return next(s for s in rec.diagnose(self.home)["stores"] if s["store"] == store)


class Diagnose(Base):
    def test_healthy_and_absent(self):
        self.put("hidden.json", json.dumps({"a": "v1"}))
        self.assertEqual(self.row("hidden")["status"], "healthy")
        self.assertEqual(self.row("hidden")["entries"], 1)
        self.assertEqual(self.row("bluf")["status"], "absent")

    def test_truncated_and_garbage_are_corrupt(self):
        self.put("hidden.json", '{"a": "v1", "b": ')
        self.put("bluf.json", b"\xff\xfe\x00 not json")
        self.assertEqual(self.row("hidden")["status"], "corrupt")
        self.assertEqual(self.row("bluf")["status"], "corrupt")

    def test_empty_file_is_corrupt_not_healthy(self):
        self.put("pending-release.json", "")
        self.assertEqual(self.row("pending-release")["status"], "corrupt")

    def test_valid_json_in_another_shape_is_unsupported_schema(self):
        self.put("hidden.json", json.dumps(["a", "b"]))
        self.put("pending-release.json", json.dumps({"a": {"nested": 1}}))
        self.put("bluf.json", json.dumps({"a": "not-an-object"}))
        for s in ("hidden", "pending-release", "bluf"):
            self.assertEqual(self.row(s)["status"], "unsupported_schema", s)

    def test_legacy_null_and_empty_hidden_records_are_healthy(self):
        self.put("hidden.json", json.dumps({"a": None, "b": "", "c": "v"}))
        r = self.row("hidden")
        self.assertEqual((r["status"], r["entries"], r["legacy_empty_versions"]), ("healthy", 3, 2))

    def test_symlinked_store_is_unsafe_and_never_followed(self):
        target = self.home / "elsewhere.json"
        target.write_text("{}")
        (self.d / "hidden.json").symlink_to(target)
        self.assertEqual(self.row("hidden")["status"], "unsafe")
        self.assertEqual(rec.preserve("hidden", self.home)["status"], "refused")

    def test_loose_permissions_are_reported(self):
        self.put("hidden.json", "{}", mode=0o644)
        self.assertFalse(self.row("hidden")["owner_only"])

    def test_output_never_contains_stored_text(self):
        self.put("hidden.json", json.dumps({SECRET: SECRET}))
        self.put("bluf.json", f'{{"{SECRET}": ')
        self.put("pending-release.json", json.dumps([SECRET]))
        self.backup("hidden-x.bak", json.dumps({SECRET: SECRET}))
        self.assertNotIn(SECRET, json.dumps(rec.diagnose(self.home)))

    def test_diagnose_changes_nothing_and_creates_nothing(self):
        p = self.put("hidden.json", '{"a": ')
        before = (p.read_bytes(), sorted(x.name for x in self.d.iterdir()))
        rec.diagnose(self.home)
        self.assertEqual((p.read_bytes(), sorted(x.name for x in self.d.iterdir())), before)

    @unittest.skipIf(fcntl is None, "needs flock")
    def test_active_writer_is_reported_separately_from_corruption(self):
        self.put("hidden.json", "{}")
        fd = os.open(self.d / ".state.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        out = rec.diagnose(self.home)
        self.assertTrue(out["writer_active"])
        self.assertEqual(self.row("hidden")["status"], "healthy")


class Preserve(Base):
    def test_corrupt_bytes_are_copied_exactly_with_private_modes(self):
        raw = b'{"a": "v1", \x00\xff truncated'
        self.put("hidden.json", raw)
        r = rec.preserve("hidden", self.home)
        self.assertEqual(r["status"], "preserved")
        copy = self.d / r["backup"]
        self.assertEqual(copy.read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(copy.parent.stat().st_mode), 0o700)
        self.assertEqual((self.d / "hidden.json").read_bytes(), raw)  # original untouched
        self.assertNotIn(SECRET, json.dumps(r))

    def test_two_preserves_never_overwrite_each_other(self):
        self.put("hidden.json", "{}")
        a = rec.preserve("hidden", self.home)["backup"]
        b = rec.preserve("hidden", self.home)["backup"]
        self.assertNotEqual(a, b)

    def test_absent_store_and_unknown_store_are_refused(self):
        self.assertEqual(rec.preserve("hidden", self.home)["status"], "refused")
        self.assertEqual(rec.preserve("../x", self.home)["status"], "refused")


class Restore(Base):
    def setUp(self):
        super().setUp()
        self.good = json.dumps({"item-1": "v1"})
        self.bad = b'{"item-1": "v1", "item-2"'
        self.cur = self.put("hidden.json", self.bad)
        self.name = self.backup("hidden-good.bak", self.good)

    def refused_without_a_lock(self, r):
        """Where there is no lock the apply is refused and nothing is touched: the whole expectation."""
        self.assertEqual(r["status"], "refused")
        self.assertEqual(self.cur.read_bytes(), self.bad)
        self.assertEqual([p.name for p in (self.d / "recovery").iterdir()], [self.name])

    def test_dry_run_changes_nothing_and_states_the_consequences(self):
        r = rec.restore("hidden", self.name, self.home)
        self.assertEqual(r["status"], "dry_run")
        self.assertEqual(self.cur.read_bytes(), self.bad)
        self.assertEqual(r["current_status"], "corrupt")
        self.assertEqual(r["backup_entries"], 1)
        self.assertTrue(any("lost" in c for c in r["consequences"]))
        self.assertEqual([p.name for p in (self.d / "recovery").iterdir()], [self.name])

    def test_apply_preserves_original_bytes_then_replaces_atomically(self):
        r = rec.restore("hidden", self.name, self.home, apply=True)
        if not APPLIES:
            return self.refused_without_a_lock(r)
        self.assertEqual(r["status"], "restored")
        self.assertEqual(json.loads(self.cur.read_text()), {"item-1": "v1"})
        self.assertEqual(stat.S_IMODE(self.cur.stat().st_mode), 0o600)
        self.assertEqual((self.d / r["preserved_current"]).read_bytes(), self.bad)
        self.assertEqual([p.name for p in self.d.glob(".restore.*")], [])
        self.assertEqual(self.row("hidden")["status"], "healthy")

    def test_restoring_over_healthy_state_says_so(self):
        self.put("hidden.json", json.dumps({"other": "v"}))
        r = rec.restore("hidden", self.name, self.home)
        self.assertTrue(any("healthy" in c for c in r["consequences"]))

    def test_restore_into_a_missing_store_works_and_preserves_nothing(self):
        self.cur.unlink()
        r = rec.restore("hidden", self.name, self.home, apply=True)
        if not APPLIES:
            self.assertEqual(r["status"], "refused")
            self.assertFalse(self.cur.exists())
            return
        self.assertEqual((r["status"], r["preserved_current"]), ("restored", None))

    def test_interruption_before_the_swap_leaves_the_original_and_a_copy(self):
        with patch.object(rec.os, "replace", side_effect=OSError("disk full")):
            r = rec.restore("hidden", self.name, self.home, apply=True)
        if not APPLIES:
            return self.refused_without_a_lock(r)
        self.assertEqual(r["status"], "failed")
        self.assertTrue(r["store_unchanged"])
        self.assertEqual(self.cur.read_bytes(), self.bad)
        self.assertEqual((self.d / r["preserved_current"]).read_bytes(), self.bad)
        self.assertEqual([p.name for p in self.d.glob(".restore.*")], [])

    def test_failure_while_copying_the_original_aside_changes_nothing(self):
        with patch.object(rec, "_preserve_raw", side_effect=OSError("read-only")):
            r = rec.restore("hidden", self.name, self.home, apply=True)
        if not APPLIES:
            return self.refused_without_a_lock(r)
        self.assertEqual((r["status"], r["store_unchanged"]), ("failed", True))
        self.assertEqual(self.cur.read_bytes(), self.bad)

    def test_a_backup_that_does_not_match_what_was_written_is_never_installed(self):
        # A real readback mismatch: the bytes on disk differ from the buffer (same length, one byte
        # changed) with nothing raising on its own. Without the comparison this would install.
        real = rec._read_back
        with patch.object(rec, "_read_back", lambda fd, n: real(fd, n)[:-1] + b"X"):
            r = rec.restore("hidden", self.name, self.home, apply=True)
        if not APPLIES:
            return self.refused_without_a_lock(r)
        self.assertEqual(r["status"], "failed")
        self.assertTrue(r["store_unchanged"])
        self.assertEqual(self.cur.read_bytes(), self.bad)
        self.assertEqual([p.name for p in self.d.glob(".restore.*")], [])

    def test_refuses_unverifiable_backups_before_any_mutation(self):
        cases = {
            "hidden-trunc.bak": ('{"a": ', 0o600),
            "hidden-shape.bak": (json.dumps(["x"]), 0o600),
            "hidden-open.bak": (self.good, 0o644),
        }
        for name, (content, mode) in cases.items():
            self.backup(name, content, mode)
            r = rec.restore("hidden", name, self.home, apply=True)
            self.assertEqual(r["status"], "refused", name)
        self.assertEqual(self.cur.read_bytes(), self.bad)

    def test_backup_for_another_store_or_outside_the_recovery_dir_is_refused(self):
        other = self.backup("bluf-good.bak", json.dumps({"a": {"bluf": "x"}}))
        self.assertEqual(rec.restore("hidden", other, self.home, apply=True)["status"], "refused")
        (self.home / "hidden-evil.bak").write_text("{}")
        for name in ("../../../hidden-evil.bak", "/etc/passwd", "recovery/../x.bak", "hidden-missing.bak"):
            self.assertEqual(rec.restore("hidden", name, self.home, apply=True)["status"], "refused", name)
        self.assertEqual(self.cur.read_bytes(), self.bad)

    def test_symlinked_backup_and_symlinked_store_are_refused(self):
        real = self.home / "real.json"
        real.write_text(self.good)
        os.chmod(real, 0o600)
        (self.d / "recovery" / "hidden-link.bak").symlink_to(real)
        self.assertEqual(rec.restore("hidden", "hidden-link.bak", self.home, apply=True)["status"], "refused")
        self.cur.unlink()
        self.cur.symlink_to(real)
        self.assertEqual(rec.restore("hidden", self.name, self.home, apply=True)["status"], "refused")
        self.assertEqual(real.read_text(), self.good)

    def test_legacy_null_records_survive_a_restore_round_trip(self):
        legacy = json.dumps({"a": None, "b": ""})
        name = self.backup("hidden-legacy.bak", legacy)
        r = rec.restore("hidden", name, self.home, apply=True)
        if not APPLIES:
            self.assertEqual(r["status"], "refused")
            return
        self.assertEqual(r["status"], "restored")
        self.assertEqual(self.cur.read_text(), legacy)

    def test_healthy_sources_continue_while_one_store_is_restored(self):
        other = self.put("pending-release.json", json.dumps({"c": "v9"}))
        before = other.read_bytes()
        rec.restore("hidden", self.name, self.home, apply=True)
        self.assertEqual(other.read_bytes(), before)
        self.assertEqual(self.row("pending-release")["status"], "healthy")

    def test_output_never_contains_stored_text(self):
        self.put("hidden.json", f'{{"{SECRET}": ')
        name = self.backup("hidden-s.bak", json.dumps({SECRET: SECRET}))
        out = json.dumps([rec.restore("hidden", name, self.home), rec.restore("hidden", name, self.home, apply=True)])
        self.assertNotIn(SECRET, out)

    @unittest.skipIf(fcntl is None, "needs flock")
    def test_active_writer_blocks_restore_and_the_lock_is_left_alone(self):
        lock = self.d / ".state.lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with patch.object(rec, "_needs", _fast_needs):
            r = rec.restore("hidden", self.name, self.home, apply=True)
        self.assertEqual(r["status"], "writer_active")
        self.assertEqual(self.cur.read_bytes(), self.bad)
        self.assertTrue(lock.exists())
        self.assertEqual(len(list((self.d / "recovery").iterdir())), 1)  # nothing was preserved or written

    def test_platform_without_a_lock_refuses_to_apply(self):
        with patch.object(rec, "fcntl", None):
            self.assertEqual(rec.restore("hidden", self.name, self.home, apply=True)["status"], "refused")
            self.assertEqual(rec.restore("hidden", self.name, self.home)["status"], "dry_run")
        self.assertEqual(self.cur.read_bytes(), self.bad)
        self.assertEqual([p.name for p in (self.d / "recovery").iterdir()], [self.name])  # nothing preserved either


def _fast_needs():
    """The real needs.py with a short lock timeout so the contention test does not wait five seconds."""
    n = _load_needs()
    n.STATE_LOCK_TIMEOUT = 0.2
    return n


def _load_needs():
    s = importlib.util.spec_from_file_location("unbl0ck_recovery_needs_t", ROOT / "needs.py")
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


class Cli(Base):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "recovery.py"), "--home", str(self.home), *args],
                              capture_output=True, text=True, timeout=60)

    def test_diagnose_exit_code_and_json(self):
        self.put("hidden.json", f'{{"{SECRET}": ')
        r = self.run_cli("diagnose")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["stores"][0]["status"], "corrupt")
        self.assertNotIn(SECRET, r.stdout + r.stderr)

    def test_restore_defaults_to_a_dry_run(self):
        cur = self.put("hidden.json", "{")
        name = self.backup("hidden-ok.bak", "{}")
        r = self.run_cli("restore", "hidden", name)
        self.assertEqual((r.returncode, json.loads(r.stdout)["status"]), (0, "dry_run"))
        self.assertEqual(cur.read_bytes(), b"{")
        r = self.run_cli("restore", "hidden", name, "--apply")
        if not APPLIES:
            self.assertEqual((r.returncode, json.loads(r.stdout)["status"]), (1, "refused"))
            return
        self.assertEqual((r.returncode, json.loads(r.stdout)["status"]), (0, "restored"))


class Docs(unittest.TestCase):
    def test_state_table_and_guide_disclose_the_recovery_directory(self):
        arch = (ROOT / "docs/architecture.md").read_text()
        guide = (ROOT / "docs/operator-guide.md").read_text()
        self.assertIn("recovery/*.bak", arch)
        self.assertIn("recovery.py", guide)
        self.assertIn("never deleted", guide)


if __name__ == "__main__":
    unittest.main()
