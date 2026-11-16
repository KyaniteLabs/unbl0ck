"""Round-4 findings on the disclosure-matrix branch (#42): which profiles each source is read
across, one settings snapshot per response, behavior-backed search and provider-call checks, and
a persistent-store inventory. Every test that creates Kanban cards isolates HERMES_KANBAN_HOME
(or derives the shared Kanban root from a temporary HERMES_HOME), never a real installation."""

import importlib.util
import json
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import fastapi
    from fastapi.testclient import TestClient
except Exception:  # noqa: BLE001
    fastapi = None

from r4c_common import ASK, HomeCase, chat_db, needs

ROOT = Path(__file__).resolve().parents[1]
ARCH = (ROOT / "docs/architecture.md").read_text()
README = (ROOT / "README.md").read_text()
NEEDLE = "zebraclause"


def matrix_row(source, settings=None):
    return {r["source"]: r for r in needs.disclosure_matrix(settings or {})}[source]


def matrix_summary(queue):
    return {r["source"]: r for r in queue["disclosure"]}["chat"]["summary"]


class KanbanIsSharedAcrossProfiles(unittest.TestCase):
    """12-002 (class I): the matrix said Kanban was per profile; Hermes keeps one board store."""

    def setUp(self):
        try:
            from hermes_cli import kanban_db, kanban_db_connect  # noqa: F401
        except ImportError as exc:  # pragma: no cover - the suite runs against Hermes
            self.skipTest(str(exc))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # No HERMES_KANBAN_HOME override: the root is derived from the temporary HERMES_HOME of
        # each profile, which is exactly what two real profiles do.
        env = {"HERMES_KANBAN_HOME": "", "UNBL0CK_OUTSIDE_DIRS": ""}
        p = patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(needs, "_settings", lambda: {"principal_name": "CEO"})
        p.start()
        self.addCleanup(p.stop)

    def as_profile(self, name):
        home = self.root / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        return patch.dict(os.environ, {"HERMES_HOME": str(home)})

    def test_a_card_blocked_from_one_profile_is_read_by_another_and_the_matrix_says_so(self):
        from hermes_cli import kanban_db, kanban_db_connect

        with self.as_profile("a"):
            with kanban_db_connect.connect(board="default") as conn:
                task = kanban_db.create_task(conn, title="Pick a plan", body="", assignee="worker")
                task_id = getattr(task, "id", task)
                kanban_db.block_task(conn, task_id, reason="Needs CEO approval", kind="needs_input")
        with self.as_profile("b"):
            seen = [i["id"] for i in needs.kanban_items()]
        self.assertEqual(seen, [f"kanban:default:{task_id}"])
        self.assertTrue(matrix_row("kanban")["cross_profile"])

    def test_docs_table_says_kanban_is_read_across_profiles(self):
        row = [l for l in ARCH.splitlines() if l.startswith("| `kanban` |")]
        self.assertEqual(len(row), 1)
        self.assertTrue(row[0].rstrip("| ").endswith("yes"), row[0])
        self.assertIn("Hermes keeps one Kanban store shared by every profile", ARCH)


class WatchedRootsAreProfileConfigured(HomeCase):
    """12-001 (class I): filed items are shared; watched folders are what this profile configures."""

    def test_each_profile_reads_only_the_watched_folders_it_configures_but_every_filed_item(self):
        a, b = self.base / "watch-a", self.base / "watch-b"
        for d, name in ((a, "from-a"), (b, "from-b")):
            d.mkdir()
            (d / (name + ".txt")).write_text(f"{name}\nNotes for the owner\n")
        needs.file_outside({"kind": "draft", "title": "Filed by work", "body": "B", "act_where": "Mail"},
                           self.home, profile="work", session_id="s1")
        needs.file_outside({"kind": "draft", "title": "Filed by home", "body": "B", "act_where": "Mail"},
                           self.home, profile="home", session_id="s2")
        seen = {}
        for name, folder in (("a", a), ("b", b)):
            self.settings["outside_dirs"] = str(folder)
            seen[name] = {i["title"] for i in needs.outside_items(self.home)}
        self.assertEqual(seen["a"] - {"Filed by work", "Filed by home"}, {"from-a"})
        self.assertEqual(seen["b"] - {"Filed by work", "Filed by home"}, {"from-b"})
        self.assertTrue({"Filed by work", "Filed by home"} <= seen["a"] & seen["b"])

    def test_docs_do_not_call_configured_watched_roots_read_for_every_profile(self):
        notes = ARCH.split("## Disclosure matrix", 1)[1].split("\n## ", 1)[0]
        self.assertNotIn("both are read for every profile", notes)
        self.assertIn("reads only the `outside_dirs` its own settings configure", notes)
        self.assertIn("Filed items live in one shared folder", notes)


