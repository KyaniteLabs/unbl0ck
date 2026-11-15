"""Round 4 for the provider adapter contract (issue #39): stateful replays, coverage reporting,
account scope, lifecycle recovery and the docs that state the same rules. The contract is pure;
no test here creates a card or touches a Kanban home."""

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("unbl0ck_adapter_contract_r4", ROOT / "adapter_contract.py")
ac = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ac
spec.loader.exec_module(ac)

A = ("gmail", "A")
B = ("gmail", "B")


def it(ext="m1", seq=1, rev="r1", state=ac.OPEN, account="A", **kw):
    return ac.ExternalItem("gmail", account, ext, seq, rev, state, **kw)


def k(ext="m1", account="A"):
    return ("gmail", account, ext)


def ops(plan):
    return [a.op for a in plan.actions]


def batch(*items, **kw):
    kw.setdefault("accounts", (A,))
    return ac.ChangeBatch(items=tuple(items), **kw)


def replay(batches, local=None):
    """Run batches through reconcile and apply each plan, as a real store would; return state and plans."""
    local = dict(local or {})
    plans = []
    for b in batches:
        plan = ac.reconcile(local, b)
        local = ac.apply(local, plan)
        plans.append(plan)
    return local, plans


class Coverage(unittest.TestCase):  # 15-001, class E
    def test_a_bounded_rescan_reports_it_stopped_early(self):
        plan = ac.reconcile({}, batch(it("a"), it("b"), complete=False, omitted=100, stop_reason="item_limit", cursor="resume"))
        self.assertFalse(plan.complete)
        self.assertEqual((plan.omitted, plan.stop_reason), (100, "item_limit"))

    def test_an_unknown_remainder_is_reported_as_unknown_not_zero(self):
        plan = ac.reconcile({}, batch(it("a"), complete=False))
        self.assertFalse(plan.complete)
        self.assertIsNone(plan.omitted)
        self.assertEqual(plan.stop_reason, "unspecified")

    def test_a_complete_read_says_so(self):
        plan = ac.reconcile({}, batch(it("a")))
        self.assertEqual((plan.complete, plan.omitted, plan.stop_reason), (True, 0, None))


class Scope(unittest.TestCase):  # 15-002, 15-006, class F
    def test_a_revocation_without_a_scope_is_refused_not_silent(self):
        with self.assertRaises(ValueError):
            ac.reconcile({k(): ac.Local(1, "r1")}, ac.ChangeBatch(status="access_revoked"))

    def test_an_ok_read_without_a_scope_is_refused(self):
        with self.assertRaises(ValueError):
            ac.reconcile({}, ac.ChangeBatch(items=(it(),)))

    def test_the_batch_scope_alone_binds_a_revocation(self):
        plan = ac.reconcile({k(): ac.Local(1, "r1")}, ac.ChangeBatch(status="access_revoked", accounts=(A,)))
        self.assertEqual(ops(plan), ["hold"])
        self.assertEqual(plan.error, "access_revoked")
        self.assertFalse(plan.complete)

    def test_a_disagreeing_scope_is_refused(self):
        with self.assertRaises(ValueError):
            ac.reconcile({}, ac.ChangeBatch(accounts=(A,)), accounts=[B])

    def test_an_item_from_another_account_is_rejected_and_the_cursor_not_accepted(self):
        local = {k(account="B"): ac.Local(1, "r1")}
        plan = ac.reconcile(local, batch(it(account="B", seq=2, state=ac.DELETED), cursor="next"))
        self.assertEqual(ops(plan), ["reject_item"])
        self.assertIsNone(plan.cursor)
        self.assertFalse(plan.complete)
        self.assertIn("rejected_items", plan.stop_reason)
        self.assertEqual(ac.apply(local, plan), local)


