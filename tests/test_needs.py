import contextlib
import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("unbl0ck_needs", ROOT / "needs.py")
needs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(needs)
needs._settings = lambda: {"principal_name": "Ada", "comment_author": "ada"}


@contextlib.contextmanager
def fake_hermes(kanban_db, kanban_db_connect):
    pkg = types.ModuleType("hermes_cli")
    pkg.kanban_db, pkg.kanban_db_connect = kanban_db, kanban_db_connect
    names = ("hermes_cli", "hermes_cli.kanban_db", "hermes_cli.kanban_db_connect")
    saved = {name: sys.modules.get(name) for name in names}
    sys.modules.update(
        {
            "hermes_cli": pkg,
            "hermes_cli.kanban_db": kanban_db,
            "hermes_cli.kanban_db_connect": kanban_db_connect,
        }
    )
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def fake_db(**overrides):
    module = types.ModuleType("hermes_cli.kanban_db")
    defaults = {
        "get_task": lambda conn, task_id: None,
        "list_tasks": lambda conn, status=None: [],
        "list_events": lambda conn, task_id: [],
        "list_comments": lambda conn, task_id: [],
        "add_comment": lambda conn, task_id, author, body: 1,
        "unblock_task": lambda conn, task_id: True,
        "latest_run": lambda conn, task_id: None,
        "task_graph_context": lambda conn, task_id: {"parents": [], "children": []},
        "list_attachments": lambda conn, task_id: [],
    }
    defaults.update(overrides)
    for key, value in defaults.items():
        setattr(module, key, value)
    module.write_txn = contextlib.contextmanager(
        lambda conn, allow_nested=False: (yield conn)
    )
    return module


def fake_connect(**overrides):
    module = types.ModuleType("hermes_cli.kanban_db_connect")
    module.connect = overrides.pop(
        "connect", lambda board=None: sqlite3.connect(":memory:")
    )
    return module