class OneSettingsSnapshotPerResponse(HomeCase):
    """12-003 (class A): a response must not mix two refreshes of the settings."""

    def test_flags_and_matrix_in_one_queue_response_agree_when_settings_change_between_reads(self):
        reads = {"n": 0}

        def alternating():
            reads["n"] += 1
            on = reads["n"] % 2 == 1  # every read sees the other state: the worst case
            return {"principal_name": "Ada", "bluf_enabled": on, "ask_enabled": on}

        with patch.object(needs, "_settings", alternating), \
                patch.object(needs, "kanban_items", lambda **kw: []), \
                patch.dict(os.environ, {"HERMES_KANBAN_HOME": str(self.base / "k")}):
            for _ in range(4):
                q = needs.build_queue(self.home)
                rows = {r["source"]: r for r in q["disclosure"]}
                self.assertEqual(q["bluf_enabled"], rows["chat"]["summary"], q["disclosure"])
                self.assertEqual(q["ask_enabled"], rows["kanban"]["ask"], q["disclosure"])

    def test_summaries_are_attached_only_when_the_response_says_they_are_enabled(self):
        """Settings expire and turn summaries off right after the cached summaries are attached."""
        needs.save_bluf("chat:default:s1", "v", "Cached summary.", "w", self.home)
        state = {"on": True}
        real_attach = needs.attach_blufs

        def attach_then_expire(items, home):
            out = real_attach(items, home)
            state["on"] = False
            return out

        item = {"id": "chat:default:s1", "version": "v", "source": "chat", "tier": "ask", "title": "t",
                "ask": ASK, "text": ASK, "since": 1, "priority": 0}
        with patch.object(needs, "_settings", lambda: {"principal_name": "Ada", "bluf_enabled": state["on"]}), \
                patch.object(needs, "attach_blufs", attach_then_expire), \
                patch.object(needs, "kanban_items", lambda **kw: []), \
                patch.object(needs, "chat_items", lambda *a, **k: [dict(item)]), \
                patch.dict(os.environ, {"HERMES_KANBAN_HOME": str(self.base / "k")}):
            q = needs.build_queue(self.home)
        attached = any(i.get("bluf") for i in q["items"])
        self.assertEqual(attached, q["bluf_enabled"], (attached, q["bluf_enabled"]))
        self.assertEqual(q["bluf_enabled"], matrix_summary(q))


class PromptsAndPairingAreReadAcrossProfiles(HomeCase):
    """Sweep (class L): the other two `cross_profile` flags are checked against the readers too."""

    def test_a_live_prompt_recorded_by_one_profile_is_read_by_another(self):
        spec = importlib.util.spec_from_file_location("ub_prompt_scope", ROOT / "needs.py")
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)  # its own home resolution, not the fixture's
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home / "profiles" / "a")}):
            fresh.record_wait(None, kind="clarify", request_id="0123456789abcdef", prompt="Which branch?")
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home / "profiles" / "b")}):
            seen = [i["id"] for i in fresh.wait_items()]
        self.assertEqual(seen, ["prompt:0123456789abcdef"])
        self.assertTrue(matrix_row("prompt")["cross_profile"])

    def test_pending_pairing_requests_of_every_profile_are_listed(self):
        for name in ("work", "home"):
            (self.home / "profiles" / name / "platforms" / "pairing").mkdir(parents=True)

        class Store:
            def __init__(self, profile):
                self.profile = profile

            def list_pending(self):
                return [{"platform": "telegram", "code": "X", "user_id": self.profile,
                         "user_name": f"Person {self.profile}", "age_minutes": 1}]

        profiles = {i["profile"] for i in needs.pairing_items(self.home, store=Store)}
        self.assertEqual(profiles, {"work", "home"})
        self.assertTrue(matrix_row("pairing")["cross_profile"])