class Failures(unittest.TestCase):  # 15-004, class G
    def test_an_unrecognised_status_is_a_failure_not_a_success(self):
        local = {k(): ac.Local(1, "r1")}
        plan = ac.reconcile(local, batch(it(seq=2, state=ac.DELETED), status="error", cursor="next"))
        self.assertEqual((plan.actions, plan.cursor), ([], None))
        self.assertIn("error", plan.error)
        self.assertFalse(plan.complete)

    def test_an_auth_failure_holds_like_a_revocation(self):
        plan = ac.reconcile({k(): ac.Local(1, "r1")}, ac.ChangeBatch(status="auth_failed", accounts=(A,), cursor="x"))
        self.assertEqual((ops(plan), plan.cursor, plan.error), (["hold"], None, "auth_failed"))


class Folding(unittest.TestCase):  # 15-005, class A/M
    def test_an_add_then_a_delete_in_one_batch_leaves_a_tombstone(self):
        local, plans = replay([batch(it(seq=1), it(seq=2, rev="r2", state=ac.DELETED))])
        self.assertEqual(ops(plans[0]), ["add", "remove"])  # applied in order, as two batches would be
        self.assertEqual((local[k()].state, local[k()].seq), ("gone", 2))

    def test_two_new_revisions_in_one_batch_add_once_at_the_newest(self):
        local, plans = replay([batch(it(seq=1, rev="r1"), it(seq=2, rev="r2"))])
        self.assertEqual(ops(plans[0]), ["add", "update"])
        self.assertEqual((local[k()].seq, local[k()].revision), (2, "r2"))

    def test_the_order_inside_a_batch_does_not_matter(self):
        local, _ = replay([batch(it(seq=2, rev="r2"), it(seq=1, rev="r1"))])
        self.assertEqual(local[k()].revision, "r2")


class Sequence(unittest.TestCase):  # 15-007, class K/E
    def test_a_negative_sequence_is_rejected_loudly_not_skipped_with_the_cursor_moving(self):
        plan = ac.reconcile({}, batch(it(seq=-1), cursor="next"))
        self.assertEqual(ops(plan), ["reject_item"])
        self.assertIsNone(plan.cursor)
        self.assertFalse(plan.complete)

    def test_a_non_integer_sequence_is_rejected(self):
        for bad in (True, 1.5, "2", None):
            plan = ac.reconcile({}, batch(it(seq=bad), cursor="next"))
            self.assertEqual(ops(plan), ["reject_item"], bad)

    def test_sequence_zero_is_a_valid_first_item(self):
        self.assertEqual(ops(ac.reconcile({}, batch(it(seq=0)))), ["add"])


class Terminal(unittest.TestCase):  # 15-008, 15-009, 15-010, 15-015
    def test_an_older_deletion_never_removes_a_newer_item(self):
        local = {k(): ac.Local(5, "r5")}
        plan = ac.reconcile(local, batch(it(seq=3, rev="r3", state=ac.DELETED)))
        self.assertEqual(ops(plan), ["ignore_stale"])

    def test_a_newer_terminal_event_with_an_unsent_edit_reports_the_conflict(self):
        local = {k(): ac.Local(1, "r1", pending_edit=True)}
        self.assertEqual(ops(ac.reconcile(local, batch(it(seq=2, rev="r2", state=ac.DELETED)))), ["conflict", "remove"])
        self.assertEqual(ops(ac.reconcile(local, batch(it(seq=2, rev="r2", state=ac.DONE)))), ["conflict", "complete_verified"])

    def test_an_untracked_completion_keeps_its_sequence_so_an_older_open_is_stale(self):
        local, plans = replay([batch(it(seq=5, rev="r5", state=ac.DONE)), batch(it(seq=3, rev="r3"))])
        self.assertEqual(ops(plans[0]), ["track_done"])
        self.assertEqual(ops(plans[1]), ["ignore_stale"])

    def test_an_untracked_deletion_keeps_a_tombstone_so_an_older_open_is_stale(self):
        local, plans = replay([batch(it(seq=5, rev="r5", state=ac.DELETED)), batch(it(seq=3, rev="r3"))])
        self.assertEqual(ops(plans[0]), ["tombstone"])
        self.assertEqual(ops(plans[1]), ["ignore_stale"])
        self.assertEqual(local[k()].state, "gone")

    def test_a_same_revision_sequence_bump_advances_the_watermark(self):
        local, plans = replay([batch(it(seq=1, rev="r1")), batch(it(seq=5, rev="r1")), batch(it(seq=3, rev="r3"))])
        self.assertEqual(ops(plans[1]), ["advance"])
        self.assertEqual(ops(plans[2]), ["ignore_stale"])
        self.assertEqual(local[k()].seq, 5)

    def test_a_deleted_item_that_comes_back_is_restored(self):
        local, plans = replay([batch(it(seq=1)), batch(it(seq=2, rev="r2", state=ac.DELETED)), batch(it(seq=3, rev="r1"))])
        self.assertEqual(ops(plans[2]), ["restore"])
        self.assertEqual((local[k()].state, local[k()].revision), (ac.OPEN, "r1"))

    def test_restore_without_a_content_change_uses_the_given_example(self):
        plan = ac.reconcile({k(): ac.Local(1, "r1", "gone")}, batch(it(seq=2, rev="r1")))
        self.assertEqual(ops(plan), ["restore"])

    def test_a_gone_item_repeating_its_deletion_is_a_no_op(self):
        local = {k(): ac.Local(2, "r2", "gone")}
        self.assertEqual(ops(ac.reconcile(local, batch(it(seq=2, rev="r2", state=ac.DELETED)))), [])


