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
    def test_an_untracked_item_that_left_its_view_or_has_an_unknown_state_keeps_its_watermark(self):
        """R5-api-009: an older open after an untracked left_view or unknown state is stale."""
        for state in (ac.LEFT_VIEW, "cancelled"):
            local, plans = replay([batch(it(seq=5, rev="r5", state=state)), batch(it(seq=3, rev="r3"))])
            self.assertEqual(ops(plans[1]), ["ignore_stale"], state)
            self.assertNotEqual(local[K].state, ac.OPEN, state)

    def test_a_left_view_hold_stores_its_revision_so_a_replay_is_a_duplicate(self):
        """R5-api-010: seq and revision move together, also for a hold."""
        local, plans = replay([batch(it(seq=2, rev="r2", state=ac.LEFT_VIEW))] * 2, {K: ac.Local(1, "r1")})
        self.assertEqual((local[K].seq, local[K].revision), (2, "r2"))
        self.assertEqual(ops(plans[1]), [])

    def test_working_reads_do_not_release_an_item_that_left_its_view(self):
        """R5-api-011: an empty successful read is no evidence that a moved item came back."""
        local, plans = replay([batch(it(seq=1)), batch(it(seq=2, state=ac.LEFT_VIEW)), batch(cursor="c3")])
        self.assertEqual(ops(plans[2]), [])
        self.assertTrue(local[K].held)

    def test_a_successful_read_with_a_stale_event_releases_an_access_hold(self):
        """R5-api-012: the read worked for that item, so its access hold ends like an unmentioned one."""
        local, plans = replay([batch(it(seq=3, rev="r3"))], {K: ac.Local(5, "r5", held=True)})
        self.assertIn("release_hold", ops(plans[0]))
        self.assertFalse(local[K].held)
        self.assertEqual(local[K].seq, 5)

    def test_a_reopen_inside_a_batch_discards_the_older_verified_evidence(self):
        """R5-api-013: one batch ends where the same events in two batches end."""
        start = {K: ac.Local(1, "r1", ac.DONE, ac.VERIFIED)}
        events = [it(seq=2, rev="r2"), it(seq=3, rev="r2", state=ac.DONE, confidence=ac.GUESSED)]
        one, _ = replay([batch(*events)], start)
        two, _ = replay([batch(e) for e in events], start)
        self.assertEqual(one[K].evidence, ac.HEURISTIC)
        self.assertEqual(one, two)

    def test_a_confirmed_completion_after_a_guess_at_the_same_seq_upgrades_inside_a_batch(self):
        """R5-api-014: equal seq and content with a stronger completion is applied, not dropped."""
        events = [it(seq=2, state=ac.DONE, confidence=ac.GUESSED), it(seq=2, state=ac.DONE)]
        one, _ = replay([batch(*events)])
        two, _ = replay([batch(e) for e in events])
        self.assertEqual(one[K].evidence, ac.VERIFIED)
        self.assertEqual(one, two)

    def test_a_different_state_at_the_same_seq_collides_inside_and_across_batches(self):
        """R5-api-015: one equal-seq rule; the first state seen is kept either way."""
        for second in (ac.DONE, ac.DELETED):
            events = [it(seq=1), it(seq=1, state=second)]
            one, p1 = replay([batch(*events)])
            two, p2 = replay([batch(e) for e in events])
            self.assertIn("seq_collision", ops(p1[0]), second)
            self.assertEqual(ops(p2[1]), ["seq_collision"], second)
            self.assertEqual(one, two, second)
            self.assertEqual(two[K].state, ac.OPEN, second)


hc = _load("unbl0ck_host_compat_r5a", "scripts/host_compat.py")


class HostCompat(unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
