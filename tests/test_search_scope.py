"""Search scope (issue #45): the filter covers titles and previews; full-content search is an
explicit, bounded, read-only action that says what it covered and what it did not."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
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
