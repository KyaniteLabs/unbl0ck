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

    def test_search_reads_the_same_sources_as_the_queue_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            before = sorted(Path(d).rglob("*"))
            self.outside_items = [outside(1, "needle")]
            needs.search_text("needle")
            self.assertEqual(sorted(Path(d).rglob("*")), before)


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
        needs.hide(it["id"], "an-older-version", self.home)
        self.assertEqual(len(needs.search_text("change")["matches"]), 1)
        needs.hide(it["id"], it["version"], self.home)
        self.assertEqual(needs.search_text("change")["matches"], [])

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
        # Every list renders Item with the item id as its key, and the full-text result never
        # replaces the list: it only adds a summary line and marks matches.
        self.assertGreaterEqual(s.count("}, item.id))"), 2)
        self.assertNotIn("setItems(fullHits", s)
        self.assertNotIn("items={fullHits", s)


@unittest.skipIf(FastAPI is None, "fastapi missing")
class Endpoint(Fixture):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["HERMES_HOME"] = self.tmp.name
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


if __name__ == "__main__":
    unittest.main()
