"""Provider adapter contract (issue #39): reconciliation rules run against a fake adapter.
Nothing here reads a real account; the contract module is not wired into the queue."""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("unbl0ck_adapter_contract", ROOT / "adapter_contract.py")
ac = importlib.util.module_from_spec(spec)
import sys

sys.modules[spec.name] = ac
spec.loader.exec_module(ac)

ACCT = ("gmail", "acct-1")


def item(ext="m1", seq=1, rev="r1", state=ac.OPEN, provider="gmail", account="acct-1"):
    return ac.ExternalItem(provider, account, ext, seq, rev, state)


def key(ext="m1"):
    return ("gmail", "acct-1", ext)


def ops(plan):
    return [a.op for a in plan.actions]


class FakeAdapter:
    name = "fake"
    capabilities = frozenset({ac.READ})

    def __init__(self, batches):
        self.batches = list(batches)
        self.cursors = []

    def identity(self):
        return "acct-1"

    def read_changes(self, cursor):
        self.cursors.append(cursor)
        return self.batches.pop(0)


class Rules(unittest.TestCase):
    def test_new_item_is_added_and_a_done_one_is_not(self):
        self.assertEqual(ops(ac.reconcile({}, ac.ChangeBatch(items=(item(),), cursor="c1"))), ["add"])
        self.assertEqual(ops(ac.reconcile({}, ac.ChangeBatch(items=(item(state=ac.DONE),)))), [])

    def test_duplicate_events_are_no_ops_across_and_within_batches(self):
        local = {key(): ac.Local(seq=1, revision="r1")}
        self.assertEqual(ops(ac.reconcile(local, ac.ChangeBatch(items=(item(),)))), [])
        self.assertEqual(ops(ac.reconcile({}, ac.ChangeBatch(items=(item(), item())))), ["add"])

    def test_an_older_revision_never_overwrites_a_newer_one(self):
        local = {key(): ac.Local(seq=5, revision="r5")}
        plan = ac.reconcile(local, ac.ChangeBatch(items=(item(seq=3, rev="r3"),)))
        self.assertEqual(ops(plan), ["ignore_stale"])

    def test_changed_draft_updates_and_resurfaces_what_the_human_dealt_with(self):
        local = {key(): ac.Local(seq=1, revision="r1", acted_revision="r1")}
        plan = ac.reconcile(local, ac.ChangeBatch(items=(item(seq=2, rev="r2"),)))
        self.assertEqual(ops(plan), ["update", "resurface"])
        untouched = {key(): ac.Local(seq=1, revision="r1")}
        self.assertEqual(ops(ac.reconcile(untouched, ac.ChangeBatch(items=(item(seq=2, rev="r2"),)))), ["update"])

    def test_external_completion_is_verified_and_reopen_resurfaces(self):
        local = {key(): ac.Local(seq=1, revision="r1")}
        self.assertEqual(ops(ac.reconcile(local, ac.ChangeBatch(items=(item(seq=2, rev="r2", state=ac.DONE),)))),
                         ["complete_verified"])
        done = {key(): ac.Local(seq=2, revision="r2", state=ac.DONE, evidence=ac.VERIFIED)}
        plan = ac.reconcile(done, ac.ChangeBatch(items=(item(seq=3, rev="r3"),)))
        self.assertEqual(ops(plan), ["reopen", "update"])

    def test_a_filed_claim_is_not_verified_completion(self):
        asserted = {key(): ac.Local(seq=1, revision="r1", state=ac.DONE, evidence=ac.ASSERTED)}
        # The provider still shows it open (a newer read): the claim is contradicted, not honoured.
        plan = ac.reconcile(asserted, ac.ChangeBatch(items=(item(seq=2, rev="r1"),)))
        self.assertIn("contradicted", ops(plan))
        self.assertNotIn("complete_verified", ops(plan))
        # Only the provider saying done at the same revision upgrades the evidence.
        plan = ac.reconcile(asserted, ac.ChangeBatch(items=(item(seq=1, rev="r1", state=ac.DONE),)))
        self.assertEqual(ops(plan), ["verify_done"])
        self.assertNotEqual(ac.completion_label(ac.ASSERTED), ac.completion_label(ac.VERIFIED))
        self.assertIn("not confirmed", ac.completion_label(ac.ASSERTED))
        self.assertEqual(ac.completion_label(None), "Open")

    def test_deletion_removes_once_and_absence_is_not_deletion(self):
        local = {key(): ac.Local(seq=1, revision="r1")}
        self.assertEqual(ops(ac.reconcile(local, ac.ChangeBatch(items=(item(seq=2, state=ac.DELETED),)))), ["remove"])
        gone = {key(): ac.Local(seq=1, revision="r1", state="gone")}
        self.assertEqual(ops(ac.reconcile(gone, ac.ChangeBatch(items=(item(seq=2, state=ac.DELETED),)))), [])
        self.assertEqual(ops(ac.reconcile(local, ac.ChangeBatch(items=(), cursor="c9"))), [])  # not in the batch: untouched

    def test_revoked_access_holds_items_and_completes_nothing(self):
        local = {key("a"): ac.Local(seq=1, revision="r1"), key("b"): ac.Local(seq=1, revision="r1", state=ac.DONE, evidence=ac.ASSERTED),
                 ("gmail", "other-acct", "z"): ac.Local(seq=1, revision="r1")}
        plan = ac.reconcile(local, ac.ChangeBatch(status="access_revoked", cursor="ignored"), accounts=[ACCT])
        self.assertEqual(sorted((a.op, a.key[2]) for a in plan.actions), [("hold", "a"), ("hold", "b")])
        self.assertIsNone(plan.cursor)

    def test_a_held_item_is_released_when_reads_work_again(self):
        local = {key(): ac.Local(seq=1, revision="r1", state="held")}
        self.assertEqual(ops(ac.reconcile(local, ac.ChangeBatch(items=(item(),), cursor="c"))), ["release_hold"])

    def test_rate_limit_changes_nothing_and_keeps_the_cursor(self):
        local = {key(): ac.Local(seq=1, revision="r1")}
        plan = ac.reconcile(local, ac.ChangeBatch(status="rate_limited", retry_after=30.0,
                                                  items=(item(seq=9, rev="r9", state=ac.DONE),), cursor="c-new"))
        self.assertEqual((plan.actions, plan.cursor, plan.retry_after), ([], None, 30.0))

    def test_conflicting_edit_is_reported_and_nothing_is_written(self):
        local = {key(): ac.Local(seq=1, revision="r1", pending_edit=True)}
        plan = ac.reconcile(local, ac.ChangeBatch(items=(item(seq=2, rev="r2"),)))
        self.assertEqual(ops(plan), ["conflict", "update"])
        self.assertFalse(any(a.op in ("write", "send", "reply") for a in plan.actions))

    def test_the_cursor_moves_only_on_a_successful_read(self):
        adapter = FakeAdapter([ac.ChangeBatch(status="rate_limited", retry_after=5),
                               ac.ChangeBatch(items=(item(),), cursor="c1")])
        cursor = None
        for _ in range(2):
            plan = ac.reconcile({}, adapter.read_changes(cursor))
            cursor = plan.cursor or cursor
        self.assertEqual((adapter.cursors, cursor), ([None, None], "c1"))

    def test_items_with_the_same_external_id_in_two_accounts_are_distinct(self):
        a, b = item(account="acct-1"), item(account="acct-2")
        self.assertNotEqual(a.key, b.key)
        plan = ac.reconcile({a.key: ac.Local(seq=1, revision="r1")}, ac.ChangeBatch(items=(a, b)))
        self.assertEqual([x.key for x in plan.actions], [b.key])

    def test_reading_never_implies_writing(self):
        self.assertFalse(ac.may_write(FakeAdapter([])))
        self.assertTrue(ac.may_write(type("W", (), {"capabilities": frozenset({ac.READ, ac.WRITE})})()))
        self.assertFalse(ac.may_write(object()))


