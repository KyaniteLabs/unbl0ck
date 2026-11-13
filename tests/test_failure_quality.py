"""Failure injection: an incomplete queue and unavailable work must stay visible."""

import importlib.util
import builtins
import json
import sqlite3
import sys
import tempfile
import threading
import types
from pathlib import Path
from unittest.mock import patch

import jobs
import needs
import pytest

from test_outside import OutsideFixture, needs as outside_needs


class SourceFailures(OutsideFixture):
    def test_settings_failure_marks_the_queue_incomplete(self):
        with patch.object(outside_needs, "_SETTINGS_ERROR", None):
            with patch("hermes_cli.config.load_config_readonly", side_effect=ValueError("broken config")):
                failed = outside_needs._load_settings()
                assert failed["bluf_enabled"] is False
                assert failed["ask_enabled"] is False
            assert any(e.startswith("settings:") for e in outside_needs.build_queue()["errors"])
            with patch("hermes_cli.config.load_config_readonly", return_value={}):
                outside_needs._load_settings()
            assert not outside_needs.build_queue()["errors"]

    def test_corrupt_visibility_state_is_reported_and_cannot_be_overwritten(self):
        path = outside_needs._hidden_path(self.home)
        path.write_text("{bad")
        assert any(e.startswith("state:") for e in outside_needs.build_queue()["errors"])
        with pytest.raises(RuntimeError, match="could not read"):
            outside_needs.unhide("outside:filed:" + "b" * 32, self.home)
        assert path.read_text() == "{bad"

    def test_corrupt_filed_item_is_reported_without_hiding_healthy_items(self):
        self.file()
        bad = outside_needs._outside_dir(self.home) / ("a" * 32 + ".json")
        header = {"id": f"outside:filed:{bad.stem}", "origin": "filed", "kind": "draft"}
        for content in ("{bad", "[]", '{"origin":"filed"}', json.dumps(header),
                        json.dumps({**header, "version": "v1", "title": "Review", "act_where": "Mail", "text": ["wrong shape"]})):
            with self.subTest(content=content):
                bad.write_text(content)
                q = outside_needs.build_queue()
                self.assertEqual(len(q["items"]), 1)
                self.assertTrue(any(e.startswith("outside:") for e in q["errors"]), q)
                self.assertEqual(bad.read_text(), content)

    def test_watched_folder_limit_is_explicit(self):
        self.watch()
        for i in range(3):
            (self.folder / f"note-{i}.txt").write_text("Review this note")
        with patch.object(outside_needs, "_OUTSIDE_LIMIT", 2):
            q = outside_needs.build_queue()
        self.assertEqual(len(q["items"]), 2)
        self.assertTrue(any("limit" in e.lower() for e in q["errors"]), q)


def test_corrupt_prompt_record_is_reported_and_preserved():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        bad = needs._waits_dir(home) / ("a" * 32 + ".json")
        bad.write_text("{bad")
        needs.record_wait(home, request_id="b" * 32, kind="clarify", prompt="Choose a date")
        errors = []
        with patch.object(needs, "_alive", return_value=True):
            items = needs.wait_items(home, errors=errors)
        assert len(items) == 1
        assert errors and errors[0].startswith("prompt:")
        assert bad.read_text() == "{bad"


def test_live_prompt_capture_failure_is_visible_without_logging_prompt_text(caplog):
    with patch.object(needs, "_write_json", side_effect=OSError("disk full")):
        needs.record_wait(Path("/unused"), request_id="a" * 32, prompt="PRIVATE PROMPT")
    assert "could not record wait" in caplog.text
    assert "PRIVATE PROMPT" not in caplog.text