class SearchFlagIsWhatSearchDoes(HomeCase):
    """12-004 (class L): each source's `search` flag is compared with the real search."""

    def setUp(self):
        super().setUp()
        kanban = self.base / "kanban"
        kanban.mkdir()
        p = patch.dict(os.environ, {"HERMES_HOME": str(self.home), "HERMES_KANBAN_HOME": str(kanban)})
        p.start()
        self.addCleanup(p.stop)

    def populate(self):
        """One current item per source, each holding the needle only in text beyond its excerpt."""
        deep = "intro " * 120 + NEEDLE
        needs.file_outside({"kind": "draft", "title": "Filed", "body": deep, "act_where": "Mail"},
                           self.home, profile="work", session_id="s1")
        chat_db(self.home / "state.db", [("s1", "cli", "u", "Should I merge it now? " + deep, "dm")])
        needs.record_wait(self.home, kind="clarify", request_id="0123456789abcdef", prompt="Which branch? " + NEEDLE)

    def test_search_finds_exactly_the_sources_the_matrix_says_it_searches(self):
        self.populate()
        pairing = {"source": "pairing", "id": "pairing:default:telegram:k", "version": "1", "title": NEEDLE,
                   "text": NEEDLE, "ask": NEEDLE, "tier": "ask", "profile": "default"}
        kanban = dict(pairing, source="kanban", id="kanban:default:t_1", title=NEEDLE)
        found = set()
        with patch.object(needs, "pairing_items", lambda *a, **k: [pairing]), \
                patch.object(needs, "kanban_items", lambda *a, **k: [kanban]):
            result = needs.search_text(NEEDLE)
        found = {m["source"] for m in result["matches"]}
        advertised = {r["source"] for r in needs.disclosure_matrix({}) if r["search"]}
        self.assertEqual(found, advertised)
        # Not only "no match": the readers of the sources that say no are never consulted.
        self.assertEqual(set(result["searched"]), {"outside", "chat", "prompt"})

    def test_the_result_names_the_sources_it_never_searches(self):
        """#45: a miss on a Kanban or pairing card is not "not found": the result names those sources."""
        self.populate()
        result = needs.search_text(NEEDLE)
        self.assertEqual(result.get("sources_not_searched"), ["kanban", "pairing"])

    def test_flipping_one_search_flag_makes_the_comparison_fail(self):
        """The test above is not a constant: a flag that disagrees with the search breaks it."""
        self.populate()
        flipped = {**needs.SURFACES, "chat": {**needs.SURFACES["chat"], "search": False}}
        with patch.object(needs, "SURFACES", flipped):
            advertised = {r["source"] for r in needs.disclosure_matrix({}) if r["search"]}
        found = {m["source"] for m in needs.search_text(NEEDLE)["matches"]}
        self.assertNotEqual(found, advertised)


