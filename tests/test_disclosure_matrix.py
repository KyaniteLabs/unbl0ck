"""Source-by-surface disclosure matrix, synthetic-secret leaks and cache partitions (issue #42)."""

import importlib.util
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import fastapi
    from fastapi.testclient import TestClient
except Exception:  # noqa: BLE001
    fastapi = None

sys.path.insert(0, str(Path(__file__).parent))
from test_needs import make_home, needs  # noqa: E402
from test_outside import OutsideFixture  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# Synthetic values only; none is a real credential.
SECRETS = [
    "sk-ant-api03-SYNTHETICKEYSYNTHETICKEY0001",
    "ghp_SYNTHETICTOKENSYNTHETICTOKEN000001",
    "hunter2-SYNTHETIC-PASSWORD",
]
LEAKY = (
    f"deploy with api_key={SECRETS[0]} and Authorization: Bearer {SECRETS[1]} then "
    f'{{"password": "{SECRETS[2]}"}} should I go ahead?'
)


def yes(flag):
    return "yes" if flag else "no"


class MatrixIsTheCode(unittest.TestCase):
    def test_every_source_has_one_row_in_queue_order(self):
        rows = needs.disclosure_matrix({})
        self.assertEqual([r["source"] for r in rows], list(needs.SOURCES))
        self.assertEqual(set(needs.SURFACES), set(needs.SOURCES))

    def test_defaults_match_what_the_queue_and_ask_actually_do(self):
        by = {r["source"]: r for r in needs.disclosure_matrix({})}
        self.assertEqual({s for s, r in by.items() if r["summary"]}, {"kanban", "outside", "chat"})
        self.assertEqual({s for s, r in by.items() if r["ask"]}, {"kanban"})
        for s in needs.NO_BLUF_SOURCES:
            self.assertFalse(by[s]["summary"], s)
        self.assertFalse(by["pairing"]["search"])

    def test_settings_turn_each_transmission_off_independently(self):
        off_summary = {r["source"]: r for r in needs.disclosure_matrix({"bluf_enabled": False})}
        self.assertFalse(any(r["summary"] for r in off_summary.values()))
        self.assertTrue(off_summary["kanban"]["ask"])
        off_ask = {r["source"]: r for r in needs.disclosure_matrix({"ask_enabled": "off"})}
        self.assertFalse(any(r["ask"] for r in off_ask.values()))
        self.assertTrue(off_ask["chat"]["summary"])

    def test_matrix_holds_booleans_only(self):
        for row in needs.disclosure_matrix({}):
            self.assertEqual(set(row), {"source", "summary", "ask", "search", "cross_profile"})
            self.assertTrue(all(isinstance(v, bool) for k, v in row.items() if k != "source"))

    def test_queue_payload_carries_the_matrix(self):
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "kanban_items", lambda boards=None, errors=None: []):
            q = needs.build_queue(Path(d))
            self.assertEqual(q["disclosure"], needs.disclosure_matrix())

    def test_settings_read_fault_turns_every_model_path_off_in_the_matrix(self):
        spec = importlib.util.spec_from_file_location("ub_disclosure_fault", ROOT / "needs.py")
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        with patch("hermes_cli.config.load_config_readonly", side_effect=ValueError("broken")):
            fresh._SETTINGS_CACHE[0] = 0.0
            rows = fresh.disclosure_matrix()
        self.assertFalse(any(r["summary"] or r["ask"] for r in rows))

    def test_docs_table_equals_the_code(self):
        text = (ROOT / "docs/architecture.md").read_text()
        head = "## Disclosure matrix"
        self.assertIn(head, text)
        table = {}
        for line in text.split(head, 1)[1].split("\n## ", 1)[0].splitlines():
            cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
            if len(cells) == 5 and cells[0] in needs.SOURCES:
                table[cells[0]] = cells[1:]
        self.assertEqual(set(table), set(needs.SOURCES))
        for row in needs.disclosure_matrix({}):
            self.assertEqual(
                table[row["source"]],
                [yes(row["summary"]), yes(row["ask"]), yes(row["search"]), yes(row["cross_profile"])],
                row["source"],
            )