class Collisions(unittest.TestCase):  # 15-011, class J
    def test_one_sequence_with_two_revisions_is_handled_the_same_across_and_within_batches(self):
        across = ac.reconcile({k(): ac.Local(1, "r1")}, batch(it(seq=1, rev="r2")))
        within = ac.reconcile({}, batch(it(seq=1, rev="r1"), it(seq=1, rev="r2")))
        self.assertEqual(ops(across), ["seq_collision"])
        self.assertEqual(sorted(ops(within)), ["add", "seq_collision"])
        self.assertEqual([a for a in within.actions if a.op == "add"][0].revision, "r1")  # the first was kept
        stored = ac.apply({k(): ac.Local(1, "r1")}, across)
        self.assertEqual(stored[k()].revision, "r1")


class Contradiction(unittest.TestCase):  # 15-012, 15-016
    def test_an_asserted_completion_is_contradicted_by_the_same_revision_still_open(self):
        local = {k(): ac.Local(1, "r1", ac.DONE, ac.ASSERTED)}
        plan = ac.reconcile(local, batch(it(seq=1, rev="r1")))
        self.assertEqual(ops(plan), ["contradicted"])
        self.assertEqual(ac.apply(local, plan)[k()].state, ac.OPEN)

    def test_the_contradiction_is_reported_once(self):
        local, plans = replay([batch(it(seq=1))], {k(): ac.Local(1, "r1", ac.DONE, ac.ASSERTED)})
        local, more = replay([batch(it(seq=1))], local)
        self.assertEqual((ops(plans[0]), ops(more[0])), (["contradicted"], []))

    def test_a_held_asserted_completion_is_still_contradicted(self):
        local = {k(): ac.Local(1, "r1", "held", ac.ASSERTED)}
        self.assertEqual(ops(ac.reconcile(local, batch(it(seq=1)))), ["release_hold", "contradicted"])

    def test_a_held_verified_completion_still_reopens(self):
        local = {k(): ac.Local(2, "r2", ac.DONE, ac.VERIFIED, held=True)}
        self.assertEqual(ops(ac.reconcile(local, batch(it(seq=3, rev="r3")))), ["release_hold", "reopen", "update"])

    def test_a_held_done_item_confirmed_done_is_verified_and_released(self):
        local = {k(): ac.Local(1, "r1", ac.DONE, ac.ASSERTED, held=True)}
        self.assertEqual(ops(ac.reconcile(local, batch(it(seq=1, state=ac.DONE)))), ["release_hold", "verify_done"])


