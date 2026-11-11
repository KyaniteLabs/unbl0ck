"""Wave 2: whole-prompt budget for Ask (U3), bounded state lock (U4), no raw exception text in
failed Ask jobs (U6) and request size caps (U7)."""
import importlib.util
import sys
import time
import types
from pathlib import Path

import pytest

import needs

ROOT = Path(__file__).resolve().parents[1]
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _total(messages):
    return sum(len(m["content"]) for m in messages)


def test_u3_ask_prompt_is_budgeted_by_total_size_newest_first():
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"{i:02d}" + "x" * 7998}
               for i in range(20)]
    card = {"id": "t_1", "title": "y" * 60000}
    msgs = needs.build_ask_messages(card, history)
    assert _total(msgs) <= needs.ASK_PROMPT_BUDGET
    tail = [m["content"][:2] for m in msgs[2:]]
    assert tail and tail[-1] == "19"                       # the newest turn survives
    assert tail == sorted(tail)                            # order preserved
    assert len(tail) < 20                                  # the oldest turns were dropped


def test_u3_one_huge_newest_message_is_trimmed_not_dropped():
    msgs = needs.build_ask_messages({"id": "t"}, [{"role": "user", "content": "q" * 8000}])
    assert msgs[-1]["role"] == "user" and len(msgs[-1]["content"]) == 8000
    assert _total(msgs) <= needs.ASK_PROMPT_BUDGET



def test_u4_state_lock_gives_up_at_the_deadline(tmp_path, monkeypatch):
    if needs.fcntl is None:
        pytest.skip("no cross-process lock on Windows")
    import fcntl, os
    d = needs.data_dir(tmp_path)
    fd = os.open(d / ".state.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)                         # another process holds the lock
    try:
        monkeypatch.setattr(needs, "STATE_LOCK_TIMEOUT", 0.3)
        # flock is per open-file-description, so a second open() in this process contends.
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            with needs._state_lock(tmp_path):
                pass
        assert time.monotonic() - t0 < 2
    finally:
        os.close(fd)
    with needs._state_lock(tmp_path):                      # free again: acquires at once
        pass


@pytest.fixture
def api(tmp_path):
    spec = importlib.util.spec_from_file_location("ub_api_w2", ROOT / "dashboard/plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ub_api_w2"] = mod
    spec.loader.exec_module(mod)
    ns = types.SimpleNamespace(ask_enabled=lambda: True, card_context=lambda b, t: {"id": t},
                               build_ask_messages=lambda c, h: list(h))
    mod._needs = lambda: ns
    mod._checked_card_ref = lambda i, b, t: ("factory", "t_1")
    return mod


def _client(mod):
    app = fastapi.FastAPI()
    app.include_router(mod.router)
    return TestClient(app)


def test_u6_failed_ask_keeps_exception_detail_out_of_the_response(api, caplog):
    marker = "SECRET-MARKER-https://provider.example/req_8812"

    def boom(messages, **kw):
        raise RuntimeError(marker)
    api._loaded_ctx = lambda: types.SimpleNamespace(llm=types.SimpleNamespace(complete=boom))
    c = _client(api)
    job = c.post("/card/ask", json={"id": "x", "messages": [{"role": "user", "content": "q"}]}).json()["job"]
    for _ in range(60):
        r = c.get(f"/card/ask/{job}")
        if not r.json().get("pending"):
            break
        time.sleep(0.05)
    assert r.status_code >= 400
    assert marker not in r.text
    assert "RuntimeError" in r.text


def test_u7_oversized_bodies_are_422(api):
    api._loaded_ctx = lambda: types.SimpleNamespace(llm=types.SimpleNamespace(complete=lambda m, **k: None))
    c = _client(api)
    big = "z" * 1_000_000
    assert c.post("/card/ask", json={"id": "x", "messages": [{"role": "user", "content": big}]}).status_code == 422
    many = [{"role": "user", "content": "q"}] * 500
    assert c.post("/card/ask", json={"id": "x", "messages": many}).status_code == 422
    assert c.post("/card/ask", json={"id": "x" * 5000, "messages": [{"role": "user", "content": "q"}]}).status_code == 422
    assert c.post("/kanban/answer", json={"board": "b", "task_id": "t", "text": big}).status_code == 422
    ok = c.post("/card/ask", json={"id": "x", "messages": [{"role": "user", "content": "q"}]})
    assert ok.status_code == 200
