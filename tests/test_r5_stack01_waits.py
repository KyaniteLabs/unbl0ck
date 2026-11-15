"""Round-5 findings for live-prompt hook failures (stack 01): faults seen from another process,
faults dropped at the cap, and a retry that races a clear."""

import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from r4c_common import RID, HomeCase, needs

ROOT = Path(__file__).resolve().parents[1]

AGENT = """
import importlib.util, sys
from pathlib import Path
root, home, rid = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
spec = importlib.util.spec_from_file_location(
    "unbl0ck_agent", root / "__init__.py", submodule_search_locations=[str(root)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
needs = plugin._needs()
real = needs._write_json
def fail_waits(path, data, **kw):
    if "waits" in Path(path).parts:
        raise OSError("disk full")
    return real(path, data, **kw)
needs._write_json = fail_waits
needs.record_wait(home, request_id=rid, kind="clarify", prompt="Pick one?", platform="cli")
print("ready", flush=True)
sys.stdin.read()
"""


class WaitCase(HomeCase):
    def setUp(self):
        super().setUp()
        needs._WAIT_FAULTS.clear()
        lost = getattr(needs, "_WAIT_FAULTS_LOST", {})
        lost.clear()
        self.addCleanup(needs._WAIT_FAULTS.clear)
        self.addCleanup(lost.clear)

    def record(self, prompt="Pick one?", rid=RID):
        needs.record_wait(self.home, request_id=rid, kind="clarify", prompt=prompt, platform="cli")


class AHookFailureInAnotherProcessIsReported(WaitCase):
    def test_the_dashboard_process_says_the_agent_could_not_record_its_prompt(self):
        """R5-needs-027 (class G): the fault list lives in the hook's process; polling elsewhere
        must still say a live prompt is missing, and stop saying so when that process is gone."""
        agent = subprocess.Popen(
            [sys.executable, "-I", "-c", AGENT, str(ROOT), str(self.home), RID],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=None,
        )
        self.addCleanup(agent.wait)
        self.addCleanup(agent.stdin.close)
        self.assertEqual(agent.stdout.readline().strip(), "ready")
        errors: list[str] = []
        self.assertEqual(needs.wait_items(self.home, errors=errors), [])
        self.assertTrue(any(e.startswith("prompt") and RID in e for e in errors), errors)
        self.assertFalse(any("Pick one" in e for e in errors))
        agent.stdin.close()
        agent.wait(10)
        errors = []
        needs.wait_items(self.home, errors=errors)
        self.assertEqual(errors, [])


class DroppedFaultsAreCounted(WaitCase):
    def test_a_prompt_dropped_from_the_full_fault_list_is_reported(self):
        """R5-needs-028 (class H): cap eviction must not make an unrecorded prompt vanish quietly."""
        with patch.object(needs, "_write_json", side_effect=OSError("disk full")):
            for n in range(needs.WAIT_FAULT_LIMIT + 1):
                self.record(rid=f"{n:016x}")
        errors: list[str] = []
        items = needs.wait_items(self.home, errors=errors)  # storage is back
        self.assertEqual(len(items), needs.WAIT_FAULT_LIMIT)
        self.assertTrue(any("no longer retried" in e for e in errors), errors)

    def test_a_list_that_never_filled_reports_nothing(self):
        with patch.object(needs, "_write_json", side_effect=OSError("disk full")):
            self.record()
        errors: list[str] = []
        needs.wait_items(self.home, errors=errors)
        self.assertEqual(errors, [])


class ARetryDoesNotResurrectAClearedPrompt(WaitCase):
    def test_a_retry_in_flight_cannot_recreate_a_prompt_that_was_cleared(self):
        """R5-needs-029 (class B): the retry and the clear settle one prompt one at a time."""
        with patch.object(needs, "_write_json", side_effect=OSError("disk full")):
            self.record()
        real = needs._write_json
        inside = threading.Event()
        proceed = threading.Event()

        def paused(path, data, **kw):
            inside.set()
            proceed.wait(10)
            return real(path, data, **kw)

        errors: list[str] = []
        with patch.object(needs, "_write_json", paused):
            retry = threading.Thread(target=lambda: needs._reconcile_waits(self.home, errors))
            retry.start()
            self.assertTrue(inside.wait(10))
            clear = threading.Thread(
                target=lambda: needs.clear_wait(self.home, request_id=RID)
            )
            clear.start()
            proceed.set()
            retry.join(10)
            clear.join(10)
        self.assertEqual(needs.wait_items(self.home), [])
        self.assertFalse((needs._waits_dir(self.home) / f"{RID}.json").exists())
        self.assertEqual(needs._WAIT_FAULTS, {})


class TheFaultFileIsDisclosed(unittest.TestCase):
    def test_the_architecture_doc_lists_the_fault_file_the_code_writes(self):
        """The new private state file is disclosed where every other one is."""
        doc = (ROOT / "docs" / "architecture.md").read_text()
        self.assertIn("`wait-faults.json`", doc)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(needs._fault_path(Path(tmp)).name, "wait-faults.json")


if __name__ == "__main__":
    unittest.main()