class Unknown(unittest.TestCase):  # 15-013
    def test_an_unrecognised_state_never_becomes_open_work(self):
        local = {k(): ac.Local(2, "r2", ac.DONE, ac.VERIFIED)}
        for state in ("cancelled", "unknown", ""):
            plan = ac.reconcile(local, batch(it(seq=3, rev="r3", state=state)))
            self.assertEqual(ops(plan), ["unknown_state"], state)
            self.assertEqual(ac.apply(local, plan)[k()].state, ac.DONE, state)

    def test_an_unrecognised_state_for_an_untracked_item_adds_nothing(self):
        plan = ac.reconcile({}, batch(it(state="cancelled")))
        self.assertEqual(ops(plan), ["watermark"])  # only the sequence is kept; no card
        self.assertEqual(ac.apply({}, plan)[k()].state, "gone")


class Heuristic(unittest.TestCase):  # 15-014, class I/G
    def test_a_guessed_completion_is_not_labelled_confirmed(self):
        local = {k(): ac.Local(1, "r1")}
        plan = ac.reconcile(local, batch(it(seq=2, rev="r2", state=ac.DONE, confidence=ac.GUESSED)))
        self.assertEqual(ops(plan), ["complete_unconfirmed"])
        stored = ac.apply(local, plan)[k()]
        self.assertEqual(stored.evidence, ac.HEURISTIC)
        self.assertNotIn("confirmed at the source", ac.completion_label(stored.evidence).replace("not confirmed at the source", ""))
        self.assertIn("not confirmed", ac.completion_label(ac.HEURISTIC))

    def test_a_guess_never_downgrades_or_replaces_verified(self):
        local = {k(): ac.Local(1, "r1", ac.DONE, ac.VERIFIED)}
        self.assertEqual(ops(ac.reconcile(local, batch(it(seq=2, rev="r1", state=ac.DONE, confidence=ac.GUESSED)))), ["advance"])

    def test_a_guess_does_not_verify_an_agent_claim(self):
        local = {k(): ac.Local(1, "r1", ac.DONE, ac.ASSERTED)}
        plan = ac.reconcile(local, batch(it(seq=1, state=ac.DONE, confidence=ac.GUESSED)))
        self.assertEqual(ops(plan), ["complete_unconfirmed"])

    def test_a_heuristic_completion_is_contradicted_by_the_provider_open(self):
        local = {k(): ac.Local(1, "r1", ac.DONE, ac.HEURISTIC)}
        self.assertEqual(ops(ac.reconcile(local, batch(it(seq=2, rev="r1")))), ["contradicted"])

    def test_an_untracked_guess_is_recorded_as_a_guess(self):
        plan = ac.reconcile({}, batch(it(state=ac.DONE, confidence=ac.GUESSED)))
        self.assertEqual(plan.actions[0].evidence, ac.HEURISTIC)


class Holds(unittest.TestCase):  # 15-003
    def test_reauthorised_incremental_read_with_no_changes_releases_every_held_item(self):
        local = {k("a"): ac.Local(1, "r1"), k("b"): ac.Local(1, "r1", ac.DONE, ac.VERIFIED)}
        local, _ = replay([ac.ChangeBatch(status="access_revoked", accounts=(A,))], local)
        self.assertTrue(all(v.held for v in local.values()))
        local, plans = replay([batch(cursor="c2")], local)
        self.assertEqual(sorted(ops(plans[0])), ["release_hold", "release_hold"])
        self.assertFalse(any(v.held for v in local.values()))
        self.assertEqual(local[k("b")].state, ac.DONE)

    def test_a_partial_read_does_not_release_what_it_did_not_cover(self):
        local = {k("a"): ac.Local(1, "r1", held=True)}
        plan = ac.reconcile(local, batch(complete=False, stop_reason="item_limit", omitted=3))
        self.assertEqual(ops(plan), [])

    def test_another_accounts_holds_stay(self):
        local = {k("a", "B"): ac.Local(1, "r1", held=True)}
        self.assertEqual(ops(ac.reconcile(local, batch())), [])

    def test_a_revocation_is_not_repeated_for_items_already_held(self):
        local = {k(): ac.Local(1, "r1", held=True)}
        self.assertEqual(ops(ac.reconcile(local, ac.ChangeBatch(status="access_revoked", accounts=(A,)))), [])