def test_one_failed_stale_prompt_cleanup_does_not_drop_a_live_prompt():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        stale = needs._waits_dir(home) / ("a" * 32 + ".json")
        needs._write_json(stale, {"request_id": "a" * 32, "pid": 0})
        needs.record_wait(home, request_id="b" * 32, kind="clarify", prompt="Choose a date")
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == stale:
                raise PermissionError("synthetic cleanup failure")
            return original(path, *args, **kwargs)
        errors = []
        with patch.object(Path, "unlink", unlink), patch.object(needs, "_alive", return_value=True):
            assert len(needs.wait_items(home, errors=errors)) == 1
        assert errors and "stale" in errors[0]


def test_chat_schema_failure_is_reported_but_unused_database_is_allowed():
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        conn = sqlite3.connect(home / "state.db")
        errors = []
        assert needs.chat_items(home, errors=errors) == []
        assert errors == []
        conn.execute("CREATE TABLE sessions (id TEXT, source TEXT)")
        conn.commit()
        errors = []
        assert needs.chat_items(home, errors=errors) == []
        assert errors and "chat[default]" in errors[0]
        conn.close()


@pytest.mark.parametrize("previous", [{}, {"principal_name": "Ada", "owner_user_ids": "telegram:42", "comment_author": "Ada", "bluf_enabled": False, "ask_enabled": False}])
@pytest.mark.parametrize("fault", ["raised", "sentinel", "import"])
def test_settings_read_fault_never_widens_identity_or_model_disclosure(previous, fault):
    # Other legacy suites replace needs._settings at collection time. Exercise a fresh
    # production module so the real cache and configuration loader are covered together.
    spec = importlib.util.spec_from_file_location("ub_settings_fault", Path(__file__).parents[1] / "needs.py")
    subject = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(subject)
    with patch.object(subject, "_SETTINGS_CACHE", [0.0, previous]):
        from hermes_cli.config import FailedConfigRead
        original_import = builtins.__import__
        def import_config(name, *args, **kwargs):
            if name == "hermes_cli.config":
                raise ImportError("broken config dependency")
            return original_import(name, *args, **kwargs)
        failure = patch("builtins.__import__", side_effect=import_config) if fault == "import" else patch(
            "hermes_cli.config.load_config_readonly",
            **({"return_value": FailedConfigRead(previous, error=ValueError("broken config"))} if fault == "sentinel" else {"side_effect": ValueError("broken config")}),
        )
        with failure:
            failed = subject._settings()
        assert failed["bluf_enabled"] is False
        assert failed["ask_enabled"] is False
        for key, value in previous.items():
            assert failed[key] == value
        errors = []
        with patch.object(subject.sqlite3, "connect") as connect:
            assert subject.chat_items(Path("/unused"), errors=errors) == []
            connect.assert_not_called()
        assert any(e.startswith("chat:") for e in errors)
        with patch("hermes_cli.kanban_db_connect.connect") as connect:
            with pytest.raises(RuntimeError, match="repair them before answering"):
                subject.answer_card("default", "t", "Approved", release=False, version="v1")
            connect.assert_not_called()
        # A repaired config restores the configured behavior at the next cache refresh.
        subject._SETTINGS_CACHE[0] = 0.0
        repaired = {"principal_name": "Grace", "bluf_enabled": True, "ask_enabled": True}
        with patch("hermes_cli.config.load_config_readonly", return_value={"plugins": {"entries": {"unbl0ck": {"settings": repaired}}}}):
            assert subject._settings() == repaired
        assert subject._SETTINGS_ERROR is None
        assert subject.principal_author() == "Grace"
        assert subject.bluf_enabled() and subject.ask_enabled()


def test_release_refuses_corrupt_recovery_state_before_touching_the_card():
    with patch.object(needs, "load_pending_releases", side_effect=RuntimeError("could not read pending-release.json")), patch("hermes_cli.kanban_db_connect.connect") as connect:
        with pytest.raises(RuntimeError, match="could not read"):
            needs.answer_card("default", "t", "Approved", release=True, version="v1")
        connect.assert_not_called()


def test_ask_expiry_does_not_require_another_request_or_allow_late_completion():
    now = [0.0]
    table = jobs.AskJobs(clock=lambda: now[0])
    table.create("poll")
    table.create("late")
    now[0] = table.TTL_S + 1
    assert table.get("poll") is None
    assert not table.finish("late", "too late")


