"""Round-4 findings 01-085 and 01-088 .. 01-096: what the Ask feature and the pocket pack send."""

import json
import time
import unittest
from unittest.mock import patch

from test_r4_stack01d_kanban import KanbanAnswerCase, kanban_db, needs


class CardContextReadsAreBoundedByTheQuery(KanbanAnswerCase):
    def fill(self, task, events, comments):
        now = int(time.time())
        with self.conn() as c:
            c.executemany(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'note', '{}', ?)",
                [(task, now + i) for i in range(events)],
            )
            c.executemany(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'w', ?, ?)",
                [(task, f"comment {i}", now + i) for i in range(comments)],
            )
            c.commit()

    def test_a_long_history_is_never_materialized_whole(self):
        """01-085 (class D): the newest 20 events and 50 comments come from a limited query."""
        task = self.card()
        self.fill(task, 3000, 600)

        def forbidden(*args, **kwargs):
            raise AssertionError("the whole history was loaded")

        with patch.object(kanban_db, "list_events", forbidden), patch.object(
            kanban_db, "list_comments", forbidden
        ):
            data = needs.card_context("default", task)
        self.assertEqual(len(data["events"]), 20)
        self.assertEqual(len(data["comments"]), 50)
        self.assertEqual(data["comments"][-1]["body"], "comment 599")
        self.assertEqual(data["comments"][0]["body"], "comment 550")
        self.assertGreaterEqual(data["omitted"]["events"], 2900)
        self.assertEqual(data["omitted"]["comments"], 550)

    def test_the_block_and_version_still_come_from_the_latest_block(self):
        task = self.card()
        self.fill(task, 100, 3)
        data = needs.card_context("default", task)
        self.assertEqual(data["version"], self.version(task))
        self.assertEqual(data["block"]["reason"], "Needs CEO approval")


SYNTH = "Zq81LmK4vP"


def sent(card, question="What now?", history=()):
    return needs.build_ask_messages(card, [*history, {"role": "user", "content": question}])


def card_json(messages):
    body = messages[1]["content"]
    start = body.index("```json\n") + len("```json\n")
    return json.loads(body[start : body.index("\n```", start)])


class NumericCredentialsAreStillCredentials(unittest.TestCase):
    def test_digit_only_values_under_credential_keys_are_redacted(self):
        """01-088 (class K): the digits are the secret; only the key can tell."""
        for text, secret in (
            ("login with password=123456 today", "123456"),
            ("api_key=9876543210", "9876543210"),
            ("secret: 55512345", "55512345"),
            ("PASSWORD = 424242", "424242"),
        ):
            with self.subTest(text=text):
                self.assertNotIn(secret, needs._ask_redact(text))

    def test_numeric_json_values_under_credential_keys_are_redacted(self):
        view = needs._ask_card_view({"password": 123456, "api_key": "9876543210"})
        self.assertEqual(view["password"], "[redacted]")
        self.assertEqual(view["api_key"], "[redacted]")

    def test_counts_and_flags_stay_readable(self):
        for text in ("max_tokens: 4000", "password_attempts: 3", "token_count=5", "3 of 4 passed"):
            with self.subTest(text=text):
                self.assertEqual(needs._ask_redact(text), text)
        self.assertEqual(needs._ask_card_view({"password": True})["password"], True)


class EmbeddedJsonCannotHideASecret(unittest.TestCase):
    def test_an_escaped_credential_key_is_still_a_credential_key(self):
        """01-092 (class K): the key is decoded before it is judged."""
        escaped = "".join(f"\\u{ord(c):04x}" for c in "password")
        text = '{"%s":"%s"}' % (escaped, SYNTH)
        self.assertNotIn(SYNTH, needs._ask_redact(text))
        self.assertNotIn(SYNTH, needs._ask_redact('note {"\\u0070assword":"%s"} end' % SYNTH))

    def test_many_leading_objects_do_not_exhaust_the_scan(self):
        """01-095 (class G): successes are not counted against the scan budget."""
        text = "{}" * 129 + '{"name":"DB_PASSWORD","metadata":"env","value":"%s"}' % SYNTH
        self.assertNotIn(SYNTH, needs._ask_redact(text))

    def test_an_exhausted_scan_withholds_the_remainder_rather_than_copying_it(self):
        text = "{x" * 300 + '{"name":"DB_PASSWORD","value":"%s"}' % SYNTH
        self.assertNotIn(SYNTH, needs._ask_redact(text))


class WorkspacePathIsRemovedEverywhere(unittest.TestCase):
    def test_a_repeated_workspace_path_does_not_reach_the_model(self):
        """01-096 (class K): the path is removed from every field, not only its own."""
        path = "/srv/private/repo"
        card = {
            "title": "Fix it",
            "workspace": {"kind": "dir", "path": path, "branch": "main"},
            "body": f"Edit {path}/src/app.py",
            "comments": [{"body": f"see {path}"}],
            "events": [{"payload": {"cwd": path}}],
        }
        out = json.dumps(card_json(sent(card)))
        self.assertNotIn(path, out)
        self.assertIn("src/app.py", out)


