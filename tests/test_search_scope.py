"""Search scope (issue #45): the filter covers titles and previews; full-content search is an
explicit, bounded, read-only action that says what it covered and what it did not."""

import importlib.util
import json
import os
import subprocess
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except Exception:  # noqa: BLE001
    FastAPI = None

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("unbl0ck_search_needs", ROOT / "needs.py")
needs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(needs)


def outside(i, text, title=None):
    return {"source": "outside", "id": f"outside:filed:{i:032x}", "version": f"v{i}", "title": title or f"Item {i}",
            "text": text, "ask": text[:80], "kind": "draft", "act_where": "Mail", "profile": "default"}


def chat(i, text, kind=None):
    return {"source": "chat", "id": f"chat:default:s{i}", "version": f"c{i}", "title": f"Chat {i}", "text": text,
            "ask": text[:80], "kind": kind, "profile": "default"}


class Fixture(unittest.TestCase):
    outside_items: list = []
    chat_items: list = []
    prompt_items: list = []
    chat_error = False

    def setUp(self):
        self.calls = {"model": 0}
        self.patches = [
            patch.object(needs, "outside_items", lambda *a, **k: list(self.outside_items)),
            patch.object(needs, "chat_items", self._chat),
            patch.object(needs, "wait_items", lambda *a, **k: list(self.prompt_items)),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _chat(self, *a, errors=None, **k):
        if self.chat_error:
            raise OSError("db locked")
        return list(self.chat_items)


class Matching(Fixture):
    def test_finds_text_that_the_preview_excerpt_does_not_contain(self):
        deep = ("intro " * 100) + "the zebra clause is here"
        self.outside_items = [outside(1, deep), outside(2, "nothing relevant")]
        r = needs.search_text("zebra")
        self.assertEqual([m["id"] for m in r["matches"]], [self.outside_items[0]["id"]])
        self.assertNotIn("zebra", self.outside_items[0]["ask"])
        self.assertEqual(r["matches"][0]["version"], "v1")
        self.assertEqual(r["matches"][0]["source"], "outside")

    def test_all_terms_must_match_case_and_unicode_insensitively(self):
        self.outside_items = [outside(1, "Zürich Straße 42 and 日本語 notes"), outside(2, "Zürich only")]
        self.assertEqual(len(needs.search_text("ZÜRICH straße")["matches"]), 1)
        self.assertEqual(len(needs.search_text("日本語")["matches"]), 1)
        self.assertEqual(len(needs.search_text("zürich")["matches"]), 2)

    def test_chats_are_searched_and_prompts_too(self):
        self.chat_items = [chat(1, "x" * 3000 + " needle in chat")]
        self.prompt_items = [{"source": "prompt", "id": "prompt:ops:1", "version": "p1", "kind": "approval", "title": "t",
                              "text": "run the needle job", "ask": "run", "profile": "ops"}]
        r = needs.search_text("needle")
        self.assertEqual({m["source"] for m in r["matches"]}, {"chat", "prompt"})

    def test_password_prompts_are_never_searched_or_returned(self):
        self.prompt_items = [{"source": "prompt", "id": "prompt:ops:2", "version": "p2", "kind": "sudo", "title": "sudo",
                              "text": "needle password", "ask": "pw", "profile": "ops"}]
        r = needs.search_text("needle")
        self.assertEqual(r["matches"], [])
        self.assertEqual(r["skipped_private"], 1)

    def test_result_carries_ids_and_versions_only_not_text(self):
        self.outside_items = [outside(1, "secret-sentence needle")]
        blob = json.dumps(needs.search_text("needle"))
        self.assertNotIn("secret-sentence", blob)

    def test_empty_or_oversized_query_is_rejected(self):
        for bad in ("", "   ", "x" * 201, None, 5):
            with self.assertRaises(ValueError):
                needs.search_text(bad)

    def test_too_many_terms_are_rejected(self):
        with self.assertRaises(ValueError):
            needs.search_text(" ".join(["a"] * 9))


class Bounds(Fixture):
    def test_more_than_200_items_in_a_source_reports_the_rest_as_not_searched(self):
        self.outside_items = [outside(i, "plain") for i in range(260)]
        self.outside_items[250]["text"] = "needle beyond the cap"
        r = needs.search_text("needle")
        self.assertEqual(r["matches"], [])
        self.assertEqual(r["searched"]["outside"], needs.SEARCH_MAX_ITEMS)
        self.assertEqual(r["not_searched"]["outside"], 60)
        self.assertTrue(r["partial"])

    def test_item_text_past_the_character_cap_is_flagged_not_silently_missed(self):
        self.outside_items = [outside(1, ("a " * (needs.SEARCH_MAX_CHARS)) + "needle")]
        r = needs.search_text("needle")
        self.assertEqual(r["matches"], [])
        self.assertEqual(r["items_cut"], 1)
        self.assertTrue(r["partial"])

    def test_complete_search_is_not_partial(self):
        self.outside_items = [outside(1, "needle")]
        r = needs.search_text("needle")
        self.assertFalse(r["partial"])
        self.assertEqual(r["not_searched"], {})
        self.assertEqual(r["errors"], [])

    def test_failed_source_is_reported_and_other_sources_still_searched(self):
        self.chat_error = True
        self.outside_items = [outside(1, "needle")]
        r = needs.search_text("needle")
        self.assertEqual(len(r["matches"]), 1)
        self.assertTrue(r["partial"])
        self.assertEqual(len(r["errors"]), 1)
        self.assertIn("chat", r["errors"][0])
        self.assertNotIn("db locked", r["errors"][0])

    def test_search_never_calls_the_model(self):
        self.outside_items = [outside(1, "needle")]
        with patch.object(needs, "ask_enabled", side_effect=AssertionError("model path touched"), create=True):
            needs.search_text("needle")


class RealReaders(unittest.TestCase):
    """The chat and live-prompt readers cut text at the search limit and expand records on demand."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        for name, value in (("_settings", lambda: {"principal_name": "Ada"}), ("hermes_home", lambda: self.home),
                            ("outside_items", lambda *a, **k: [])):
            p = patch.object(needs, name, value)
            p.start()
            self.addCleanup(p.stop)

    def chats(self, contents):
        db = sqlite3.connect(self.home / "state.db")
        db.execute("CREATE TABLE sessions(id TEXT, title TEXT, source TEXT, chat_type TEXT, started_at REAL,"
                   " last_activity_at REAL, user_id TEXT, display_name TEXT)")
        db.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,"
                   " timestamp REAL, display_kind TEXT, active INT)")
        now = time.time()
        for i, content in enumerate(contents):
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)", (f"s{i}", "", "cli", "dm", now - 60, now - 30, "u", "n"))
            db.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES(?, 'assistant', ?, ?)",
                       (f"s{i}", content, now - 30))
        db.commit()
        db.close()

    def test_chat_text_the_reader_cut_is_counted_as_cut_not_as_a_complete_miss(self):
        self.chats(["x " * 12000 + "needle? Should I merge it now?"])
        r = needs.search_text("needle")
        self.assertEqual((r["matches"], r["items_cut"], r["partial"]), ([], 1, True))

    def test_prompt_text_the_reader_cut_is_counted_as_cut(self):
        needs.record_wait(self.home, request_id="0123456789abcdef", kind="clarify",
                          prompt="y " * 15000 + "needle", platform="cli")
        r = needs.search_text("needle")
        self.assertEqual((r["matches"], r["items_cut"], r["partial"]), ([], 1, True))

    def test_only_the_searched_window_is_expanded_and_the_rest_are_still_counted(self):
        self.chats([f"Should I merge change {i} now?" for i in range(205)])
        calls = []
        real = needs._full_fields
        with patch.object(needs, "_full_fields", lambda text: calls.append(1) or real(text)):
            r = needs.search_text("change")
        self.assertEqual(len(calls), needs.SEARCH_MAX_ITEMS)
        self.assertEqual((r["searched"]["chat"], r["not_searched"]["chat"]), (200, 5))
        self.assertEqual(len(r["matches"]), 200)
        self.assertTrue(r["partial"])

    def test_the_queue_itself_still_carries_every_item_without_full_text(self):
        self.chats([f"Should I merge change {i} now?" for i in range(205)])
        items = needs.chat_items(self.home)
        self.assertEqual(len(items), 205)
        self.assertTrue(all("text" not in i for i in items))


class SearchBudget(RealReaders):
    """Round-3 review: the budget goes to cards a person could see, and coverage tells the truth."""

    test_chat_text_the_reader_cut_is_counted_as_cut_not_as_a_complete_miss = None
    test_prompt_text_the_reader_cut_is_counted_as_cut = None
    test_only_the_searched_window_is_expanded_and_the_rest_are_still_counted = None
    test_the_queue_itself_still_carries_every_item_without_full_text = None

    def test_hidden_chats_do_not_use_up_the_search_budget(self):
        self.chats([f"Should I merge change {i} now?" for i in range(201)])
        listed = needs.chat_items(self.home)
        for it in listed[:200]:
            needs.hide(it["id"], it["version"], self.home)
        last = listed[200]
        r = needs.search_text("change")
        self.assertEqual([m["id"] for m in r["matches"]], [last["id"]])
        self.assertEqual(r["searched"]["chat"], 1)
        self.assertNotIn("chat", r["not_searched"])
        self.assertFalse(r["partial"])

    def test_a_hidden_chat_that_changed_is_searched_again(self):
        self.chats(["Should I merge change 0 now?"])
        (it,) = needs.chat_items(self.home)
        # A dismissal of an older version (hide itself now refuses one, so it is written as
        # an older tab's hide would have left it).
        with patch.object(needs, "_current_version", lambda *a: None):
            needs.hide(it["id"], "an-older-version", self.home)
        self.assertEqual(len(needs.search_text("change")["matches"]), 1)
        needs.hide(it["id"], it["version"], self.home)
        self.assertEqual(needs.search_text("change")["matches"], [])

    def test_a_dismissed_live_prompt_is_not_searched_but_a_changed_one_is(self):
        needs.record_wait(self.home, request_id="0123456789abcdef", kind="clarify",
                          prompt="Should I ship the needle build?", platform="cli")
        (it,) = needs.wait_items(self.home)
        needs.hide(it["id"], it["version"], self.home)
        r = needs.search_text("needle")
        self.assertEqual((r["matches"], r["searched"].get("prompt", 0)), ([], 0))
        needs.record_wait(self.home, request_id="0123456789abcdef", kind="clarify",
                          prompt="Should I ship the other needle build?", platform="cli")
        self.assertEqual(len(needs.search_text("needle")["matches"]), 1)

    def test_password_prompts_are_skipped_not_counted_as_searched(self):
        needs.record_wait(self.home, request_id="0123456789abcdef", kind="sudo", prompt="sudo password for ada", platform="cli")
        r = needs.search_text("ada")
        self.assertEqual((r["matches"], r["skipped_private"]), ([], 1))
        self.assertEqual(r["searched"].get("prompt", 0), 0)


class SearchFolders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.a, self.b = Path(self.tmp.name) / "a", Path(self.tmp.name) / "b"
        self.a.mkdir()
        self.b.mkdir()
        settings = {"principal_name": "Ada", "outside_dirs": f"{self.a},{self.b}"}
        for name, value in (("_settings", lambda: settings), ("hermes_home", lambda: self.home),
                            ("chat_items", lambda *a, **k: []), ("wait_items", lambda *a, **k: [])):
            p = patch.object(needs, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_two_folders_share_one_read_budget(self):
        for folder in (self.a, self.b):
            for n in range(110):
                (folder / f"{n}.txt").write_text(f"note {n} in {folder.name}")
        reads = []
        real = needs._folder_outside_item
        with patch.object(needs, "_folder_outside_item", lambda *a, **k: reads.append(1) or real(*a, **k)):
            r = needs.search_text("note")
        self.assertLessEqual(len(reads), needs.SEARCH_MAX_ITEMS)
        self.assertEqual(r["searched"]["outside"], 200)
        self.assertEqual(r["not_searched"]["outside"], 20)
        self.assertTrue(r["partial"])
        self.assertEqual(len(r["matches"]), 200)

    def test_a_file_cut_at_the_read_limit_is_counted_as_cut(self):
        # The headers use up the whole read, so the body (and the term) is never seen, and the
        # text left over is short: only the reader's own `truncated` flag tells search it is partial.
        (self.a / "big.eml").write_text("Subject: hello\nX-Pad: " + "a" * 300000 + "\n\nthe needle is in the body\n")
        r = needs.search_text("needle")
        self.assertEqual((r["matches"], r["items_cut"], r["partial"]), ([], 1, True))

    def test_a_small_file_is_found_and_complete(self):
        (self.a / "small.txt").write_text("a needle here")
        r = needs.search_text("needle")
        self.assertEqual(len(r["matches"]), 1)
        self.assertFalse(r["partial"])

    def test_folder_files_past_the_folder_window_are_counted_not_silently_skipped(self):
        # Bug: a folder with more than 200 waiting files showed the newest 200 and the rest were
        # neither searched nor listed as not searched; only a generic read error appeared.
        for n in range(230):
            (self.a / f"{n}.txt").write_text(f"note {n}")
        r = needs.search_text("note")
        self.assertEqual(r["searched"]["outside"], 200)
        self.assertEqual(r["not_searched"]["outside"], 30)
        self.assertTrue(r["partial"])

    def test_a_damaged_hidden_file_is_reported_not_treated_as_no_dismissals(self):
        # Bug: a corrupt hidden.json read as empty, so dismissed cards became searchable and
        # the result said errors: [].
        (self.a / "x.txt").write_text("a needle here")
        hidden = needs.data_dir(self.home) / "hidden.json"
        hidden.write_text("{not json")
        r = needs.search_text("needle")
        self.assertTrue(r["partial"])
        self.assertTrue(any("dismiss" in e for e in r["errors"]), r["errors"])


class SearchProfileBoundary(unittest.TestCase):
    """Search reads exactly what the queue reads: every profile under the Hermes home, nothing outside it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "home"
        self.outside_home = Path(self.tmp.name) / "elsewhere"
        for profile_home, profile, text in (
            (self.root / "profiles" / "work", "work", "Should I ship the zebra release?"),
            (self.root / "profiles" / "personal", "personal", "Should I book the zebra trip?"),
            (self.outside_home, "stranger", "Should I wipe the zebra archive?"),
        ):
            profile_home.mkdir(parents=True)
            db = sqlite3.connect(profile_home / "state.db")
            db.execute("CREATE TABLE sessions(id TEXT, title TEXT, source TEXT, chat_type TEXT, started_at REAL,"
                       " last_activity_at REAL, user_id TEXT, display_name TEXT)")
            db.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,"
                       " timestamp REAL, display_kind TEXT, active INT)")
            now = time.time()
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)", (f"s-{profile}", "", "cli", "dm", now - 60, now - 30, "u", "n"))
            db.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES(?, 'assistant', ?, ?)",
                       (f"s-{profile}", text, now - 30))
            db.commit()
            db.close()
        for name, value in (("_settings", lambda: {"principal_name": "Ada"}), ("hermes_home", lambda: self.root),
                            ("outside_items", lambda *a, **k: []), ("wait_items", lambda *a, **k: [])):
            p = patch.object(needs, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_every_profile_under_the_home_is_searched_and_no_other_home_is(self):
        r = needs.search_text("zebra")
        sessions = {m["id"] for m in r["matches"]}
        self.assertEqual(len(sessions), 2)
        queue_ids = {i["id"] for i in needs.chat_items(self.root)}
        self.assertEqual(sessions, queue_ids)  # search finds exactly the cards the queue shows
        self.assertEqual(r["searched"]["chat"], 2)

    def test_results_carry_ids_and_versions_only_and_no_message_text(self):
        r = needs.search_text("zebra")
        self.assertTrue(all(set(m) == {"id", "version", "source"} for m in r["matches"]))
        blob = repr(r)
        for leak in ("stranger", "ship the", "book the", "wipe the"):
            self.assertNotIn(leak, blob)


class DesktopSearchPreservesState(unittest.TestCase):
    """Using the full-text button must not touch typed answers, drafts or open conversations."""

    def test_the_click_handler_changes_only_the_search_state(self):
        s = (ROOT / "desktop/src/plugin.src.js").read_text()
        start = s.index("const runFullSearch = async () => {")
        end = s.index("const fullSummary", start)
        body = s[start:end]
        setters = set(re.findall(r"\b(set[A-Z]\w*)\(", body))
        self.assertEqual(setters, {"setFullBusy", "setFullError", "setFullHits"})
        for forbidden in ("refresh(", "reload(", "load(", "setItems", "setQuery", "setSource", "localStorage", "composer", "notify("):
            self.assertNotIn(forbidden, body)
        self.assertEqual(body.count("ctx.rest("), 1)

    def test_a_late_search_answer_never_lands_after_the_query_changed(self):
        # Bug: a slow /search response for the old query still wrote its hits, error and busy flag
        # after the person had edited or cleared the query.
        s = (ROOT / "desktop/src/plugin.src.js").read_text()
        start = s.index("const runFullSearch = async () => {")
        end = s.index("const fullSummary", start)
        body = s[start:end]
        self.assertIn("searchGen.current", body)
        self.assertGreaterEqual(body.count("searchGen.current !== gen"), 2)  # hits and error
        self.assertIn("if (searchGen.current === gen) setFullBusy(false)", body)
        for fn in ("const clearFilters", "const changeQuery"):
            line = s[s.index(fn):].split("\n", 1)[0]
            self.assertIn("searchGen.current += 1", line)

    def test_cards_stay_mounted_by_id_so_their_state_survives_a_search(self):
        s = (ROOT / "desktop/src/plugin.src.js").read_text()
        # Every list renders Item keyed by its scope and item id, and the full-text result never
        # replaces the list: it only adds a summary line and marks matches.
        self.assertGreaterEqual(s.count("}, askKey(scope, item.id)))"), 2)
        self.assertNotIn("setItems(fullHits", s)
        self.assertNotIn("items={fullHits", s)


@unittest.skipIf(FastAPI is None, "fastapi missing")
class Endpoint(Fixture):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_HOME": self.tmp.name,
                                      "HERMES_KANBAN_HOME": str(Path(self.tmp.name) / "kanban")})
        env.start()
        self.addCleanup(env.stop)
        aspec = importlib.util.spec_from_file_location("unbl0ck_search_api", ROOT / "dashboard/plugin_api.py")
        self.api = importlib.util.module_from_spec(aspec)
        aspec.loader.exec_module(self.api)
        self.api._needs = lambda: needs
        app = FastAPI()
        app.include_router(self.api.router)
        self.client = TestClient(app)

    def test_search_endpoint_returns_matches_and_coverage(self):
        self.outside_items = [outside(1, "needle")]
        r = self.client.get("/search", params={"q": "needle"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["matches"]), 1)
        self.assertIn("searched", body)
        self.assertIn("partial", body)

    def test_bad_queries_are_400(self):
        for q in ("", "x" * 201, "a b c d e f g h i"):
            self.assertEqual(self.client.get("/search", params={"q": q}).status_code, 400, q)

    def test_search_is_get_only(self):
        self.assertEqual(self.client.post("/search", params={"q": "x"}).status_code, 405)


