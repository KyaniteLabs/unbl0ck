"""Round-6 lane W: the live-prompt fault journal (wait-faults.json) and its PID checks."""

import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from r4c_common import RID, HomeCase, needs

ROOT = Path(__file__).resolve().parents[1]

# An agent whose wait-record writes fail; it records the given ids, then stays alive.
AGENT = """
import importlib.util, sys
from pathlib import Path
root, home, prefix, n = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
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
for i in range(n):
    needs.record_wait(home, request_id=f"{prefix}{i:015x}", kind="clarify", prompt="Pick?", platform="cli")
print("ready", flush=True)
sys.stdin.read()
"""


def reported(errors: list[str]) -> int:
    """How many missing live prompts the errors report, per id or as a count."""
    total = 0
    for e in errors:
        words = e.split()
        if e.startswith("prompt: ") and len(words) > 1 and words[1].isdigit():
            total += int(words[1])
        elif "a live prompt" in e:
            total += 1
    return total


class WaitCase(HomeCase):
    def setUp(self):
        super().setUp()
        needs._WAIT_FAULTS.clear()
        needs._WAIT_FAULTS_LOST.clear()
        self.addCleanup(needs._WAIT_FAULTS.clear)
        self.addCleanup(needs._WAIT_FAULTS_LOST.clear)

    def record(self, rid=RID):
        needs.record_wait(self.home, request_id=rid, kind="clarify", prompt="Pick one?", platform="cli")

    def journal(self) -> dict:
        return needs._read_json(needs._fault_path(self.home))

    def agent(self, prefix: str, n: int) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", AGENT, str(ROOT), str(self.home), prefix, str(n)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.addCleanup(proc.wait)
        self.addCleanup(proc.stdin.close)
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        return proc


class OutOfRangePidsAreRefused(WaitCase):
    def test_a_huge_pid_in_the_fault_journal_does_not_abort_the_poll(self):
        """R6-018: an out-of-range pid is malformed input, not an OverflowError from os.kill."""
        needs._fault_path(self.home).write_text(
            json.dumps({RID: {"what": "record", "pid": 10**400}})
        )
        other = "fedcba9876543210"
        self.record(other)
        items = needs.wait_items(self.home, errors=[])
        self.assertEqual([i["id"] for i in items], [f"prompt:{other}"])

    def test_a_huge_pid_in_a_wait_record_is_stale(self):
        """R6-018 (sweep): wait_state uses the same PID check, so a huge pid is stale, not a crash."""
        f = needs._waits_dir(self.home) / f"{RID}.json"
        rec = {"request_id": RID, "pid": 10**400, "since": 1000}
        self.assertEqual(needs.wait_state(f, rec, now=1000), needs.WaitState.STALE)
        self.assertIsNone(needs._valid_pid(True))
        self.assertIsNone(needs._valid_pid(2**31))
        self.assertEqual(needs._valid_pid(2**31 - 1), 2**31 - 1)



if __name__ == "__main__":
    unittest.main()