def api_module():
    pytest.importorskip("fastapi")
    name = "ub_failure_quality_api"
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "dashboard/plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules[name] = api
    spec.loader.exec_module(api)
    return api


def test_visibility_write_failure_has_a_repairable_conflict_response():
    from fastapi import HTTPException
    api = api_module()
    def failed(*args):
        raise RuntimeError("could not read hidden.json; repair the record before changing it")
    api._needs = lambda: types.SimpleNamespace(hide=failed, unhide=failed)
    for operation in (lambda: api.hide(api.Hide(id="x", version="1")), lambda: api.unhide(api.Unhide(id="x"))):
        with pytest.raises(HTTPException) as exc:
            operation()
        assert exc.value.status_code == 409
        assert "repair" in exc.value.detail


def test_background_summary_backlog_is_bounded_and_failure_is_visible():
    api = api_module()
    submitted = []
    api._BLUF_POOL = types.SimpleNamespace(submit=lambda *args: submitted.append(args))
    items = [{"id": str(i), "version": "1"} for i in range(20)]
    for item in items:
        api._queue_bluf(None, None, item)
    assert len(submitted) == 2
    assert items[-1]["bluf_status"] == "waiting"
    api._BLUF_JOBS.fail(("0", "1"), "private provider detail")
    item = {"id": "0", "version": "1"}
    api._queue_bluf(None, None, item)
    assert item["bluf_status"] == "failed"
    assert "private provider detail" not in str(item)


def test_failed_summary_submission_does_not_strand_the_job():
    api = api_module()
    api._BLUF_POOL = types.SimpleNamespace(submit=lambda *args: (_ for _ in ()).throw(RuntimeError("pool closed")))
    item = {"id": "x", "version": "1"}
    api._queue_bluf(None, None, item)
    assert not api._BLUF_JOBS.is_pending(("x", "1"))
    assert item["bluf_status"] == "failed"


@pytest.mark.parametrize("fail", [False, True])
def test_worker_completion_or_failure_returns_model_capacity(fail):
    api = api_module()
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    def worker():
        if fail:
            raise RuntimeError("synthetic worker failure")
        return "done"
    if fail:
        with pytest.raises(RuntimeError):
            api._run_with_slot(slots, worker)
    else:
        assert api._run_with_slot(slots, worker) == "done"
    assert slots.acquire(blocking=False)


def test_ask_overload_and_failed_submission_are_explicit_and_recoverable():
    from fastapi import HTTPException
    api = api_module()
    api._needs = lambda: types.SimpleNamespace(ask_enabled=lambda: True, card_context=lambda *a: {}, build_ask_messages=lambda *a: [])
    api._checked_card_ref = lambda *a: ("default", "t")
    api._loaded_ctx = lambda: types.SimpleNamespace(llm=object())
    submitted = []
    api._ASK_POOL = types.SimpleNamespace(submit=lambda *args: submitted.append(args))
    body = api.CardAsk(id="x", messages=[{"role": "user", "content": "Which?"}])
    for _ in range(4):
        assert api.card_ask(body)["pending"]
    with pytest.raises(HTTPException) as exc:
        api.card_ask(body)
    assert exc.value.status_code == 429
    assert len(submitted) == 4
    api = api_module()
    api._needs = lambda: types.SimpleNamespace(ask_enabled=lambda: True, card_context=lambda *a: {}, build_ask_messages=lambda *a: [])
    api._checked_card_ref = lambda *a: ("default", "t")
    api._loaded_ctx = lambda: types.SimpleNamespace(llm=object())
    api._ASK_POOL = types.SimpleNamespace(submit=lambda *args: (_ for _ in ()).throw(RuntimeError("closed")))
    with pytest.raises(HTTPException) as exc:
        api.card_ask(body)
    assert exc.value.status_code == 503
    assert len(api._ASK_JOBS) == 0