class Classify(unittest.TestCase):
    def test_gate_reasons_need_you(self):
        for r in [
            "Awaiting Ada's approval to publish",
            "CEO-gated: repo publication",
            "needs Ada to pick option A or B",
            "human gate: credentials",
        ]:
            self.assertTrue(needs.kanban_needs_you(r, None), r)

    def test_needs_input_naming_principal(self):
        self.assertTrue(
            needs.kanban_needs_you("Which port should Ada want here", "needs_input")
        )
        self.assertIsNone(needs.kanban_needs_you("Which port", "needs_input"))

    def test_not_asks(self):
        self.assertIsNone(needs.kanban_needs_you("waiting on CI", None))
        self.assertIsNone(needs.kanban_needs_you("", "needs_input"))
        # a stop order that names Ada is not a question for Ada
        self.assertIsNone(
            needs.kanban_needs_you(
                "Ada 18:07 PT: Claude Code supervisor owns this repair; needs Ada later",
                None,
            )
        )

    def test_live_false_positives_drop(self):
        cases = [
            (
                "Superseded. Dead card. Do not retry. Path /home/ada/workspaces/demo/app",
                "needs_input",
            ),
            ("Ask the operator in the fleet channel", "needs_input"),
            ("Ada-reserved slot", "needs_input"),
            ("no new Ada approval requested", None),
            ("Owner-domain input, not a principal gate", "needs_input"),
        ]
        for reason, kind in cases:
            self.assertIsNone(needs.kanban_needs_you(reason, kind), reason)

    def test_second_person_forms_detected(self):
        for reason in [
            "Waiting for your approval",
            "Needs your sign-off before deploy",
            "Waiting on your decision: A or B",
            "needs your input on the credentials",
            "blocked pending your go",
            "Which option do you prefer",
        ]:
            self.assertTrue(needs.kanban_needs_you(reason, None), reason)

    def test_negated_second_person_forms_drop(self):
        self.assertIsNone(needs.kanban_needs_you("not waiting on your approval", None))
        self.assertIsNone(needs.kanban_needs_you("your approval is not needed", None))

    def test_chat_asks(self):
        self.assertTrue(needs.chat_asks_you("Done drafting. Your call: ship A or B?"))
        self.assertTrue(needs.chat_asks_you("Should I open the PR now?"))
        self.assertFalse(needs.chat_asks_you("Deployed v6. All 914 tests pass."))
        long_packet = (
            "Four picks for the CEO:\n1. Ship A.\n2. Park B.\n" + "Detail line. " * 120
        )
        self.assertTrue(needs.chat_asks_you(long_packet))
        self.assertFalse(needs.chat_asks_you("Is the build green? Yes."[:0]))

    def test_negation_in_another_clause_keeps_the_ask(self):
        # Audit probe: 13 of 14 real asks were dropped by a negation window that
        # reached across clauses and looked after the gate phrase.
        for reason, kind in [
            ("Cannot proceed: needs CEO approval to merge", None),
            ("Blocked: can't deploy without your approval", None),
            ("Waiting on Ada's decision; no rush", None),
            ("Needs your approval. Not urgent.", "needs_input"),
            (
                "Tests don't pass on main; awaiting Ada to pick option A or B",
                "needs_input",
            ),
            ("I could not log in. Ada must sign in to the registry", "needs_input"),
            (
                "Finish needs a decision only a human can give: no model credentials on this host",
                "needs_input",
            ),
            ("CEO says: need your approval for the spend", "needs_input"),
            ("Need the human to approve", None),
            ("Please approve the release", "needs_input"),
            ("Waiting for approval from Ada", None),
            ("Requires sign-off from the CEO", None),
        ]:
            self.assertTrue(needs.kanban_needs_you(reason, kind), reason)

    def test_comma_ends_a_negation_clause(self):
        self.assertTrue(
            needs.kanban_needs_you("Not yet done, needs CEO approval", None)
        )

    def test_approval_conditions_are_asks_but_denials_remain_negated(self):
        for reason in [
            "Won't merge without CEO approval",
            "Do not deploy until CEO approval",
            "Can't deploy without your approval",
            "Blocked: can't deploy without your approval",
            "Nobody can release this without Ada's sign-off",
            "None of this can ship without CEO approval",
            "PR is done, but nobody can merge without Ada's approval",
        ]:
            with self.subTest(reason=reason):
                self.assertTrue(needs.kanban_needs_you(reason, None), reason)
        for reason in ["No CEO approval needed", "Not waiting on CEO approval"]:
            with self.subTest(reason=reason):
                self.assertIsNone(needs.kanban_needs_you(reason, None), reason)

    def test_permission_words_do_not_hide_real_approval_gates(self):
        asks = [
            "We are not proceeding without CEO approval",
            "We are not continuing without CEO approval",
            "We aren't continuing without CEO approval",
            "Nothing is fixed until CEO approves",
            "The issue is not resolved until CEO approves",
            "Releasing the fixed version without CEO approval is blocked by policy.",
            "Shipping the resolved patch without CEO approval breaks policy.",
            "Continuing without CEO sign-off is not allowed.",
            "Continuing is impossible without CEO approval",
            "The conflict is resolved yet the merge waits until CEO signs off",
            "Is it safe to ship without CEO approval",
            "The fixed build is stuck without CEO approval",
            "Fixed scope work is paused until CEO decides",
            "We continued on staging while prod is frozen until CEO approval",
        ]
        for reason in asks:
            with self.subTest(reason=reason):
                self.assertTrue(needs.kanban_needs_you(reason, None), reason)

        permitted = [
            "Can proceed without CEO approval once CI is green",
            "Proceeded without CEO approval",
            "The issue is resolved without CEO approval",
        ]
        for reason in permitted:
            with self.subTest(reason=reason):
                self.assertIsNone(needs.kanban_needs_you(reason, None), reason)

    def test_plural_no_subjects_do_not_create_false_approval_asks(self):
        for reason in [
            "No new failures we can deploy without CEO approval.",
            "No blockers remain we can merge without CEO sign-off.",
        ]:
            with self.subTest(reason=reason):
                self.assertIsNone(needs.kanban_needs_you(reason, None), reason)

    def test_until_holds_are_gates_but_negation_is_scoped_to_its_clause(self):
        for reason in [
            "Hold until CEO approval",
            "Holding until CEO approval",
            "On hold until Ada's approval",
            "Paused until Ada's sign-off",
            "Waiting until CEO approval",
            "Deploy held until Ada approval",
            "Release is pending until Ada approval",
            "Parked until CEO approval",
            "Holding without Ada's approval",
        ]:
            with self.subTest(reason=reason):
                self.assertTrue(needs.kanban_needs_you(reason, None), reason)

    def test_waiting_until_a_gate_stays_an_ask(self):
        for reason in [
            "Held until CEO approval",
            "Paused until CEO approval",
            "Stopped until CEO approval",
            "Frozen until CEO approval",
            "Halted until CEO approval",
            "Suspended until CEO approval",
            "Stuck until CEO approval",
            "Deferred until CEO approval",
            "Queued until CEO approval",
            "Pending until CEO approval",
            "On hold until CEO's decision",
            "Waiting until CEO sign-off",
            "Deploy is on hold until CEO approval",
            "Holding the release until CEO approval",
            "Release waits until CEO approval",
            "Nothing ships until CEO approval",
            "No action until CEO approval",
            "Idle until CEO reply",
            "Unable to proceed until CEO approval",
            "Refusing to merge without CEO approval",
            "Shouldn't deploy until CEO approval",
            "Until CEO's decision, this is paused",
            "I am waiting until I get CEO approval",
        ]:
            with self.subTest(reason=reason):
                self.assertTrue(needs.kanban_needs_you(reason, "needs_input"), reason)
        for reason in ["No CEO approval needed", "No approval needed until CEO review"]:
            with self.subTest(reason=reason):
                self.assertIsNone(needs.kanban_needs_you(reason, "needs_input"), reason)

    def test_negation_in_another_conjunct_does_not_turn_without_into_a_gate(self):
        self.assertIsNone(
            needs.kanban_needs_you(
                "Not urgent and we can ship without Ada's approval", None
            )
        )

    def test_without_until_gate_requires_a_prohibition_in_the_same_clause(self):
        cases = [
            ("Won't merge without explicit CEO approval", "needs_input", True),
            ("Blocked: won't merge without CEO approval", None, True),
            (
                "Blocked on CI flake; can ship without CEO approval",
                "needs_input",
                False,
            ),
            ("Can proceed without CEO approval once CI is green", "needs_input", False),
            ("Proceeded without CEO approval", "needs_input", False),
            ("Works without CEO input", "needs_input", False),
            ("No change needed until CEO review next quarter", "needs_input", False),
            ("Not urgent; can merge without approval", "needs_input", False),
        ]
        for reason, kind, expected in cases:
            with self.subTest(reason=reason):
                actual = needs.kanban_needs_you(reason, kind) is not None
                self.assertEqual(actual, expected)
        with mock.patch.object(
            needs, "_settings", lambda: {"principal_name": "operator"}
        ):
            for reason in [
                "Can't start until the operator gives the go",
                "Not proceeding without the operator's explicit decision",
            ]:
                with self.subTest(reason=reason):
                    self.assertTrue(
                        needs.kanban_needs_you(reason, "needs_input"), reason
                    )

    def test_chat_non_asks_drop(self):
        for text in [
            "Done. The file is saved at /tmp/out.txt. Anything else you need?",
            "Here is your link: https://example.com/search?q=1",
            "I compared which option was faster and went with B. All tests pass.",
            "Summary: decision packet archived last week. Nothing pending.",
            "Why did the build fail? Because your lockfile was stale. Fixed.",
        ]:
            self.assertFalse(needs.chat_asks_you(text), text)
        self.assertTrue(needs.chat_asks_you("Should I delete the production database?"))
        self.assertTrue(
            needs.chat_asks_you("Decision packet for you: ship A or park B.")
        )

    def test_chat_status_report_with_gate_idiom_is_not_an_ask(self):
        report = (
            "Tonight it worked at machine speed. The only open item is that's an "
            "allowlist change — Platform's, on your word."
        )
        self.assertFalse(needs.chat_asks_you(report))