@unittest.skipIf(fastapi is None, "fastapi not installed")
class SettingsFaultSendsNothing(HomeCase):
    """12-004 (class L): under a settings read fault no provider call happens, end to end."""

    def setUp(self):
        super().setUp()
        kanban = self.base / "kanban"
        kanban.mkdir()
        p = patch.dict(os.environ, {"HERMES_HOME": str(self.home), "HERMES_KANBAN_HOME": str(kanban)})
        p.start()
        self.addCleanup(p.stop)
        # A module instance of its own, so its settings loader is the real one.
        spec = importlib.util.spec_from_file_location("ub_fault_needs", ROOT / "needs.py")
        self.fresh = importlib.util.module_from_spec(spec)
        sys.modules["ub_fault_needs"] = self.fresh
        spec.loader.exec_module(self.fresh)
        self.fresh.hermes_home = lambda: self.home
        spec = importlib.util.spec_from_file_location("ub_fault_api", ROOT / "dashboard/plugin_api.py")
        self.api = importlib.util.module_from_spec(spec)
        sys.modules["ub_fault_api"] = self.api
        spec.loader.exec_module(self.api)
        self.sent = []
        outer = self

        class LLM:
            def complete(inner, messages, **kw):
                outer.sent.append(json.dumps(messages))
                return types.SimpleNamespace(text='{"bluf": "Decide.", "why": "Blocked."}', provider="p", model="m")

        self.api._loaded_ctx = lambda: types.SimpleNamespace(llm=LLM())
        self.api._needs = lambda: self.fresh
        self.api._BLUF_POOL = types.SimpleNamespace(submit=lambda fn, *a: fn(*a))
        app = fastapi.FastAPI()
        app.include_router(self.api.router)
        self.client = TestClient(app)
        from hermes_cli import kanban_db, kanban_db_connect

        with kanban_db_connect.connect(board="default") as conn:
            task = kanban_db.create_task(conn, title="Pick a plan", body="", assignee="worker")
            self.task_id = getattr(task, "id", task)
            kanban_db.block_task(conn, self.task_id, reason="Needs Ada approval", kind="needs_input")
        self.fresh.file_outside({"kind": "draft", "title": "Filed", "body": "Please review", "act_where": "Mail"},
                                self.home, profile="work", session_id="s1")
        chat_db(self.home / "state.db", [("s1", "cli", "u", ASK, "dm")])

    def run_queue(self, config_error):
        # An expired cache that still remembers the last good settings, as a running process has.
        self.fresh._SETTINGS_CACHE[:] = [0.0, {"principal_name": "Ada"}]
        config = {"plugins": {"entries": {"unbl0ck": {"settings": {"principal_name": "Ada"}}}}}

        class Config(dict):
            read_error = None

        def load():
            if config_error:
                raise ValueError("broken")
            return Config(config)

        with patch("hermes_cli.config.load_config_readonly", side_effect=load):
            return self.client.get("/queue").json()

    def test_a_healthy_read_summarizes_the_three_sources(self):
        """Positive control: the same setup does call the provider, so zero below means something."""
        q = self.run_queue(False)
        self.assertEqual(sorted({i["source"] for i in q["items"]}), ["chat", "kanban", "outside"])
        self.assertEqual(len(self.sent), 3, [i["source"] for i in q["items"]])

    def test_a_settings_fault_makes_no_provider_call_from_the_queue_or_from_ask(self):
        q = self.run_queue(True)
        self.assertTrue(any(e.startswith("settings:") for e in q["errors"]), q["errors"])
        # Items that would be summarized are still there (chats are not read on a settings fault).
        self.assertTrue({"kanban", "outside"} <= {i["source"] for i in q["items"]})
        self.assertFalse(q["bluf_enabled"] or q["ask_enabled"])
        self.assertFalse(any(r["summary"] or r["ask"] for r in q["disclosure"]))
        with patch("hermes_cli.config.load_config_readonly", side_effect=ValueError("broken")):
            self.fresh._SETTINGS_CACHE[:] = [0.0, {"principal_name": "Ada"}]
            resp = self.client.post("/card/ask", json={
                "board": "default", "task_id": self.task_id, "messages": [{"role": "user", "content": "q"}]})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.sent, [])


STORE_NAME = re.compile(r'["\']([A-Za-z0-9_.-]+\.(?:json|lock|gone|tmp))["\']')
# Hermes' own pairing file (read, never written), suffix filters and the short-lived write
# temporary files that the stores' own rows already cover.
STORE_IGNORED = {"-pending.json", ".json", ".tmp", ".gone"}


class StoreInventoryMatchesTheCode(unittest.TestCase):
    """12-005 (class I): every persistent file the code names is disclosed in the README and in
    the state table; adding a store without documenting it fails here."""

    def names(self):
        found = set()
        for rel in ("needs.py", "jobs.py", "__init__.py", "dashboard/plugin_api.py"):
            found |= set(STORE_NAME.findall((ROOT / rel).read_text()))
        return found - STORE_IGNORED

    def test_the_scan_finds_the_known_stores(self):
        self.assertTrue({"hidden.json", "bluf.json", "chat-backfill.json", "pending-release.json",
                         "pocket-pack.json", ".state.lock"} <= self.names(), self.names())

    def test_readme_discloses_every_store_it_writes(self):
        writes = README.split("- **Writes:**", 1)[1].split("In Desktop, two display toggles", 1)[0]
        for name in sorted(self.names()):
            self.assertIn(name, writes, f"README Writes does not list {name}")
        for folder in ("waits/", "outside/", "ask/"):
            self.assertIn(folder, writes)

    def test_state_table_lists_every_store(self):
        table = ARCH.split("## State lifecycle", 1)[1].split("\n\n", 3)[2]
        for name in sorted(self.names()):
            self.assertIn(name, table, f"architecture state table does not list {name}")
        for folder in ("waits/", "outside/", "ask/"):
            self.assertIn(folder, table)


if __name__ == "__main__":
    unittest.main()
