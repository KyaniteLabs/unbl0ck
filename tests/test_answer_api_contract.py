"""Answer preconditions and identifier validation across the HTTP routes."""

import importlib
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
HERMES_SOURCE = os.environ.get("UNBL0CK_HERMES_SOURCE")
if HERMES_SOURCE:
    sys.path.insert(0, HERMES_SOURCE)
sys.path.insert(0, str(ROOT))

import needs

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    kanban_db = importlib.import_module("hermes_cli.kanban_db")
    kanban_db_connect = importlib.import_module("hermes_cli.kanban_db_connect")
    _MISSING = None
except Exception as exc:  # noqa: BLE001
    _MISSING = str(exc)


@unittest.skipIf(_MISSING, f"Hermes or FastAPI unavailable: {_MISSING}")
class AnswerContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        p = patch.dict(os.environ, {"HERMES_HOME": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)
        self.settings = {"principal_name": "CEO", "comment_author": "operator"}
        p = patch.object(needs, "_settings", lambda: self.settings)
        p.start()
        self.addCleanup(p.stop)
        spec = importlib.util.spec_from_file_location("ub_api_answer_contract", ROOT / "dashboard/plugin_api.py")
        self.api = importlib.util.module_from_spec(spec)
        sys.modules["ub_api_answer_contract"] = self.api
        spec.loader.exec_module(self.api)
        self.api._needs = lambda: needs
        self.api._loaded_ctx = lambda: None
        app = FastAPI()
        app.include_router(self.api.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def block(self, title="Choose", reason="Needs CEO approval"):
        with kanban_db_connect.connect(board="default") as conn:
            tid = kanban_db.create_task(conn, title=title, body="", assignee="worker")
            tid = getattr(tid, "id", tid)
            kanban_db.block_task(conn, tid, reason=reason, kind="needs_input")
            return tid, str(needs._latest_block(conn, kanban_db, tid).id)

    def reblock(self, tid):
        """A newer block on the same blocked card (a worker blocks it again)."""
        with kanban_db_connect.connect(board="default") as conn:
            old = needs._latest_block(conn, kanban_db, tid)
            with kanban_db.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
                conn.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, 'blocked', ?, ?)",
                    (tid, old.run_id, '{"kind":"needs_input"}', int(old.created_at) + 1),
                )
            return str(needs._latest_block(conn, kanban_db, tid).id)

    def comments(self, tid):
        with kanban_db_connect.connect(board="default") as conn:
            return [c.body for c in kanban_db.list_comments(conn, tid)]

    def post(self, tid, **extra):
        body = {"board": "default", "task_id": tid, "text": "Use option A é ✓", "release": False}
        body.update(extra)
        return self.client.post("/kanban/answer", json=body)

    # --- versions -------------------------------------------------------------------------

    def test_versioned_answer_reports_the_version_it_applied_to(self):
        tid, version = self.block()
        r = self.post(tid, version=version)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["version"], version)
        self.assertTrue(r.json()["versioned"])
        self.assertNotIn("warning", r.headers)
        self.assertEqual(self.comments(tid), ["Use option A é ✓"])

    def test_legacy_unversioned_answer_still_works_and_is_flagged(self):
        tid, version = self.block()
        r = self.post(tid)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["versioned"])
        self.assertEqual(body["version"], version)  # so the caller can adopt versions
        self.assertIn("version", r.headers["warning"])
        self.assertEqual(self.comments(tid), ["Use option A é ✓"])

    def test_required_version_setting_refuses_unversioned_without_mutating(self):
        self.settings["require_answer_version"] = True
        tid, version = self.block()
        r = self.post(tid)
        self.assertEqual(r.status_code, 428)
        self.assertEqual(self.comments(tid), [])
        self.assertEqual(self.post(tid, version=version).status_code, 200)

    def test_required_version_applies_to_release_without_changing_its_400(self):
        tid, _ = self.block()
        self.assertEqual(self.post(tid, release=True).status_code, 400)
        self.assertEqual(self.comments(tid), [])

    def test_stale_version_is_409_and_never_posts_or_releases(self):
        tid, old = self.block()
        new = self.reblock(tid)
        self.assertNotEqual(old, new)
        for release in (False, True):
            r = self.post(tid, version=old, release=release)
            self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.comments(tid), [])
        with kanban_db_connect.connect(board="default") as conn:
            self.assertEqual(str(getattr(kanban_db.get_task(conn, tid).status, "value", kanban_db.get_task(conn, tid).status)), "blocked")

    def test_retry_never_duplicates_the_comment_or_releases_a_newer_block(self):
        tid, v1 = self.block()
        self.assertEqual(self.post(tid, version=v1).status_code, 200)
        self.assertEqual(self.post(tid, version=v1).status_code, 409)  # already answered
        self.assertEqual(self.post(tid, version=v1, release=True).status_code, 200)
        self.assertEqual(self.comments(tid), ["Use option A é ✓"])
        v2 = self.reblock(tid)
        r = self.post(tid, version=v1, release=True)  # retry of the old release
        self.assertEqual(r.status_code, 409)
        with kanban_db_connect.connect(board="default") as conn:
            self.assertEqual(str(getattr(kanban_db.get_task(conn, tid).status, "value", kanban_db.get_task(conn, tid).status)), "blocked")
        self.assertEqual(self.comments(tid), ["Use option A é ✓"])
        self.assertNotEqual(v1, v2)

    def test_empty_version_is_rejected_not_treated_as_missing(self):
        tid, _ = self.block()
        self.assertEqual(self.post(tid, version="").status_code, 400)
        self.assertEqual(self.comments(tid), [])

    # --- identifiers ----------------------------------------------------------------------

    def test_card_reference_bounds_apply_to_get_card(self):
        tid, _ = self.block()
        long = "x" * 301
        for params in ({"id": long}, {"board": long, "task_id": tid}, {"board": "default", "task_id": long}):
            self.assertEqual(self.client.get("/card", params=params).status_code, 400, params)
        self.assertEqual(self.client.get("/card", params={"id": f"kanban:default:{tid}"}).status_code, 200)

    def test_malformed_or_conflicting_references_do_not_retarget(self):
        tid, _ = self.block()
        for params in (
            {"id": "kanban:default", "board": "default", "task_id": tid},  # malformed id, valid fallbacks
            {"id": "kanban::t", "board": "default", "task_id": tid},
            {"id": f"kanban:default:{tid}", "board": "other"},
            {"id": f"kanban:default:{tid}", "task_id": "someone-else"},
            {"id": "chat:p:s", "board": "default", "task_id": tid},
            {"id": "outside:filed:" + "a" * 32},
            {"id": f"kanban:default:{tid}\x00"},
            {"board": "default", "task_id": tid + "\n"},
        ):
            self.assertEqual(self.client.get("/card", params=params).status_code, 400, params)

    def test_same_bounds_on_hide_unhide_and_outside_done(self):
        long = "x" * 301
        self.assertEqual(self.client.post("/hide", json={"id": long, "version": "1"}).status_code, 400)
        self.assertEqual(self.client.post("/hide", json={"id": "chat:p:s", "version": long}).status_code, 400)
        self.assertEqual(self.client.post("/unhide", json={"id": long}).status_code, 400)
        self.assertEqual(self.client.post("/outside/done", json={"id": long, "version": "1"}).status_code, 400)
        self.assertEqual(self.client.post("/outside/done", json={"id": "outside:filed:" + "a" * 32, "version": long}).status_code, 400)
        exact = "x" * 300  # the documented limit itself is accepted
        self.assertEqual(self.client.post("/hide", json={"id": exact, "version": exact}).status_code, 200)

    def test_ask_job_identifier_is_bounded(self):
        self.assertEqual(self.client.get("/card/ask/" + "j" * 301).status_code, 404)

    def test_oversize_answer_fields_stay_422(self):
        tid, _ = self.block()
        self.assertEqual(self.post(tid, version="v" * 301).status_code, 422)
        self.assertEqual(self.post("t" * 301).status_code, 422)


if __name__ == "__main__":
    unittest.main()