class CrossProfileIsWhatIsCollected(OutsideFixture):
    """The matrix column is checked against what the readers return, not against a constant."""

    def file(self, profile, title):
        args = {"kind": "draft", "title": title, "body": "B", "act_where": "Mail"}
        return json.loads(self.tools["unbl0ck_file"][2](args, session_id="s", profile=profile))

    def test_filed_items_from_every_profile_are_collected_and_the_matrix_says_so(self):
        self.file("work", "From work")
        self.file("personal", "From personal")
        titles = {i["title"] for i in needs.outside_items(self.home)}
        self.assertEqual(titles, {"From work", "From personal"})
        row = {r["source"]: r for r in needs.disclosure_matrix({})}["outside"]
        self.assertTrue(row["cross_profile"])

    def test_chats_of_every_profile_are_collected_and_the_matrix_says_so(self):
        import sqlite3

        for profile in ("work", "personal"):
            prof = self.home / "profiles" / profile
            prof.mkdir(parents=True)
            db = sqlite3.connect(prof / "state.db")
            db.executescript("""
              CREATE TABLE sessions(id TEXT, title TEXT, archived INT, source TEXT, chat_type TEXT,
                                    user_id TEXT, display_name TEXT, started_at REAL, last_activity_at REAL);
              CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
                                    timestamp REAL, display_kind TEXT, active INT);
            """)
            now = __import__("time").time()
            db.execute("INSERT INTO sessions VALUES(?,?,0,'cli','dm','u','Pat',?,?)", (f"s-{profile}", "t", now, now))
            db.execute("INSERT INTO messages VALUES(1,?,?,?,?,NULL,1)", (f"s-{profile}", "assistant", "Should I open the PR now?", now))
            db.commit()
            db.close()
        with patch.object(needs, "_settings", lambda: {"principal_name": "Ada"}):
            got = {i["session_id"] for i in needs.chat_items(self.home)}
        self.assertEqual(got, {"s-work", "s-personal"})
        self.assertTrue({r["source"]: r for r in needs.disclosure_matrix({})}["chat"]["cross_profile"])


