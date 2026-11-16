"""The card view Ask sends is one declared table, ``needs.ASK_CARD_VIEW`` (#71). This file pins that
table: sending one more card field to the model provider means editing the expected list below,
so a widened allowlist is a reviewed change, never a side effect."""

import unittest

from test_outside import needs

EXPECTED = [
    ("id", "field", ("id",), ()),
    ("title", "field", ("title",), ()),
    ("block", "part", ("block",), ("reason", "kind", "recurrences", "limit", "since")),
    ("status", "field", ("status",), ()),
    ("priority", "field", ("priority",), ()),
    ("assignee", "field", ("assignee",), ()),
    ("created_by", "field", ("created_by",), ()),
    ("consecutive_failures", "field", ("consecutive_failures",), ()),
    ("comments", "rows", ("comments",), ("author", "body", "created_at")),
    ("last_run", "part", ("last_run",), ("status", "outcome", "summary", "error", "ended_at")),
    ("events", "rows", ("events",), ("kind", "created_at")),
    ("branch", "field", ("workspace", "branch"), ()),
    ("pr", "field", ("pr", "reference"), ()),
    ("omitted", "part", ("omitted",), ("events", "comments", "attachments")),
    ("result", "field", ("result",), ()),
    ("last_failure_error", "field", ("last_failure_error",), ()),
    ("body", "field", ("body",), ()),
]


def full_card():
    """A card with every allowed field set, plus fields that must never be sent."""
    card = {key: f"v-{key}" for key, shape, path, _ in EXPECTED if shape == "field" and len(path) == 1}
    card.update(
        block={"reason": "r", "kind": "k", "recurrences": "1", "limit": "2", "since": "s", "payload": "NOPE1"},
        last_run={"status": "s", "outcome": "o", "summary": "m", "error": "e", "ended_at": "t", "log": "NOPE2"},
        omitted={"events": "1", "comments": "2", "attachments": "3"},
        comments=[{"author": "a", "body": "b", "created_at": "c", "id": "NOPE3"}],
        events=[{"kind": "k", "created_at": "c", "payload": "NOPE4"}],
        workspace={"branch": "main", "path": "/srv/NOPE5"},
        pr={"reference": "#1", "token": "NOPE6"},
        attachments=[{"content": "NOPE7"}],
        links=[{"id": "NOPE8"}],
        future_hermes_field="NOPE9",
    )
    return card


class AskCardView(unittest.TestCase):
    def test_the_allowlist_is_exactly_the_reviewed_one(self):
        self.assertEqual(list(needs.ASK_CARD_VIEW), EXPECTED)

    def test_the_payload_has_the_table_keys_in_the_table_order(self):
        payload = needs._ask_card_payload(full_card(), 100_000)
        self.assertEqual(list(payload), [key for key, *_ in EXPECTED])
        for key, shape, _, fields in EXPECTED:
            if shape == "part":
                self.assertEqual(list(payload[key]), list(fields))
            elif shape == "rows":
                self.assertEqual([list(r) for r in payload[key]], [list(fields)])

    def test_no_field_outside_the_table_reaches_the_model(self):
        content = needs.build_ask_messages(full_card(), [])[1]["content"]
        self.assertNotIn("NOPE", content)
        self.assertIn("v-last_failure_error", content)


if __name__ == "__main__":
    unittest.main()
