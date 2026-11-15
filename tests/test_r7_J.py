"""Round-7 regression tests for Ask job admission (lane J). Each fails on the code before its fix
for the behavioural reason."""

import importlib.util
import os
import sys
import tempfile
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


jobs = _load("ub_jobs_r7j", "jobs.py")
J1 = "a" * 32


class Admission(unittest.TestCase):
    def test_two_workers_never_both_send_a_job_whose_claim_cannot_be_published(self):
        """R7-002: with publication failing on I/O, two workers creating the same id must not both
        reach the model: an unclaimed job is refused instead of admitted memory-only."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name) / "ask"
        workers = [jobs.AskJobs(journal=jobs.AskJournal(lambda: d)) for _ in range(2)]
        real = os.rename

        def failing(src, dst, *a, **k):
            if str(dst).endswith(f"{J1}.json"):
                raise OSError("I/O error")
            return real(src, dst, *a, **k)

        sent = 0
        with patch.object(os, "rename", failing):
            for w in workers:
                try:
                    gen = w.create(J1, card="c1")
                except jobs.ScopeUnavailable:
                    continue
                sent += w.mark_sent(J1, gen=gen)
        self.assertLessEqual(sent, 1, "the same job reached the model twice")


if __name__ == "__main__":
    unittest.main()