@unittest.skipIf(fastapi is None, "fastapi not installed")
class WhatLeavesTheMachine(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("ub_api_disclosure", ROOT / "dashboard/plugin_api.py")
        self.api = importlib.util.module_from_spec(spec)
        sys.modules["ub_api_disclosure"] = self.api
        spec.loader.exec_module(self.api)
        self.sent = []
        outer = self

        class LLM:
            def complete(inner, messages, **kw):
                outer.sent.append(json.dumps(messages))
                return types.SimpleNamespace(text='{"bluf": "Decide the deploy.", "why": "Blocked."}', provider="p", model="m")

        self.api._loaded_ctx = lambda: types.SimpleNamespace(llm=LLM())
        self.api._needs = lambda: needs
        app = fastapi.FastAPI()
        app.include_router(self.api.router)
        self.c = TestClient(app)

    def run_queue(self, bluf_on):
        items = [
            {"id": f"{s}:x", "version": "1", "source": s, "tier": "ask", "title": "T", "ask": LEAKY, "owner": "o", "profile": "p"}
            for s in needs.SOURCES
        ]
        q = {"items": items, "bluf_enabled": bluf_on, "count": len(items), "errors": []}

        def inline(slots, worker, *args):
            try:
                worker(*args)
            finally:
                slots.release()

        self.api._BLUF_POOL = types.SimpleNamespace(submit=lambda fn, *a: fn(*a))
        with patch.object(needs, "build_queue", lambda: q), \
                patch.object(needs, "save_bluf", lambda *a, **k: None), \
                patch.object(needs, "recent_comments", lambda item, limit=6: []):
            self.c.get("/queue")
        return q

    def test_summary_requests_match_the_matrix_and_carry_no_secret(self):
        self.run_queue(True)
        blob = " ".join(self.sent)
        self.assertEqual(len(self.sent), 3, "kanban, outside and chat only")
        for secret in SECRETS:
            self.assertNotIn(secret, blob)
        self.assertIn("[redacted]", blob)

    def test_summaries_off_sends_nothing(self):
        self.run_queue(False)
        self.assertEqual(self.sent, [])

    def test_ask_is_refused_for_every_non_kanban_reference(self):
        body = {"messages": [{"role": "user", "content": "q"}]}
        for ref in ("chat:default:s1", "prompt:r1", "pairing:default:telegram:k", "outside:folder:abc"):
            resp = self.c.post("/card/ask", json={**body, "id": ref})
            self.assertGreaterEqual(resp.status_code, 400, ref)
        self.assertEqual(self.sent, [])

    def test_ask_off_is_403_before_any_card_read(self):
        with patch.object(needs, "ask_enabled", lambda s=None: False), patch.object(needs, "card_context", side_effect=AssertionError("read")):
            resp = self.c.post("/card/ask", json={"board": "b", "task_id": "t", "messages": [{"role": "user", "content": "q"}]})
        self.assertEqual(resp.status_code, 403)


class SyntheticSecretsNeverEnterModelInput(unittest.TestCase):
    SHAPES = {
        "title": lambda s: {"title": s, "ask": "ok?"},
        "ask": lambda s: {"title": "t", "ask": s},
        "owner": lambda s: {"title": "t", "ask": "ok?", "owner": s},
        "long body": lambda s: {"title": "t", "ask": "x " * 2000 + s},
    }

    def test_summary_messages_redact_every_card_field(self):
        for name, make in self.SHAPES.items():
            with self.subTest(name):
                blob = json.dumps(needs.bluf_messages(make(LEAKY), [{"author": "a", "body": LEAKY}]))
                for secret in SECRETS:
                    self.assertNotIn(secret, blob)

    def test_comments_cannot_carry_secrets_into_a_summary(self):
        comments = [{"author": "a", "body": f"token={SECRETS[0]} and password={SECRETS[2]}"}] * 8
        blob = json.dumps(needs.bluf_messages({"title": "t", "ask": "ok?"}, comments))
        for secret in (SECRETS[0], SECRETS[2]):
            self.assertNotIn(secret, blob)

    def test_pairing_and_prompt_have_no_model_path(self):
        rows = {r["source"]: r for r in needs.disclosure_matrix({})}
        for source in ("pairing", "prompt"):
            self.assertIn(source, needs.NO_BLUF_SOURCES)
            self.assertFalse(rows[source]["summary"] or rows[source]["ask"], source)

    def test_ask_messages_redact_card_comments_and_history(self):
        card = {"title": "t", "description": LEAKY, "comments": [{"author": "a", "body": LEAKY}], "workspace": {"path": "/home/x/secret"}}
        history = [{"role": "user", "content": LEAKY}]
        messages = needs.build_ask_messages(card, history)
        card_blob = json.dumps(messages[:-1])
        for secret in SECRETS:
            self.assertNotIn(secret, card_blob)
        self.assertNotIn("/home/x/secret", card_blob)
        # The operator's own typed question is sent as typed (documented in the matrix notes).
        self.assertEqual(messages[-1], history[0])


class CachePartitions(unittest.TestCase):
    def item(self, item_id, version="1"):
        return {"id": item_id, "version": version}

    def test_summaries_do_not_cross_profiles_or_sources(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            needs.save_bluf("chat:work:s1", "1", "Work summary.", "w", home)
            items = [self.item("chat:personal:s1"), self.item("chat:work:s1"), self.item("kanban:work:s1")]
            got = {i["id"]: i.get("bluf") for i in needs.attach_blufs(items, home)}
            self.assertEqual(got, {"chat:personal:s1": None, "chat:work:s1": "Work summary.", "kanban:work:s1": None})

    def test_changed_version_never_shows_a_stale_summary(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            needs.save_bluf("kanban:b:t", "1", "Old.", "w", home)
            self.assertIsNone(needs.attach_blufs([self.item("kanban:b:t", "2")], home)[0].get("bluf"))

    def test_corrupt_cache_shows_no_summary_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            needs.data_dir(home).mkdir(parents=True, exist_ok=True)
            (needs.data_dir(home) / "bluf.json").write_text("{not json")
            self.assertIsNone(needs.attach_blufs([self.item("kanban:b:t")], home)[0].get("bluf"))

    def test_concurrent_saves_from_separate_sources_all_survive(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            ids = [f"{src}:p:{n}" for src in ("kanban", "chat", "outside") for n in range(12)]
            threads = [threading.Thread(target=needs.save_bluf, args=(i, "1", f"s {i}", "w", home)) for i in ids]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(set(needs.load_blufs(home)), set(ids))

    def test_pruning_after_a_failed_source_keeps_that_sources_summaries_only(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            needs.save_bluf("chat:work:s1", "1", "kept.", "w", home)
            needs.save_bluf("kanban:b:gone", "1", "dropped.", "w", home)
            needs.prune_blufs([], home, preserve_prefixes=("chat:",))
            self.assertEqual(set(needs.load_blufs(home)), {"chat:work:s1"})


if __name__ == "__main__":
    unittest.main()
