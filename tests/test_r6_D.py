"""Round 6, lane D: desktop connection lifecycle and Ask deadline, acceptance dates, adapter tombstones.

The desktop tests drive the real views (source and built) through the shared harness."""
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

from test_operator_recovery import HARNESS
from test_r4_stack06 import STORE

ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


ac = _load("unbl0ck_adapter_contract_r6", "adapter_contract.py")
host_compat = _load("unbl0ck_host_compat_r6", "scripts/host_compat.py")


class NodeCase(unittest.TestCase):
    def run_views(self, script):
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                result = subprocess.run(
                    ["node", "-e", HARNESS + STORE + script, str(path)], capture_output=True, text=True, timeout=60
                )
                self.assertEqual(result.returncode, 0, result.stderr)


class AcceptanceDates(unittest.TestCase):
    def test_an_impossible_recorded_time_or_release_date_is_refused(self):
        """R6-024: a date-shaped but impossible time or release date does not pass as acceptance evidence."""
        good = ["r", "h", "source abcdef0 / Hermes 0.23.1 released 2026.2.9", "2026-02-09T10:00:00Z", "pass"]
        self.assertEqual(host_compat.matrix_row_problems(good), [])
        bad = ["r", "h", "source abcdef0 / Hermes 0.23.1 released 2026.99.99", "2026-99-99T99:99:99Z", "pass"]
        self.assertEqual(host_compat.matrix_row_problems(bad), ["recorded at is not a UTC time", "no Hermes release date"])
        late = ["r", "h", "source abcdef0 / Hermes 0.23.1 released 2026-02-30", "2026-02-09T24:00:00Z", "pass"]
        self.assertEqual(host_compat.matrix_row_problems(late), ["recorded at is not a UTC time", "no Hermes release date"])


class Tombstones(unittest.TestCase):
    def test_deleted_and_left_view_at_the_same_seq_and_revision_collide(self):
        """R6-026: an untracked deletion and an untracked left_view are different provider states at one seq."""
        def batch(state, seq=1):
            return ac.ChangeBatch(items=(ac.ExternalItem("p", "a", "x", seq, "r", state),), accounts=(("p", "a"),))

        for first, second in ((ac.DELETED, ac.LEFT_VIEW), (ac.LEFT_VIEW, ac.DELETED)):
            local = ac.apply({}, ac.reconcile({}, batch(first)))
            self.assertEqual([a.op for a in ac.reconcile(local, batch(second)).actions], ["seq_collision"], first)
            self.assertEqual([a.op for a in ac.reconcile(local, batch(first)).actions], [], "a repeat is a no-op")
        # A card removed by a deletion, then advanced by a newer left_view, keeps the newer state.
        local = {("p", "a", "x"): ac.Local(0, "r")}
        for b in (batch(ac.DELETED), batch(ac.LEFT_VIEW, 2)):
            local = ac.apply(local, ac.reconcile(local, b))
        self.assertEqual([a.op for a in ac.reconcile(local, batch(ac.DELETED, 2)).actions], ["seq_collision"])


if __name__ == "__main__":
    unittest.main()