class LeftView(unittest.TestCase):  # 15-020, class G
    def test_a_message_that_left_the_folder_is_held_not_removed(self):
        local = {k(): ac.Local(1, "r1")}
        plan = ac.reconcile(local, batch(it(seq=2, rev="r1", state=ac.LEFT_VIEW)))
        self.assertEqual(ops(plan), ["hold"])
        stored = ac.apply(local, plan)[k()]
        self.assertTrue(stored.held)
        self.assertEqual(stored.state, ac.OPEN)

    def test_a_moved_message_that_reappears_is_released_not_duplicated(self):
        local, plans = replay([batch(it(seq=1)), batch(it(seq=2, state=ac.LEFT_VIEW)), batch(it(seq=3))])
        self.assertEqual(ops(plans[2]), ["release_hold", "advance"])
        self.assertFalse(local[k()].held)

    def test_leaving_the_view_never_creates_a_card(self):
        plan = ac.reconcile({}, batch(it(state=ac.LEFT_VIEW)))
        self.assertEqual(ops(plan), ["watermark"])  # only the sequence is kept; no card
        self.assertEqual(ac.apply({}, plan)[k()].state, "gone")


class Imap(unittest.TestCase):  # 15-019
    def test_two_mailboxes_with_the_same_uid_and_validity_get_different_ids(self):
        self.assertNotEqual(ac.imap_external_id("Drafts", 100, 1), ac.imap_external_id("Archive", 100, 1))

    def test_the_id_changes_with_the_validity_and_hides_the_folder_name(self):
        a, b = ac.imap_external_id("Drafts", 100, 1), ac.imap_external_id("Drafts", 101, 1)
        self.assertNotEqual(a, b)
        self.assertNotIn("Drafts", a)

    def test_two_messages_in_two_mailboxes_stay_separate_in_the_plan(self):
        one = ac.ExternalItem("imap", "A", ac.imap_external_id("Drafts", 100, 1), 1, "r1")
        two = ac.ExternalItem("imap", "A", ac.imap_external_id("Archive", 100, 1), 1, "r1")
        plan = ac.reconcile({}, ac.ChangeBatch(items=(one, two), accounts=(("imap", "A"),)))
        self.assertEqual(ops(plan), ["add", "add"])

    def test_invalid_parts_are_refused(self):
        for args in (("", 1, 1), ("Drafts", 0, 1), ("Drafts", 1, True), ("Drafts", 1, "1")):
            with self.assertRaises(ValueError):
                ac.imap_external_id(*args)