class QuestionAndBlockReasonAreNotStarved(unittest.TestCase):
    def big_card(self, **extra):
        return {
            "title": "Choose",
            "body": "B" * 30000,
            "block": {"reason": "Decide: ship or hold?"},
            "comments": [{"body": "C" * 5000}],
            **extra,
        }

    def test_a_huge_description_does_not_push_out_the_block_question(self):
        """01-089 (class D): the decision is budgeted before the description."""
        data = card_json(sent(self.big_card()))
        self.assertIn("ship or hold", data["block"]["reason"])
        self.assertIn("[truncated]", data["body"])

    def test_the_card_data_is_always_valid_json(self):
        card = self.big_card(comments=[{"body": '"quoted"\n' * 3000} for _ in range(5)])
        card_json(sent(card))

    def test_a_long_question_is_sent_whole_beside_a_large_card(self):
        """01-090 (class E): the card shrinks, the operator's question does not."""
        question = "A" * 19000 + " DO NOT DEPLOY"
        messages = sent(self.big_card(), question)
        self.assertEqual(messages[-1]["content"], question)
        self.assertLessEqual(sum(len(m["content"]) for m in messages), needs.ASK_PROMPT_BUDGET)


def kanban_item(**changes):
    item = {
        "id": "kanban:ops:t_1",
        "version": "v1",
        "source": "kanban",
        "board": "ops",
        "task_id": "t_1",
        "title": "Choose a database",
        "ask": "Pick one",
        "why": "Blocked on you",
        "tier": "ask",
        "status": "blocked",
        "owner": "worker",
    }
    return {**item, **changes}


class PocketPackIsBoundAndHonest(unittest.TestCase):
    def pack(self, *items, **kw):
        return needs.pocket_pack({"items": list(items)}, **kw)

    def test_the_unblock_option_names_the_board_and_the_block(self):
        """01-091 (class A): an action that does not say which block it answers can hit a later one."""
        (card,) = self.pack(kanban_item())["cards"]
        sends = [o["send"] for o in card["options"]]
        self.assertTrue(all("ops" in s and "v1" in s for s in sends), sends)

    def test_a_triage_card_is_not_offered_an_unblock(self):
        (card,) = self.pack(kanban_item(status="triage", tier="triage"))["cards"]
        self.assertFalse(any("unblock" in o["send"].lower() for o in card["options"]), card["options"])
        self.assertTrue(card["options"])

    def test_a_truncated_source_is_flagged_not_called_complete(self):
        """01-093 (class E): a cut question or file says so in the pack."""
        pack = self.pack(
            kanban_item(id="chat:1", source="chat", ask_truncated=True, profile="p"),
            kanban_item(id="outside:2", source="outside", truncated=True, act_where="Mail"),
        )
        self.assertTrue(all(c["truncated"] and c["warning"] for c in pack["cards"]), pack["cards"])
        self.assertFalse(pack["complete"])

    def test_an_untouched_pack_is_complete(self):
        pack = self.pack(kanban_item())
        self.assertTrue(pack["complete"])
        self.assertFalse(pack["cards"][0]["truncated"])

    def test_dropped_cards_make_the_pack_incomplete(self):
        pack = self.pack(*[kanban_item(id=f"kanban:ops:t_{i}", task_id=f"t_{i}") for i in range(8)], limit=6)
        self.assertEqual(len(pack["cards"]), 6)
        self.assertEqual(pack["omitted"], 2)
        self.assertFalse(pack["complete"])

    def test_a_huge_block_reason_does_not_make_a_huge_pack(self):
        """01-094 (class D): prompt text has a per-card and a total budget, disclosed."""
        pack = self.pack(*[kanban_item(id=f"kanban:ops:t_{i}", task_id=f"t_{i}", ask="x" * 1_048_576) for i in range(6)])
        self.assertLess(len(json.dumps(pack)), 64 * 1024)
        self.assertTrue(all(c["truncated"] for c in pack["cards"]))
        self.assertTrue(pack["cards"][0]["prompt"].endswith("[truncated]"))
        self.assertFalse(pack["complete"])


class AskDocs(unittest.TestCase):
    def test_the_readme_states_what_ask_removes_and_cuts(self):
        """01-089/01-090/01-096 (class I): the README matches what Ask now sends."""
        from pathlib import Path

        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        self.assertIn("wherever it appears in the card text", readme)
        self.assertIn("end in `[truncated]`", readme)
        self.assertIn("your own question is sent whole", readme)

    def test_the_readme_states_what_the_pocket_pack_bounds_and_names(self):
        """01-091/01-093/01-094 (class I)."""
        from pathlib import Path

        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        self.assertIn("`complete` is false when text was cut or cards were left out", readme)
        self.assertIn("names the board and block in every Kanban action", readme)
        self.assertIn("offers no unblock for block-loop cards", readme)


if __name__ == "__main__":
    unittest.main()
