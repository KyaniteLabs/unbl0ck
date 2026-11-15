"""Round 5 for the provider adapter contract (issue #39): watermarks for items with no card, the
revision a hold keeps, why a hold is released, and one rule for events inside and across batches.
The contract is pure; every case replays batches through reconcile and apply, as a store would."""

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("unbl0ck_adapter_contract_r5", ROOT / "adapter_contract.py")
ac = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ac
spec.loader.exec_module(ac)

A = ("gmail", "A")


def it(seq=1, rev="r1", state=ac.OPEN, ext="m1", **kw):
    return ac.ExternalItem("gmail", "A", ext, seq, rev, state, **kw)


KEY = ("gmail", "A", "m1")


def ops(plan):
    return [a.op for a in plan.actions]


def batch(*items, **kw):
    kw.setdefault("accounts", (A,))
    return ac.ChangeBatch(items=tuple(items), **kw)


def replay(batches, local=None):
    local = dict(local or {})
    plans = []
    for b in batches:
        plan = ac.reconcile(local, b)
        local = ac.apply(local, plan)
        plans.append(plan)
    return local, plans


class UntrackedWatermarks(unittest.TestCase):  # R5-api-009, class A
    def test_an_untracked_left_view_keeps_its_sequence(self):
        local, plans = replay([batch(it(5, "r5", ac.LEFT_VIEW)), batch(it(3, "r3"))])
        self.assertEqual(ops(plans[1]), ["ignore_stale"])
        self.assertNotIn("add", ops(plans[1]))
        self.assertEqual(local[KEY].seq, 5)

    def test_an_untracked_left_view_that_returns_later_is_restored(self):
        local, plans = replay([batch(it(5, "r5", ac.LEFT_VIEW)), batch(it(6, "r6"))])
        self.assertEqual(ops(plans[1]), ["restore"])
        self.assertEqual(local[KEY].state, ac.OPEN)

    def test_an_unknown_state_changes_nothing_not_even_the_sequence(self):  # R5-desk-017
        local, plans = replay([batch(it(1, "r1")), batch(it(9, "r9", "paused")), batch(it(8, "r8"))])
        self.assertEqual(local[KEY].seq, 8)
        self.assertEqual(ops(plans[1]), ["unknown_state"])
        self.assertEqual(ops(plans[2]), ["update"])

    def test_an_untracked_unknown_state_leaves_no_watermark(self):  # R5-api-009, disputed half
        local, plans = replay([batch(it(5, "r5", "paused")), batch(it(3, "r3"))])
        self.assertEqual(ops(plans[1]), ["add"])
        self.assertEqual(local[KEY].seq, 3)


class HoldKeepsRevision(unittest.TestCase):  # R5-api-010, class B
    def test_a_left_view_hold_stores_the_revision_it_saw(self):
        local, _ = replay([batch(it(2, "r2", ac.LEFT_VIEW))], {KEY: ac.Local(1, "r1")})
        self.assertEqual((local[KEY].seq, local[KEY].revision, local[KEY].held), (2, "r2", True))

    def test_replaying_the_same_event_is_a_duplicate_not_a_collision(self):
        local, plans = replay(
            [batch(it(2, "r2", ac.LEFT_VIEW)), batch(it(2, "r2", ac.LEFT_VIEW))], {KEY: ac.Local(1, "r1")}
        )
        self.assertEqual(plans[1].actions, [])
        self.assertEqual((local[KEY].seq, local[KEY].revision), (2, "r2"))


class HoldReleaseNeedsTheRightEvidence(unittest.TestCase):
    def test_a_left_view_hold_is_not_released_by_an_empty_read(self):  # R5-api-011, class A
        local, plans = replay([batch(it(1, "r1")), batch(it(2, "r1", ac.LEFT_VIEW)), batch(), batch()])
        self.assertTrue(local[KEY].held)
        self.assertEqual([ops(p) for p in plans[2:]], [[], []])

    def test_a_left_view_hold_is_released_when_the_item_comes_back(self):
        local, plans = replay([batch(it(1, "r1")), batch(it(2, "r1", ac.LEFT_VIEW)), batch(it(3, "r1"))])
        self.assertFalse(local[KEY].held)
        self.assertIn("release_hold", ops(plans[2]))

    def test_an_access_hold_is_still_released_by_an_empty_read(self):
        local, plans = replay([batch(it(1, "r1")), ac.ChangeBatch(status=ac.ACCESS_REVOKED, accounts=(A,)), batch()])
        self.assertEqual(ops(plans[2]), ["release_hold"])
        self.assertFalse(local[KEY].held)

    def test_a_left_view_on_an_access_held_item_needs_its_own_evidence(self):
        local, plans = replay(
            [batch(it(1, "r1")), ac.ChangeBatch(status=ac.AUTH_FAILED, accounts=(A,)), batch(it(2, "r1", ac.LEFT_VIEW)), batch()]
        )
        self.assertTrue(local[KEY].held)
        self.assertEqual(ops(plans[3]), [])

    def test_a_successful_read_with_only_a_stale_event_releases_an_access_hold(self):  # R5-api-012, class G
        start = {KEY: ac.Local(5, "r5", held=True)}
        local, plans = replay([batch(it(3, "r3"))], start)
        self.assertEqual(ops(plans[0]), ["ignore_stale", "release_hold"])
        self.assertFalse(local[KEY].held)
        self.assertEqual((local[KEY].seq, local[KEY].revision), (5, "r5"))

    def test_a_stale_event_does_not_release_a_left_view_hold(self):
        start = {KEY: ac.Local(5, "r5", held=True, held_by=ac.LEFT_VIEW)}
        local, _ = replay([batch(it(3, "r3"))], start)
        self.assertTrue(local[KEY].held)

    def test_an_incomplete_read_releases_nothing(self):
        start = {KEY: ac.Local(5, "r5", held=True)}
        local, plans = replay([batch(it(3, "r3"), complete=False, omitted=1, stop_reason="item_limit")], start)
        self.assertTrue(local[KEY].held)
        self.assertNotIn("release_hold", ops(plans[0]))


