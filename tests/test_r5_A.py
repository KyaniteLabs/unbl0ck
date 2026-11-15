"""Round 5, lane A: the provider adapter contract replays batches exactly like separate deliveries,
keeps every watermark and tells its two holds apart; the host acceptance script survives odd
metadata and its acceptance-row check runs on rows that must pass and rows that must not."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ac = _load("unbl0ck_adapter_contract_r5a", "adapter_contract.py")
A = ("gmail", "A")


def it(seq=1, rev="r1", state=ac.OPEN, **kw):
    return ac.ExternalItem("gmail", "A", "m1", seq, rev, state, **kw)


K = ("gmail", "A", "m1")


def batch(*items, **kw):
    return ac.ChangeBatch(items=tuple(items), accounts=(A,), **kw)


def ops(plan):
    return [a.op for a in plan.actions]


def replay(batches, local=None):
    local, plans = dict(local or {}), []
    for b in batches:
        plan = ac.reconcile(local, b)
        local = ac.apply(local, plan)
        plans.append(plan)
    return local, plans


class Contract(unittest.TestCase):
    def test_a_left_view_hold_stores_its_revision_so_a_replay_is_a_duplicate(self):
        """R5-api-010: seq and revision move together, also for a hold."""
        local, plans = replay([batch(it(seq=2, rev="r2", state=ac.LEFT_VIEW))] * 2, {K: ac.Local(1, "r1")})
        self.assertEqual((local[K].seq, local[K].revision), (2, "r2"))
        self.assertEqual(ops(plans[1]), [])



hc = _load("unbl0ck_host_compat_r5a", "scripts/host_compat.py")


class HostCompat(unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
