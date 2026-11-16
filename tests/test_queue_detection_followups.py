"""Regression coverage for Kanban queue classification."""
import unittest
from test_needs import needs


class QueueDetectionFollowups(unittest.TestCase):
    def test_natural_decisions(self):
        for reason in [
            "Missing input = Ada's pick (A, B or C).",
            "Missing input = Ada's icon pick (A, B or C).",
            "Held per Ada's own ruling on this card: the sitting is still open.",
            "The card routes the outstanding retention decision to Ada.",
            "Waiting for Ada's verdict on the redesign.",
        ]:
            with self.subTest(reason=reason):
                self.assertTrue(needs.kanban_needs_you(reason, "capability"))

    def test_manual_steps(self):
        for reason in [
            "All of our work is done and merged; one root command on the host remains, which policy denies to this seat. One paste finishes it: sudo bash install.sh",
            "Ada must run the staged root installer.",
            "Ada to paste the staged command.",
        ]:
            with self.subTest(reason=reason):
                self.assertTrue(needs.kanban_needs_you(reason, "capability"))

    def test_other_parties_do_not_make_principal_asks(self):
        for reason in [
            "Owner decision needed: the org owner adds the required check, or the card owner re-issues the card.",
            "Robin/operator input is needed to reconcile a contradictory judge.",
            "Deliverables are done. Remaining: Security review on the PR before Platform merges; Robin's overlap check. Ada's batch is tracked on another card.",
        ]:
            with self.subTest(reason=reason):
                self.assertIsNone(needs.kanban_needs_you(reason, "needs_input"))

    def test_created_blocked_uses_title_or_body_but_not_old_context(self):
        from test_needs import fake_db, fake_connect, fake_hermes, task, block_event
        for reason, title, body, tier in [
            ('initial_status', 'APPROVAL HOLD: publish (Ada decides)', '', 'ask'),
            ('', 'Publish', "Waiting for Ada's pick", 'ask'),
            ('initial_status', 'Repair CI', '', 'stuck'),
            ('Waiting on CI', 'Ada decides', '', 'stuck'),
        ]:
            with self.subTest(reason=reason, title=title):
                card = task(title=title, body=body)
                db = fake_db(list_tasks=lambda c, status=None: [card] if status == 'blocked' else [],
                             list_events=lambda c, t: [block_event(reason=reason, kind=None)])
                with fake_hermes(db, fake_connect()):
                    items = needs.kanban_items(['default'])
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]['tier'], tier)
                if tier == 'ask':
                    self.assertIn('Ada', items[0]['ask'])

    def test_handoff_needs_an_unfinished_human_step(self):
        for reason in [
            'No manual step remains; the sudo command was already run.',
            'Security must run the staged command with sudo; Ada already answered.',
            'The build for Ada is complete; Security review remains.',
            'Owner decision needed from Robin.',
            "Robin's operator input is needed.",
        ]:
            with self.subTest(reason=reason):
                self.assertIsNone(needs.kanban_needs_you(reason, 'needs_input'))

    def test_missing_input_names_the_principal(self):
        self.assertTrue(needs.kanban_needs_you('Missing input = Ada: choose A or B.', None))

    def test_completed_delivery_is_not_an_outstanding_decision(self):
        self.assertIsNone(needs.kanban_needs_you('The decision was sent to Ada yesterday; waiting on CI.', 'needs_input'))

    def test_multiword_principal_is_not_another_owner(self):
        from unittest.mock import patch
        with patch.object(needs, '_settings', lambda: {'principal_name': 'Ada Lovelace'}):
            self.assertTrue(needs.kanban_needs_you('Owner decision needed from Ada Lovelace.', None))
            self.assertIsNone(needs.kanban_needs_you('Owner decision needed from Ada Other.', None))

    def test_explicit_please_handoff_after_agent_inability(self):
        self.assertTrue(needs.kanban_needs_you('We cannot run sudo. Please run the staged command.', 'capability'))

    def test_completed_new_gate_phrases_do_not_queue(self):
        for reason in [
            'The decision to Ada was sent yesterday; waiting on CI.',
            "Ada's previous ruling was implemented. Waiting on CI.",
        ]:
            with self.subTest(reason=reason):
                self.assertIsNone(needs.kanban_needs_you(reason, 'needs_input'))