class Docs(unittest.TestCase):
    def setUp(self):
        self.doc = (ROOT / "docs/provider-integrations.md").read_text()

    def test_the_contract_covers_every_required_topic(self):
        for topic in ("Identity", "Permissions", "Provenance", "Stable external IDs", "Deduplication",
                      "Incremental reads", "Deletion", "Completion and reopen", "Revoked access", "Rate limits",
                      "Reading vs writing"):
            self.assertIn(f"| {topic} |", self.doc, topic)

    def test_the_matrix_selects_providers_explicitly_across_email_and_notes_tasks(self):
        for provider in ("Generic IMAP", "Gmail", "Microsoft 365 / Outlook", "CalDAV tasks", "Notion", "Any other service"):
            self.assertIn(provider, self.doc, provider)
        self.assertIn("not supported", self.doc)
        self.assertIn("Outlook is one row, not the design", self.doc)

    def test_the_doc_says_nothing_is_built_and_claims_are_not_verification(self):
        self.assertIn("plan only", self.doc)
        self.assertIn("calls no mailbox", self.doc)
        self.assertIn("not confirmed", self.doc)

    def test_the_module_does_no_io(self):
        src = (ROOT / "adapter_contract.py").read_text()
        for banned in ("import socket", "import urllib", "import requests", "import http", "open(", "subprocess"):
            self.assertNotIn(banned, src, banned)

    def test_the_architecture_page_links_the_plan(self):
        self.assertIn("provider-integrations.md", (ROOT / "docs/architecture.md").read_text())


if __name__ == "__main__":
    unittest.main()
