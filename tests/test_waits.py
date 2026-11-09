import importlib.util, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("unbl0ck_needs_w", ROOT / "needs.py")
needs = importlib.util.module_from_spec(spec); spec.loader.exec_module(needs)

RID = "a" * 32


class LivePrompts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_open_wait_is_listed_then_cleared(self):
        needs.record_wait(self.home, kind="approval", request_id=RID, prompt="rm -rf build/", session_id="s1", platform="telegram")
        items = needs.wait_items(self.home)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual((it["source"], it["kind"], it["id"]), ("prompt", "approval", f"prompt:{RID}"))
        self.assertIn("approval", it["title"]); self.assertIn("in Telegram.", it["why"])
        needs.clear_wait(self.home, request_id=RID, outcome="once")
        self.assertEqual(needs.wait_items(self.home), [])

    def test_dead_process_wait_is_dropped(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait()
        needs.record_wait(self.home, kind="clarify", request_id=RID, prompt="Which branch?")
        f = self.home / "plugin-data/unbl0ck/waits" / f"{RID}.json"
        rec = needs._read_json(f); rec["pid"] = p.pid; rec["proc_start"] = "1"
        needs._write_json(f, rec)
        self.assertEqual(needs.wait_items(self.home), [])
        self.assertFalse(f.exists())

    def test_recycled_pid_is_not_the_waiter(self):
        needs.record_wait(self.home, kind="sudo", request_id=RID)
        f = self.home / "plugin-data/unbl0ck/waits" / f"{RID}.json"
        rec = needs._read_json(f); rec["proc_start"] = "-1"; needs._write_json(f, rec)
        self.assertEqual(needs.wait_items(self.home), [])

    def test_hostile_and_malformed_payloads_never_raise_or_escape(self):
        for rid in ("../../etc/passwd", "", None, "A" * 32, "x" * 200):
            needs.record_wait(self.home, kind="clarify", request_id=rid)
            needs.clear_wait(self.home, request_id=rid)
        waits = self.home / "plugin-data/unbl0ck/waits"
        self.assertEqual(list(waits.glob("*")) if waits.exists() else [], [])
        waits.mkdir(parents=True, exist_ok=True)
        (self.home / "plugin-data/unbl0ck/waits" / f"{RID}.json").write_text("{not json")
        self.assertEqual(needs.wait_items(self.home), [])

    def test_hide_until_a_new_wait(self):
        needs.record_wait(self.home, kind="clarify", request_id=RID, prompt="Ship it?")
        it = needs.wait_items(self.home)[0]
        needs.hide(it["id"], it["version"], self.home)
        q = needs.build_queue(self.home)
        self.assertNotIn(it["id"], [i["id"] for i in q["items"]])

    def test_queue_survives_unreadable_waits_dir(self):
        d = self.home / "plugin-data/unbl0ck"; d.mkdir(parents=True)
        (d / "waits").write_text("a file where a dir should be")
        q = needs.build_queue(self.home)
        self.assertTrue(any(e.startswith("prompt:") for e in q["errors"]))


if __name__ == "__main__":
    unittest.main()
