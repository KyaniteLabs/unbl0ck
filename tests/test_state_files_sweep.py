"""Class-sweep regressions for state-file handling (issue #48): a state file is read only when it
is a plain, small file; the temp name of a write never follows a link; recovery refuses cleanly."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("unbl0ck_needs_sweep48", ROOT / "needs.py")
needs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(needs)

RID = "b" * 32


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)


class ReadsOnlyPlainSmallFiles(Base):
    def test_a_symlinked_wait_is_not_read(self):
        needs.record_wait(self.home, kind="approval", request_id=RID, prompt="x", session_id="s", platform="")
        waits = needs._waits_dir(self.home)
        real = next(waits.glob("*.json"))
        outside = self.home / "elsewhere.json"
        outside.write_text(real.read_text())
        real.unlink()
        real.symlink_to(outside)
        errors = []
        self.assertEqual(needs.wait_items(self.home, errors), [])
        self.assertTrue(errors)  # reported, not silently dropped
        self.assertTrue(outside.exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs mkfifo")
    def test_a_fifo_named_json_does_not_block_reads(self):
        waits = needs._waits_dir(self.home)
        waits.mkdir(parents=True, exist_ok=True)
        os.mkfifo(waits / ("c" * 32 + ".json"))
        out = []
        t = threading.Thread(target=lambda: out.append(needs.wait_items(self.home, [])), daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "reading a FIFO named *.json blocked forever")
        self.assertEqual(out, [[]])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs mkfifo")
    def test_a_fifo_in_the_filed_folder_does_not_block_purge(self):
        d = needs._outside_dir(self.home)
        d.mkdir(parents=True, exist_ok=True)
        os.mkfifo(d / "x.json")
        out = []
        t = threading.Thread(target=lambda: out.append(needs._purge_done(d)), daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "purge blocked on a FIFO")

    def test_an_oversized_state_file_is_refused_before_it_is_read(self):
        p = self.home / "big.json"
        with open(p, "wb") as fh:
            fh.truncate(needs.MAX_STATE_BYTES + 1)
        errors = []
        self.assertEqual(needs._read_json(p, errors=errors), {})
        self.assertTrue(errors)
        with self.assertRaises(RuntimeError):
            needs._read_json(p, strict=True)

    def test_a_symlink_state_file_is_never_followed(self):
        target = self.home / "t.json"
        target.write_text(json.dumps({"a": "b"}))
        link = self.home / "l.json"
        link.symlink_to(target)
        errors = []
        self.assertEqual(needs._read_json(link, errors=errors), {})
        self.assertTrue(errors)


class WriteDoesNotFollowLinks(Base):
    def test_a_planted_link_at_the_temp_name_does_not_clobber_its_target(self):
        d = self.home / "d"
        d.mkdir()
        p = d / "hidden.json"
        victim = self.home / "victim.txt"
        victim.write_text("keep me")
        tmp = p.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.symlink_to(victim)
        needs._write_json(p, {"k": "v"})
        self.assertEqual(victim.read_text(), "keep me")
        self.assertEqual(json.loads(p.read_text()), {"k": "v"})
        self.assertFalse(p.is_symlink())


class RecoveryCli(Base):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "recovery.py"), "--home", str(self.home), *args],
            capture_output=True, text=True, timeout=30,
        )

    def data(self):
        d = self.home / "plugin-data" / "unbl0ck"
        d.mkdir(parents=True)
        return d

    def test_a_recovery_path_that_is_a_file_gives_a_refusal_not_a_traceback(self):
        d = self.data()
        (d / "hidden.json").write_text("{not json")
        (d / "recovery").write_text("i am a file")
        r = self.run_cli("preserve", "hidden")
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(json.loads(r.stdout)["status"], "refused")
        self.assertEqual(r.returncode, 1)

    def test_a_symlinked_recovery_dir_gives_a_refusal_not_a_traceback(self):
        d = self.data()
        (d / "hidden.json").write_text("{not json")
        (self.home / "else").mkdir()
        (d / "recovery").symlink_to(self.home / "else")
        r = self.run_cli("preserve", "hidden")
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(json.loads(r.stdout)["status"], "refused")

    def test_a_lock_that_cannot_be_checked_is_not_reported_as_no_writer(self):
        spec2 = importlib.util.spec_from_file_location("unbl0ck_recovery_sweep48", ROOT / "recovery.py")
        rec = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(rec)
        if rec.fcntl is None:
            self.skipTest("no flock on this platform")
        d = self.data()
        target = self.home / "lock-target"
        target.write_text("")
        (d / rec.LOCK_NAME).symlink_to(target)
        dfd = os.open(d, os.O_RDONLY)
        self.addCleanup(os.close, dfd)
        self.assertIsNone(rec._writer_active(dfd))
        (d / rec.LOCK_NAME).unlink()
        self.assertFalse(rec._writer_active(dfd))  # no lock file: no writer


if __name__ == "__main__":
    unittest.main()