class OneBatchEqualsManyBatches(unittest.TestCase):
    """Events folded in one batch must leave the store where separate batches leave it."""

    def assertSame(self, events, local=None):
        together, _ = replay([batch(*events)], local)
        apart, _ = replay([batch(e) for e in events], local)
        self.assertEqual(together, apart, events)
        return together

    def test_an_intervening_reopen_invalidates_older_completion_evidence(self):  # R5-api-013, class A
        verified = {KEY: ac.Local(1, "r1", ac.DONE, ac.VERIFIED)}
        out = self.assertSame([it(2, "r2"), it(3, "r3", ac.DONE, confidence=ac.GUESSED)], verified)
        self.assertEqual(out[KEY].evidence, ac.HEURISTIC)

    def test_a_reopen_listed_after_the_completion_in_arrival_order_still_counts(self):
        verified = {KEY: ac.Local(1, "r1", ac.DONE, ac.VERIFIED)}
        out, _ = replay([batch(it(3, "r3", ac.DONE, confidence=ac.GUESSED), it(2, "r2"))], verified)
        self.assertEqual(out[KEY].evidence, ac.HEURISTIC)

    def test_confirmed_done_after_guessed_done_at_the_same_seq_upgrades(self):  # R5-api-014, class A
        out = self.assertSame([it(1, "r1", ac.DONE, confidence=ac.GUESSED), it(1, "r1", ac.DONE)])
        self.assertEqual(out[KEY].evidence, ac.VERIFIED)

    def test_guessed_done_after_confirmed_done_never_downgrades(self):
        out = self.assertSame([it(1, "r1", ac.DONE), it(1, "r1", ac.DONE, confidence=ac.GUESSED)])
        self.assertEqual(out[KEY].evidence, ac.VERIFIED)

    def test_a_different_state_at_the_same_seq_is_a_collision_everywhere(self):  # R5-api-015, class M
        for state in (ac.DONE, ac.DELETED):
            with self.subTest(state=state):
                stored = {KEY: ac.Local(1, "r1")}
                across, plans = replay([batch(it(1, "r1", state))], stored)
                self.assertEqual(ops(plans[0]), ["seq_collision"])
                self.assertEqual(across[KEY], ac.Local(1, "r1"))
                inside, plans = replay([batch(it(1, "r1"), it(1, "r1", state))])
                self.assertIn("seq_collision", ops(plans[0]))
                self.assertEqual(inside[KEY].state, ac.OPEN)
                self.assertEqual(inside, across)

    def test_the_same_event_twice_is_a_no_op_inside_a_batch(self):
        out = self.assertSame([it(1, "r1"), it(1, "r1")])
        self.assertEqual(out[KEY], ac.Local(1, "r1"))

    def test_a_deletion_after_an_addition_is_not_left_as_an_addition(self):
        out = self.assertSame([it(1, "r1"), it(2, "r2", ac.DELETED)])
        self.assertEqual(out[KEY].state, "gone")

    def test_several_revisions_end_on_the_newest(self):
        out = self.assertSame([it(1, "r1"), it(2, "r2"), it(3, "r3")])
        self.assertEqual((out[KEY].seq, out[KEY].revision), (3, "r3"))


class Docs(unittest.TestCase):  # R5-desk-028, R5-desk-029, class I
    def doc(self):
        return " ".join((ROOT / "docs" / "provider-integrations.md").read_text(encoding="utf-8").split())

    def test_a_rate_limited_read_needs_no_scope_and_the_docs_say_so(self):
        plan = ac.reconcile({}, ac.ChangeBatch(status=ac.RATE_LIMITED))
        self.assertEqual((plan.actions, plan.stop_reason), ([], ac.RATE_LIMITED))
        with self.assertRaises(ValueError):
            ac.reconcile({}, ac.ChangeBatch(status=ac.OK))
        self.assertIn("except a rate-limited read, which carries and changes nothing", self.doc())

    def test_a_failed_read_holds_open_items_and_the_docs_say_so(self):
        start = {KEY: ac.Local(1, "r1")}
        local, plans = replay([ac.ChangeBatch(status=ac.AUTH_FAILED, accounts=(A,))], start)
        self.assertTrue(local[KEY].held)
        doc = self.doc()
        self.assertIn("`access_revoked` and `auth_failed` only hold items", doc)
        self.assertNotIn("any other status is a failure and changes nothing", doc)

    def test_the_docs_describe_what_releases_each_kind_of_hold(self):
        doc = self.doc()
        self.assertIn("released only by a later event for that item", doc)
        self.assertIn("A provider state the contract does not know changes nothing, not even the watermark", doc)
        self.assertIn("in the order of their `seq`", doc)


if __name__ == "__main__":
    unittest.main()