class DesktopSearch(unittest.TestCase):
    """The filter says what it covers, and full-text search is an explicit button."""

    def _src(self, name):
        return (ROOT / name).read_text()

    def test_label_says_titles_and_previews_of_loaded_cards(self):
        for name in ("desktop/src/plugin.src.js", "desktop/plugin.js"):
            s = self._src(name)
            self.assertIn("searches titles and previews of the cards loaded here", s, name)
            self.assertIn("Search full text", s, name)

    def test_full_text_search_is_never_automatic(self):
        s = self._src("desktop/src/plugin.src.js")
        self.assertIn("/search?q=", s)
        # Exactly one place calls the endpoint, and it is the click handler, not an effect.
        self.assertEqual(s.count("/search?q="), 1)
        i = s.index("/search?q=")
        window = s[max(0, i - 400):i]
        self.assertIn("const runFullSearch", window)
        self.assertNotIn("useEffect", window)


class SearchRealHome(unittest.TestCase):
    """Round 4: the real readers over a real temporary home. Only where the home and the plugin
    settings come from is replaced, and every environment variable is restored afterwards."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.settings = {"principal_name": "Ada"}
        env = patch.dict(os.environ, {"HERMES_HOME": str(self.home), "HERMES_KANBAN_HOME": str(self.root / "kanban"),
                                      "UNBL0CK_OUTSIDE_DIRS": ""})
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("_settings", lambda: self.settings), ("hermes_home", lambda: self.home)):
            p = patch.object(needs, name, value)
            p.start()
            self.addCleanup(p.stop)

    def file(self, key, body="a needle in the body", title="Reply"):
        return needs.file_outside({"kind": "draft", "title": title, "body": body, "act_where": "Mail",
                                   "dedupe_key": key}, self.home, profile="work", session_id="s1")

    def snapshot(self):
        out = {}
        for p in sorted(self.home.rglob("*")):
            out[str(p.relative_to(self.home))] = p.read_bytes() if p.is_file() else None
        return out

    def chat(self, title, content, sid="s1"):
        db = sqlite3.connect(self.home / "state.db")
        db.execute("CREATE TABLE IF NOT EXISTS sessions(id TEXT, title TEXT, source TEXT, chat_type TEXT, started_at REAL,"
                   " last_activity_at REAL, user_id TEXT, display_name TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,"
                   " timestamp REAL, display_kind TEXT, active INT)")
        now = time.time()
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)", (sid, title, "cli", "dm", now - 60, now - 30, "u", "n"))
        db.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES(?, 'assistant', ?, ?)",
                   (sid, content, now - 30))
        db.commit()
        db.close()

    # --- damaged dismissals (class G) ---------------------------------------------------

    def test_a_dismissal_entry_with_no_version_is_reported_not_read_as_resurfaced(self):
        made = self.file("k1")
        hidden = needs.data_dir(self.home) / "hidden.json"
        hidden.write_text(json.dumps({made["id"]: None}))
        r = needs.search_text("needle")
        self.assertEqual(len(r["matches"]), 1)
        self.assertTrue(r["partial"])
        self.assertTrue(any("dismiss" in e for e in r["errors"]), r["errors"])

    def test_every_malformed_dismissal_value_is_reported_and_a_good_one_is_not(self):
        made = self.file("k1")
        hidden = needs.data_dir(self.home) / "hidden.json"
        for bad in (5, ["v"], {"a": 1}, True, ""):
            hidden.write_text(json.dumps({made["id"]: bad}))
            r = needs.search_text("needle")
            self.assertTrue(any("dismiss" in e for e in r["errors"]), (bad, r["errors"]))
        hidden.write_text(json.dumps({"outside:filed:" + "0" * 32: "some-version"}))
        r = needs.search_text("needle")
        self.assertEqual((r["errors"], r["partial"]), ([], False))

    # --- read-only contract (class G/I) -------------------------------------------------

    def test_search_deletes_no_expired_done_record_and_no_stale_wait(self):
        made = self.file("k1")
        (item,) = needs.outside_items(self.home)
        needs.done_outside(item["id"], item["version"], self.home)
        path = needs.data_dir(self.home) / "outside" / (made["id"].split(":")[-1] + ".json")
        rec = json.loads(path.read_text())
        rec["done_at"] -= needs.OUTSIDE_UNDO_SECONDS + 1
        path.write_text(json.dumps(rec))
        needs.record_wait(self.home, kind="clarify", request_id="0123456789abcdef", prompt="Which branch?")
        wait = needs.data_dir(self.home) / "waits" / "0123456789abcdef.json"
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        stale = needs._read_json(wait)
        stale["pid"], stale["proc_start"] = dead.pid, "1"
        needs._write_json(wait, stale)
        before = self.snapshot()
        needs.search_text("branch")
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(path.exists() and wait.exists())

    def test_searching_a_fresh_home_creates_no_state_folders(self):
        r = needs.search_text("anything")
        self.assertEqual(r["errors"], [])
        self.assertEqual(list(self.home.rglob("*")), [])

    def test_the_queue_still_cleans_up_what_search_leaves_alone(self):
        made = self.file("k1")
        (item,) = needs.outside_items(self.home)
        needs.done_outside(item["id"], item["version"], self.home)
        path = needs.data_dir(self.home) / "outside" / (made["id"].split(":")[-1] + ".json")
        rec = json.loads(path.read_text())
        rec["done_at"] -= needs.OUTSIDE_UNDO_SECONDS + 1
        path.write_text(json.dumps(rec))
        needs.search_text("needle")
        self.assertTrue(path.exists())
        needs.outside_items(self.home)
        self.assertFalse(path.exists())

    # --- a full queue of healthy records is not a read failure (class G) -----------------

    def test_exactly_the_filed_capacity_of_healthy_records_is_complete(self):
        for n in range(needs._OUTSIDE_LIMIT):
            self.file(f"key-{n}", body=f"record {n} needle")
        r = needs.search_text("needle")
        self.assertEqual(r["searched"]["outside"], needs._OUTSIDE_LIMIT)
        self.assertEqual(len(r["matches"]), needs._OUTSIDE_LIMIT)
        self.assertEqual((r["errors"], r["partial"]), ([], False))

    # --- one query over every field a card is filtered on (class K) -----------------------

    def test_terms_may_be_split_between_the_chat_title_and_the_deep_body(self):
        body = "needle " + "filler words " * 200 + "Should I merge it now?"
        self.chat("Release", body)
        r = needs.search_text("release needle")
        self.assertEqual([m["source"] for m in r["matches"]], ["chat"])
        self.assertFalse(r["partial"])
        self.assertEqual(needs.search_text("release nothing-like-this")["matches"], [])

    def test_terms_may_be_split_between_the_app_and_the_body(self):
        self.file("k1", body="the needle sentence", title="Reply")
        self.assertEqual(len(needs.search_text("mail needle")["matches"]), 1)
        self.assertEqual(len(needs.search_text("reply mail needle")["matches"]), 1)
        self.assertEqual(needs.search_text("skype needle")["matches"], [])


class SearchFolderBudget(unittest.TestCase):
    """Round 4: overlapping and failing watched folders against the shared read budget."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.settings = {"principal_name": "Ada"}
        env = patch.dict(os.environ, {"HERMES_HOME": str(self.home), "HERMES_KANBAN_HOME": str(self.root / "kanban"),
                                      "UNBL0CK_OUTSIDE_DIRS": ""})
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("_settings", lambda: self.settings), ("hermes_home", lambda: self.home),
                            ("chat_items", lambda *a, **k: []), ("wait_items", lambda *a, **k: [])):
            p = patch.object(needs, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_files_left_unread_under_two_overlapping_roots_are_counted_once(self):
        parent = self.root / "parent"
        child = parent / "child"
        child.mkdir(parents=True)
        for n in range(230):
            (child / f"{n}.txt").write_text(f"note {n}")
        self.settings.update(outside_dirs=f"{child},{parent}", outside_recursive=True)
        r = needs.search_text("note")
        self.assertEqual(r["searched"]["outside"], 200)
        self.assertEqual(r["not_searched"]["outside"], 30)

    def test_reads_that_fail_still_use_up_the_shared_budget(self):
        a, b = self.root / "a", self.root / "b"
        for folder in (a, b):
            folder.mkdir()
            for n in range(200):
                (folder / f"{n}.txt").write_text(f"note {n}")
        self.settings["outside_dirs"] = f"{a},{b}"
        attempts = []
        with patch.object(needs, "_folder_outside_item", lambda *args, **k: attempts.append(1)):  # changed mid-read
            r = needs.search_text("note")
        self.assertLessEqual(len(attempts), needs.SEARCH_MAX_ITEMS)
        self.assertTrue(r["partial"])
        self.assertEqual(r["not_searched"]["outside"], 200)

    def test_dismissed_files_left_unread_by_one_root_and_read_by_another_are_not_counted_missing(self):
        parent = self.root / "parent"
        child = parent / "child"
        child.mkdir(parents=True)
        for n in range(5):
            (child / f"{n}.txt").write_text(f"note {n}")
        self.settings.update(outside_dirs=f"{child},{parent}", outside_recursive=True)
        for item in needs.outside_items(self.home):
            needs.hide(item["id"], item["version"], self.home)
        stats = {}
        with patch.object(needs, "_OUTSIDE_LIMIT", 3):
            listed = needs.outside_items(self.home, with_hidden=True, stats=stats)
        self.assertEqual(len(listed), 5)
        self.assertEqual(stats.get("hidden_unread", 0), 0)


class DesktopSearchExecuted(unittest.TestCase):
    """Round 4: the page is run, not read. Click the button, render the answer, change the queue."""

    PRELUDE = r"""
let queueNow={items:[{source:'outside',id:'outside:filed:a',version:'v1',title:'Reply to Sam',ask:'x'},
                     {source:'outside',id:'outside:filed:b',version:'v1',title:'Other',ask:'y'}],count:2,errors:[],at:1000};
const refs=[];let rc=0;
env.useRef=()=>{const i=rc++;return refs[i]||(refs[i]={current:0})};
env.useValue=key=>key==='queue'?queueNow:key===env.$undo?undo:false;
const render=ctx=>{cursor=0;rc=0;return Page({ctx,refresh})};
const status=tree=>{const n=all(tree).find(n=>n?.props?.className==='ub-filter-status');return n?String(n.props.children):''};
const typeQuery=(tree,v)=>all(tree).find(n=>n?.type==='input'&&n.props.type==='search').props.onChange({target:{value:v}});
const reply=(matches,extra={})=>({matches,searched:{outside:2},not_searched:{},items_cut:0,errors:[],partial:false,
  limits:{items_per_source:200,chars_per_item:20000},skipped_private:0,...extra});
const shown=tree=>all(tree).filter(n=>n?.type===Item&&!n.props.hidden).length;
"""

    def run_views(self, script):
        from test_operator_recovery import HARNESS
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                r = subprocess.run(["node", "-e", HARNESS + self.PRELUDE + script, str(path)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_button_asks_once_and_the_answer_is_rendered_with_its_coverage(self):
        self.run_views(r"""
let asked=[];
const ctx={rest:async u=>{asked.push(u);return reply([{id:'outside:filed:b',version:'v1',source:'outside'}],{not_searched:{outside:30},partial:true})}};
(async()=>{
 typeQuery(render(ctx),'zzz');
 assert.equal(asked.length,0,'typing must not search');
 assert.equal(shown(render(ctx)),0);
 await find(render(ctx),'Search full text').onClick();
 assert.deepEqual(asked,['/search?q=zzz']);
 const t=render(ctx);
 assert.equal(shown(t),1,'only the matched card is shown');
 const s=status(t);
 assert.match(s,/2 outside/);assert.match(s,/30 outside not searched/);assert.match(s,/may be incomplete/);
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_a_match_for_another_version_does_not_show_the_card(self):
        self.run_views(r"""
const ctx={rest:async()=>reply([{id:'outside:filed:b',version:'OLD',source:'outside'}])};
(async()=>{
 typeQuery(render(ctx),'zzz');
 await find(render(ctx),'Search full text').onClick();
 const t=render(ctx);
 assert.equal(shown(t),0,'a stale version never reveals the current card');
 assert.match(status(t),/1 match\(es\) are not on a card loaded now/);
 assert.match(status(t),/may be incomplete/);
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_a_refresh_after_the_search_says_to_search_again(self):
        self.run_views(r"""
const ctx={rest:async()=>reply([{id:'outside:filed:b',version:'v1',source:'outside'}])};
(async()=>{
 typeQuery(render(ctx),'zzz');
 await find(render(ctx),'Search full text').onClick();
 assert.doesNotMatch(status(render(ctx)),/refreshed/);
 queueNow={...queueNow,at:2000};
 assert.match(status(render(ctx)),/search again/);
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_a_late_answer_after_the_query_changed_is_dropped(self):
        self.run_views(r"""
let release;const ctx={rest:()=>new Promise(r=>{release=r})};
(async()=>{
 typeQuery(render(ctx),'zzz');
 const pending=find(render(ctx),'Search full text').onClick();
 typeQuery(render(ctx),'zzzz');
 typeQuery(render(ctx),'zzz');  // same text again: only the generation tells the answers apart
 release(reply([{id:'outside:filed:b',version:'v1',source:'outside'}]));
 await pending;
 const t=render(ctx);
 assert.equal(shown(t),0,'the late answer must not reveal a card');
 assert.doesNotMatch(status(t),/full text searched/);
 assert.equal(find(t,'Search full text').disabled,false);
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_a_failed_search_shows_no_hits(self):
        self.run_views(r"""
const ctx={rest:async()=>{throw new Error('offline')}};
(async()=>{
 typeQuery(render(ctx),'zzz');
 await find(render(ctx),'Search full text').onClick();
 const t=render(ctx);
 assert.equal(shown(t),0);
 assert.ok(all(t).some(n=>typeof n==='string'&&n.includes('offline')));
})().catch(e=>{console.error(e);process.exitCode=1});
""")


class SearchDocs(unittest.TestCase):
    """Round 4: the guides say what search does, and the verification map matches the tests."""

    def _flat(self, name):
        return " ".join((ROOT / name).read_text().split())

    def test_operator_guide_states_the_cap_per_source_and_what_the_page_shows(self):
        g = self._flat("docs/operator-guide.md")
        self.assertIn("at most 200 items per source", g)
        self.assertNotIn("at most 200 items and", g)
        self.assertIn("the page shows a match only on a card of the same id and version", g)
        self.assertIn("Search only reads: it deletes, rewrites and creates nothing", g)

    def test_verification_map_names_the_executed_search_tests_that_exist(self):
        v = self._flat("docs/verification.md")
        self.assertNotIn("are not yet written", v)
        me = Path(__file__).read_text()
        for name in ("SearchProfileBoundary", "SearchRealHome", "SearchFolderBudget", "DesktopSearchExecuted"):
            self.assertIn(f"`{name}`", v)
            self.assertIn(f"class {name}(", me)


if __name__ == "__main__":
    unittest.main()