def make_home(
    tmp: Path, last_role="assistant", last_text="Want me to merge it?", age=60
):
    prof = tmp / "profiles" / "income"
    prof.mkdir(parents=True)
    db = sqlite3.connect(prof / "state.db")
    db.executescript("""
      CREATE TABLE sessions(id TEXT, title TEXT, archived INT);
      CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                            timestamp REAL, display_kind TEXT, active INT);
    """)
    now = time.time()
    db.execute("INSERT INTO sessions VALUES('s1','Bot Chat',0)")
    db.execute("INSERT INTO sessions VALUES('s2','kanban t_1',0)")
    db.execute(
        "INSERT INTO messages VALUES(1,'s1','user','hi',?,NULL,1)", (now - age - 10,)
    )
    db.execute(
        "INSERT INTO messages VALUES(2,'s1',?,?,?,NULL,1)",
        (last_role, last_text, now - age),
    )
    db.execute(
        "INSERT INTO messages VALUES(3,'s2','assistant','Should I?',?,NULL,1)", (now,)
    )
    db.commit()
    db.close()
    return tmp


class Chats(unittest.TestCase):
    def setUp(self):
        self._kanban_items = needs.kanban_items

    def tearDown(self):
        needs.kanban_items = self._kanban_items

    def test_bot_chat_question_is_queued(self):
        with tempfile.TemporaryDirectory() as d:
            items = needs.chat_items(make_home(Path(d)))
            self.assertEqual([i["session_id"] for i in items], ["s1"])
            self.assertEqual(items[0]["version"], "2")

    def test_user_replied_last(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(needs.chat_items(make_home(Path(d), last_role="user")), [])

    def test_old_ask_ages_out(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(needs.chat_items(make_home(Path(d), age=30 * 86400)), [])

    def test_hide_until_changed(self):
        with tempfile.TemporaryDirectory() as d:
            home = make_home(Path(d))
            needs.kanban_items = lambda boards=None, errors=None: []
            q = needs.build_queue(home)
            self.assertEqual(q["count"], 1)
            needs.hide(q["items"][0]["id"], q["items"][0]["version"], home)
            self.assertEqual(needs.build_queue(home)["count"], 0)
            needs.hide(
                q["items"][0]["id"], "older", home
            )  # source changed → shows again
            self.assertEqual(needs.build_queue(home)["count"], 1)

    def test_queue_count_and_pocket_pack_include_only_unanswered_asks(self):
        ask = {
            "id": "kanban:f:ask",
            "version": "1",
            "source": "kanban",
            "tier": "ask",
            "task_id": "ask",
            "board": "f",
            "title": "Ask",
            "ask": "Choose A",
            "why": "blocked",
        }
        stuck = {
            "id": "kanban:f:stuck",
            "version": "2",
            "source": "kanban",
            "tier": "stuck",
            "task_id": "stuck",
            "board": "f",
            "title": "Stuck",
            "ask": "Order only",
            "why": "stuck",
        }
        answered = {
            "id": "kanban:f:answered",
            "version": "3",
            "source": "kanban",
            "tier": "answered",
            "task_id": "answered",
            "board": "f",
            "title": "Answered",
            "ask": "Already answered",
            "why": "held",
        }
        original = (
            needs.kanban_items,
            needs.chat_items,
            needs.wait_items,
            needs.pairing_items,
        )
        needs.kanban_items = lambda errors=None: [ask, stuck, answered]
        needs.chat_items = needs.wait_items = needs.pairing_items = (
            lambda home, errors=None: []
        )
        try:
            with tempfile.TemporaryDirectory() as d:
                queue = needs.build_queue(Path(d))
                self.assertEqual(queue["count"], 1)
                self.assertEqual(queue["counts"]["asks"], 1)
                self.assertEqual(
                    [card["id"] for card in needs.pocket_pack(queue)["cards"]],
                    [ask["id"]],
                )
        finally:
            (
                needs.kanban_items,
                needs.chat_items,
                needs.wait_items,
                needs.pairing_items,
            ) = original

    def test_pocket_pack_shape(self):
        with tempfile.TemporaryDirectory() as d:
            home = make_home(Path(d))
            needs.kanban_items = lambda boards=None, errors=None: []
            pack = needs.pocket_pack(needs.build_queue(home))
            self.assertEqual(pack["mode"], "unblock")
            card = pack["cards"][0]
            for k in (
                "id",
                "kind",
                "title",
                "short",
                "choices",
                "options",
                "prompt",
                "why_now",
                "ttl_s",
            ):
                self.assertIn(k, card)
            p = needs.write_pocket_pack(needs.build_queue(home), home)
            self.assertEqual(p.parent.name, "unbl0ck")  # never the gadget plugin's pack


def block_event(
    reason="needs Ada to decide", kind="needs_input", recurrences=1, created_at=100
):
    return types.SimpleNamespace(
        id=11,
        kind="blocked",
        created_at=created_at,
        payload={"reason": reason, "kind": kind, "recurrences": recurrences},
    )


def task(status="blocked", task_id="t_1", **overrides):
    values = {
        "id": task_id,
        "title": "Fix it",
        "status": status,
        "priority": 3,
        "created_at": 100,
        "assignee": None,
        "block_kind": "needs_input",
        "block_recurrences": 1,
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


class FakeKanban(unittest.TestCase):
    """answer_card queue behavior is tested separately from its real-Hermes release helper."""

    def setUp(self):
        self.calls = []
        self.task = task()
        self.events = [block_event()]
        self.comments = []
        self.release_behavior = lambda conn, db, task_id, version: True
        kdb = fake_db(
            get_task=lambda c, t: self.task if t == "t_1" else None,
            list_events=lambda c, t: self.events,
            list_comments=lambda c, t: self.comments,
            add_comment=lambda c, t, author, body: (
                self.calls.append(("comment", t, author, body)) or 7
            ),
            unblock_task=lambda c, t: self.calls.append(("unblock", t)) or True,
        )
        self.stack = fake_hermes(kdb, fake_connect())
        self.stack.__enter__()

        def release(conn, db, task_id, version):
            self.calls.append(("unblock", task_id))
            return self.release_behavior(conn, db, task_id, version)

        self.release_patcher = mock.patch.object(
            needs, "_unblock_task_at_version", release
        )
        self.release_patcher.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.saved_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(
            self.home
        )  # the pending-release marker lands here

    def tearDown(self):
        self.release_patcher.stop()
        self.stack.__exit__(None, None, None)
        if self.saved_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self.saved_home
        self.tmp.cleanup()

    def test_answer_and_release(self):
        r = needs.answer_card("factory", "t_1", "Go with A", release=True, version="11")
        self.assertEqual(r, {"ok": True, "comment_id": 7, "released": True})
        self.assertEqual(
            self.calls, [("comment", "t_1", "ada", "Go with A"), ("unblock", "t_1")]
        )

    def test_answer_only(self):
        needs.answer_card("factory", "t_1", "noted", release=False)
        self.assertEqual([c[0] for c in self.calls], ["comment"])

    def test_empty_and_missing(self):
        with self.assertRaises(ValueError):
            needs.answer_card("factory", "t_1", "  ", release=True, version="11")
        with self.assertRaises(LookupError):
            needs.answer_card("factory", "t_9", "x", release=True, version="11")

    def test_double_submit_is_rejected_before_a_second_comment(self):
        self.comments = [
            types.SimpleNamespace(author="ada", created_at=150, body="already")
        ]
        with self.assertRaises(RuntimeError):
            needs.answer_card("factory", "t_1", "again", release=False)
        self.assertEqual(self.calls, [])

    def test_failed_unblock_is_not_reported_as_success(self):
        self.release_behavior = lambda *args: False
        with self.assertRaises(needs.UnblockFailed) as ctx:
            needs.answer_card("factory", "t_1", "Go", release=True, version="11")
        self.assertIn("comment 7", str(ctx.exception))
        self.assertEqual([c[0] for c in self.calls], ["comment", "unblock"])

    def test_answer_to_an_old_block_is_refused(self):
        # The card was blocked again (event 12) after the human read the block-11 question.
        self.events = [
            block_event(),
            types.SimpleNamespace(
                id=12,
                kind="blocked",
                created_at=130,
                payload={"reason": "new question", "kind": "needs_input"},
            ),
        ]
        with self.assertRaises(needs.StaleAnswer):
            needs.answer_card("factory", "t_1", "Go with A", release=True, version="11")
        self.assertEqual(self.calls, [])
        r = needs.answer_card("factory", "t_1", "Go with B", release=True, version="12")
        self.assertTrue(r["released"])

    def test_stale_release_error_keeps_the_posted_answer(self):
        def stale_after_comment(conn, db, task_id, version):
            self.events.append(
                types.SimpleNamespace(
                    id=12,
                    kind="blocked",
                    created_at=130,
                    payload={"reason": "new question", "kind": "needs_input"},
                )
            )
            raise needs.StaleAnswer("newer block")

        self.release_behavior = stale_after_comment
        with self.assertRaises(needs.StaleAnswer):
            needs.answer_card("factory", "t_1", "Go with A", release=True, version="11")
        self.assertEqual(
            self.calls, [("comment", "t_1", "ada", "Go with A"), ("unblock", "t_1")]
        )
        self.assertEqual([event.id for event in self.events], [11, 12])

    def test_failed_release_stays_in_queue_and_retries_without_a_second_comment(self):
        kdb = sys.modules["hermes_cli.kanban_db"]
        self.release_behavior = lambda *args: False
        with self.assertRaises(needs.UnblockFailed):
            needs.answer_card("factory", "t_1", "Go", release=True, version="11")
        self.assertEqual(needs.load_pending_releases(), {"kanban:factory:t_1": "11"})
        # The comment is now on the card.
        self.comments = [types.SimpleNamespace(author="ada", created_at=150, body="Go")]
        kdb.list_tasks = lambda c, status=None: (
            [self.task] if status == "blocked" else []
        )
        (it,) = needs.kanban_items(boards=["factory"])
        self.assertEqual(it["tier"], "release_failed")
        # Answer-only on an answered card is still refused; a release retry is allowed once.
        with self.assertRaises(RuntimeError):
            needs.answer_card("factory", "t_1", "Go", release=False)
        self.release_behavior = lambda *args: True
        r = needs.answer_card("factory", "t_1", "Go", release=True, version="11")
        self.assertEqual(r, {"ok": True, "comment_id": None, "released": True})
        self.assertEqual([c[0] for c in self.calls], ["comment", "unblock", "unblock"])
        self.assertEqual(needs.load_pending_releases(), {})
        kdb.list_tasks = (
            lambda c, status=None: []
        )  # the release moved it out of blocked
        self.assertEqual(needs.kanban_items(boards=["factory"]), [])

    def test_answered_and_kept_blocked_stays_listed_and_can_be_released(self):
        """Roast U2: "Answer, keep blocked" used to drop the card with no way back (409)."""
        kdb = sys.modules["hermes_cli.kanban_db"]
        kdb.list_tasks = lambda c, status=None: (
            [self.task] if status == "blocked" else []
        )
        needs.answer_card(
            "factory", "t_1", "Hold until Friday", release=False, version="11"
        )
        self.comments = [
            types.SimpleNamespace(
                author="ada", created_at=150, body="Hold until Friday"
            )
        ]
        (it,) = needs.kanban_items(boards=["factory"])
        self.assertEqual(it["tier"], "answered")
        # A second answer is still refused; a release with no text goes ahead, with no new comment.
        with self.assertRaises(RuntimeError):
            needs.answer_card("factory", "t_1", "again", release=False)
        r = needs.answer_card("factory", "t_1", "", release=True, version="11")
        self.assertEqual(r, {"ok": True, "comment_id": None, "released": True})
        self.assertEqual([c[0] for c in self.calls], ["comment", "unblock"])

    def test_release_with_updated_answer_posts_it_on_an_already_answered_card(self):
        self.comments = [
            types.SimpleNamespace(author="ada", created_at=150, body="old answer")
        ]
        r = needs.answer_card(
            "factory", "t_1", "Updated answer", release=True, version="11"
        )
        self.assertEqual(r["comment_id"], 7)
        self.assertEqual(
            self.calls,
            [("comment", "t_1", "ada", "Updated answer"), ("unblock", "t_1")],
        )

    def test_release_requires_the_version_the_user_saw(self):
        with self.assertRaisesRegex(ValueError, "version"):
            needs.answer_card("factory", "t_1", "Go with A", release=True)
        self.assertEqual(self.calls, [])

    def test_external_release_clears_pending_release_marker(self):
        needs._set_pending_release("kanban:factory:t_1", "11", self.home)
        self.comments = [types.SimpleNamespace(author="ada", created_at=150, body="Go")]
        kdb = sys.modules["hermes_cli.kanban_db"]
        self.task.status = "blocked"

        def external_release(c, t):
            self.task.status = "open"
            return False

        kdb.unblock_task = external_release
        self.release_behavior = lambda *args: external_release(args[0], args[2])
        result = needs.answer_card(
            "factory", "t_1", "", release=True, version="11", home=self.home
        )
        self.assertTrue(result["released"])
        self.assertEqual(needs.load_pending_releases(self.home), {})

    def test_empty_text_still_needs_an_answer_on_an_unanswered_card(self):
        with self.assertRaises(ValueError):
            needs.answer_card("factory", "t_1", "", release=True, version="11")
        with self.assertRaises(ValueError):
            needs.answer_card("factory", "t_1", "  ", release=False)
        self.assertEqual(self.calls, [])

    def test_an_answered_triage_card_leaves_the_queue(self):
        self.task = task(status="triage")
        kdb = sys.modules["hermes_cli.kanban_db"]
        kdb.list_tasks = lambda c, status=None: (
            [self.task] if status == "triage" else []
        )
        self.comments = [
            types.SimpleNamespace(author="ada", created_at=150, body="noted")
        ]
        self.assertEqual(needs.kanban_items(boards=["factory"]), [])

    def test_triage_cannot_be_released(self):
        self.task = task(status="triage")
        with self.assertRaises(ValueError):
            needs.answer_card("factory", "t_1", "answer", release=True, version="11")
        self.assertEqual(self.calls, [])
        needs.answer_card("factory", "t_1", "answer", release=False)
        self.assertEqual([c[0] for c in self.calls], ["comment"])


class QueueKanban(unittest.TestCase):
    def test_per_board_failure_is_surfaced(self):
        good_task = task(task_id="t_ok", priority=5, block_recurrences=2)
        kdb = fake_db(
            list_tasks=lambda conn, status=None: (
                [good_task] if status == "blocked" else []
            ),
            list_events=lambda conn, task_id: [block_event(recurrences=2)],
        )

        def connect(board=None):
            if board == "bad":
                raise sqlite3.OperationalError("no such table: tasks")
            return sqlite3.connect(":memory:")

        errors = []
        with fake_hermes(kdb, fake_connect(connect=connect)):
            items = needs.kanban_items(["bad", "good"], errors=errors)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["board"], "good")
        self.assertEqual(items[0]["priority"], 5)
        self.assertEqual(items[0]["block_kind"], "needs_input")
        self.assertEqual(items[0]["block_recurrences"], 2)
        self.assertEqual(len(errors), 1)
        self.assertIn("kanban[bad]", errors[0])

    def test_block_loop_triage_card_is_queued(self):
        triage = task(status="triage", task_id="t_triage", block_recurrences=2)
        triage_event = types.SimpleNamespace(
            id=12,
            kind="block_loop_detected",
            created_at=200,
            payload={
                "reason": "needs Ada to choose a path",
                "kind": "needs_input",
                "recurrences": 2,
                "limit": 2,
            },
        )
        kdb = fake_db(
            list_tasks=lambda conn, status=None: [triage] if status == "triage" else [],
            list_events=lambda conn, task_id: [triage_event],
        )
        with fake_hermes(kdb, fake_connect()):
            items = needs.kanban_items(["factory"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "triage")
        self.assertEqual(items[0]["id"], "kanban:factory:t_triage")
        self.assertEqual(items[0]["block_recurrences"], 2)
        self.assertTrue(needs.kanban_needs_you(items[0]["ask"], "needs_input"))

    def test_plain_triage_card_without_ask_is_queued_as_stuck(self):
        triage = task(status="triage", task_id="t_plain")
        event = types.SimpleNamespace(
            id=13,
            kind="block_loop_detected",
            created_at=200,
            payload={
                "reason": "repeated transient CI failure",
                "kind": "transient",
                "recurrences": 2,
            },
        )
        kdb = fake_db(
            list_tasks=lambda conn, status=None: [triage] if status == "triage" else [],
            list_events=lambda conn, task_id: [event],
        )
        with fake_hermes(kdb, fake_connect()):
            items = needs.kanban_items(["factory"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["tier"], "stuck")

    def test_stuck_reasons_are_plain(self):
        ev = types.SimpleNamespace(id=1, kind="blocked", created_at=1, payload={})
        self.assertIn("no block record", needs.stuck_reason("", None, None))
        self.assertIn(
            "Held on creation", needs.stuck_reason("initial_status", None, ev)
        )
        self.assertIn("capability", needs.stuck_reason("x", "capability", ev))

    def test_operator_and_credential_phrasing_detected(self):
        for reason in [
            "Operator input is needed to reconcile the judge",
            "Ada approved this last week",
            "npm credentials are dead",
            "needs an authorized host route",
        ]:
            self.assertTrue(needs.kanban_needs_you(reason, None), reason)


class CardContext(unittest.TestCase):
    def test_context_shape(self):
        ctx_task = task(
            task_id="t_ctx",
            priority=4,
            created_by="ada",
            session_id="sess-1",
            body="Do the thing",
            assignee="worker",
            workspace_kind="git",
            workspace_path="/tmp/wt",
            branch_name="feat/x",
            project_id="proj",
            completion_contract="owner/repo#7",
            last_failure_error="boom",
            consecutive_failures=2,
            result=None,
            started_at=110,
            completed_at=None,
        )
        block = block_event(created_at=120, recurrences=2)
        pr_event = types.SimpleNamespace(
            id=14,
            kind="pr_acceptance",
            created_at=130,
            payload={"ok": True, "classification": "green", "detail": ""},
            run_id=3,
        )
        comment = types.SimpleNamespace(
            id=4, author="worker", body="Which option?", created_at=121
        )
        run = types.SimpleNamespace(
            id=3,
            profile="worker",
            status="blocked",
            outcome="blocked",
            summary="waiting on you",
            error=None,
            started_at=110,
            ended_at=120,
        )
        attachment = types.SimpleNamespace(
            id=1,
            filename="evidence.txt",
            content_type="text/plain",
            size=12,
            uploaded_by="worker",
            created_at=115,
        )
        kdb = fake_db(
            get_task=lambda conn, task_id: ctx_task if task_id == "t_ctx" else None,
            list_events=lambda conn, task_id: [block, pr_event],
            list_comments=lambda conn, task_id: [comment],
            task_graph_context=lambda conn, task_id: {
                "parents": [{"id": "t_0", "title": "Parent", "status": "done"}],
                "children": [{"id": "t_2", "title": "Child", "status": "todo"}],
            },
            list_attachments=lambda conn, task_id: [attachment],
            latest_run=lambda conn, task_id: run,
        )
        with fake_hermes(kdb, fake_connect()):
            data = needs.card_context("factory", "t_ctx")
        self.assertEqual(data["id"], "kanban:factory:t_ctx")
        self.assertEqual(data["body"], "Do the thing")
        self.assertEqual(data["created_by"], "ada")
        self.assertEqual(data["workspace"]["branch"], "feat/x")
        self.assertEqual(data["pr"]["reference"], "owner/repo#7")
        self.assertTrue(data["pr"]["ok"])
        self.assertEqual(data["block"]["reason"], "needs Ada to decide")
        self.assertEqual(data["block"]["recurrences"], 2)
        self.assertEqual(data["comments"][0]["body"], "Which option?")
        self.assertEqual(data["graph"]["parents"][0]["id"], "t_0")
        self.assertEqual(data["last_run"]["outcome"], "blocked")
        self.assertEqual(data["attachments"][0]["filename"], "evidence.txt")
        self.assertNotIn("stored_path", data["attachments"][0])

    def test_missing_card_raises(self):
        with fake_hermes(fake_db(), fake_connect()), self.assertRaises(LookupError):
            needs.card_context("factory", "nope")


class Ask(unittest.TestCase):
    def test_messages_are_marked_untrusted_and_keep_turns(self):
        card = {
            "id": "kanban:factory:t_1",
            "title": "T",
            "body": "Body",
            "comments": [],
        }
        history = [
            {"role": "user", "content": "What should I do?"},
            {"role": "tool", "content": "ignored"},
            {"role": "assistant", "content": "Ship A."},
        ]
        messages = needs.build_ask_messages(card, history)
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("untrusted", messages[0]["content"].lower())
        self.assertIn("never claim to have acted", messages[0]["content"].lower())
        self.assertIn("CARD DATA", messages[1]["content"])
        self.assertIn("Body", messages[1]["content"])
        self.assertEqual(messages[-1], {"role": "assistant", "content": "Ship A."})
        self.assertEqual(
            [m["role"] for m in messages], ["system", "user", "user", "assistant"]
        )

    def test_ask_enabled_setting(self):
        self.assertTrue(needs.ask_enabled({}))
        self.assertTrue(needs.ask_enabled({"ask_enabled": True}))
        self.assertTrue(needs.ask_enabled({"ask_enabled": "true"}))
        self.assertFalse(needs.ask_enabled({"ask_enabled": False}))
        self.assertFalse(needs.ask_enabled({"ask_enabled": "off"}))
        self.assertFalse(needs.ask_enabled({"ask_enabled": "0"}))


class PrincipalName(unittest.TestCase):
    def test_other_name_detected_and_default_name_not_hardcoded(self):
        orig = needs._settings
        needs._settings = lambda: {"principal_name": "Grace"}
        try:
            self.assertTrue(
                needs.kanban_needs_you("Awaiting Grace's approval to publish", None)
            )
            self.assertIsNone(
                needs.kanban_needs_you("Awaiting Ada's approval to publish", None)
            )
            self.assertTrue(needs.kanban_needs_you("CEO-gated: repo publication", None))
            self.assertEqual(needs.principal_author(), "Grace")
        finally:
            needs._settings = orig

    def test_no_name_configured_uses_generic_words(self):
        self.assertEqual(needs.principal_names({}), [])
        self.assertEqual(needs.principal_author({}), "user")
        self.assertTrue(needs._patterns([])["_GATE"].search("needs human approval"))

    def test_author_env_var_is_spelled_correctly(self):
        orig = needs._settings
        saved = {k: os.environ.get(k) for k in ("UNBL0CK_AUTHOR", "UNBL0CK_AUTHOr")}
        needs._settings = dict
        try:
            for key in saved:
                os.environ.pop(key, None)
            os.environ["UNBL0CK_AUTHOR"] = "from-env"
            self.assertEqual(needs.principal_author(), "from-env")
            os.environ.pop("UNBL0CK_AUTHOR", None)
            os.environ["UNBL0CK_AUTHOr"] = "typo"
            self.assertEqual(needs.principal_author(), "user")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            needs._settings = orig


if __name__ == "__main__":
    unittest.main()