class Stateful(unittest.TestCase):  # 15-021, class L
    def test_the_same_item_in_two_successful_batches_adds_once(self):
        local, plans = replay([batch(it(), cursor="c1"), batch(it(), cursor="c2")])
        self.assertEqual([ops(p) for p in plans], [["add"], []])
        self.assertEqual(len(local), 1)

    def test_a_full_lifecycle_replays_to_the_right_state(self):
        local, plans = replay([
            batch(it(seq=1, rev="r1")),
            batch(it(seq=2, rev="r2")),
            batch(it(seq=3, rev="r2", state=ac.DONE)),
            batch(it(seq=4, rev="r4")),
            batch(it(seq=2, rev="r2")),  # a late old event
        ])
        self.assertEqual([ops(p) for p in plans],
                         [["add"], ["update"], ["complete_verified"], ["reopen", "update"], ["ignore_stale"]])
        self.assertEqual((local[k()].state, local[k()].seq, local[k()].revision), (ac.OPEN, 4, "r4"))

    def test_revoke_reauthorise_and_recover(self):
        local, plans = replay([batch(it(seq=1)), ac.ChangeBatch(status="access_revoked", accounts=(A,)),
                               ac.ChangeBatch(status="rate_limited", retry_after=3.0), batch(cursor="c3")])
        self.assertEqual([ops(p) for p in plans], [["add"], ["hold"], [], ["release_hold"]])
        self.assertFalse(local[k()].held)

    def test_an_agent_claim_survives_revocation_and_is_then_contradicted(self):
        local = {k(): ac.Local(1, "r1", ac.DONE, ac.ASSERTED)}
        local, plans = replay([ac.ChangeBatch(status="access_revoked", accounts=(A,)), batch(it(seq=1))], local)
        self.assertEqual([ops(p) for p in plans], [["hold"], ["release_hold", "contradicted"]])
        self.assertEqual(local[k()].state, ac.OPEN)

    def test_revisions_inside_a_batch_then_the_next_batch_repeating_them(self):
        local, plans = replay([batch(it(seq=1, rev="r1"), it(seq=2, rev="r2")), batch(it(seq=1, rev="r1"), it(seq=2, rev="r2"))])
        self.assertEqual([ops(p) for p in plans], [["add", "update"], ["ignore_stale"]])
        self.assertEqual((local[k()].seq, local[k()].revision), (2, "r2"))

    def test_a_rejected_batch_changes_nothing_in_the_replay(self):
        local, _ = replay([batch(it(seq=1))])
        again, plans = replay([batch(it(seq=2, rev="r2", state=ac.DELETED), status="error")], local)
        self.assertEqual(again, local)


class Docs(unittest.TestCase):  # 15-017, 15-018, 15-019, 15-020, class I
    def setUp(self):
        self.doc = (ROOT / "docs/provider-integrations.md").read_text()
        self.rows = {line.split("|")[1].strip(): line for line in self.doc.splitlines() if line.startswith("| ")}

    def test_the_imap_row_does_not_need_sent_inside_its_minimum_read(self):
        row = self.rows["Generic IMAP (Drafts folder)"]
        self.assertIn("Sent is a separate read", row)
        self.assertIn("approved on its own", row)
        self.assertIn("completion stays unknown", row)

    def test_authentication_failures_are_not_rate_limits(self):
        row = self.rows["Generic IMAP (Drafts folder)"]
        for code in ("AUTHENTICATIONFAILED", "AUTHORIZATIONFAILED", "EXPIRED"):
            self.assertIn(code, row)
        self.assertNotIn("treat a refusal as rate limited", self.doc)
        self.assertIn("LIMIT", row)

    def test_imap_ids_include_the_mailbox(self):
        row = self.rows["Generic IMAP (Drafts folder)"]
        self.assertIn("mailbox", row.lower())
        self.assertIn("imap_external_id", self.doc)

    def test_outlook_removed_is_not_a_deletion(self):
        row = self.rows["Microsoft 365 / Outlook (Graph)"]
        self.assertIn("left_view", row)
        self.assertIn("reason", row)
        self.assertNotIn("| `@removed` in delta |", row)

    def test_an_imap_vanished_uid_is_not_a_deletion_either(self):
        row = self.rows["Generic IMAP (Drafts folder)"]
        self.assertIn("vanished UID is `left_view`", row)

    def test_caldav_removed_and_notion_archived_are_not_deletions(self):
        caldav = self.rows["CalDAV tasks (Apple Reminders, Nextcloud)"]
        notion = self.rows["Notion"]
        self.assertIn("is `left_view`", caldav)
        self.assertIn("moved to another list", caldav)
        self.assertNotIn("| Removed in sync report |", caldav)
        self.assertIn("is `left_view`", notion)
        self.assertIn("restorable", notion)
        self.assertNotIn("| Archived or trashed |", notion)

    def test_the_rules_for_coverage_scope_and_sequences_are_documented(self):
        for phrase in ("complete", "stop_reason", "account scope", "non-negative", "tombstone", "heuristic", "seq_collision"):
            self.assertIn(phrase, self.doc, phrase)


if __name__ == "__main__":
    unittest.main()
