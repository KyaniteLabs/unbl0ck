"""Regression coverage for Kanban queue classification."""
import unittest
from test_needs import needs


class QueueDetectionControls(unittest.TestCase):
    def test_mixed_choice_still_needs_principal(self):
        self.assertTrue(needs.kanban_needs_you("The coordinator dispatches it, or Ada decides which route to take.", "needs_input"))

    def test_negative_controls(self):
        for reason in ["no new Ada approval requested", "Ada says: run the installer", "Missing a host capability", "The agent can run sudo itself"]:
            with self.subTest(reason=reason):
                self.assertIsNone(needs.kanban_needs_you(reason, "capability"))

    def test_unqualified_owner_and_configured_owner_still_gate(self):
        from unittest.mock import patch
        self.assertTrue(needs.kanban_needs_you('Owner decision needed: A or B.', None))
        with patch.object(needs, '_settings', lambda: {'principal_name': 'Ada', 'role_words': 'owner'}):
            self.assertTrue(needs.kanban_needs_you('Org owner decision needed.', None))
