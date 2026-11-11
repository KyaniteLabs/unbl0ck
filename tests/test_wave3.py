"""Wave 3 (Sonnet audit): BLUF egress scrub at the model boundary (UB-2), Ask history turn cap
(UB-3) and the BLUF retry cap over 24 hours (UB-1b)."""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

import jobs
import needs

ROOT = Path(__file__).resolve().parents[1]
fastapi = pytest.importorskip("fastapi")


def _api():
    spec = importlib.util.spec_from_file_location("ub_api_w3", ROOT / "dashboard/plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ub_api_w3"] = mod
    spec.loader.exec_module(mod)
    return mod


LEAKS = [
    "sk-" + "Q" * 40,
    "ghp_" + "r" * 36,
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJlMTIzNDU2",
    "/home/alice/secret-project/app.py",
    "/Users/bob/Documents/plans.txt",
    "C:\\Users\\carol\\Desktop\\x.txt",
    "password=hunter2hunter2",
]


def test_ub2_credential_and_home_path_shapes_never_reach_ctx_llm_complete():
    api = _api()
    seen = []

    def complete(messages, **kw):
        seen.append(messages)
        return types.SimpleNamespace(text='{"bluf": "ok", "why": "fine"}')

    ctx = types.SimpleNamespace(llm=types.SimpleNamespace(complete=complete))
    n = types.SimpleNamespace(
        recent_comments=lambda it: [{"author": "a", "body": f"see {LEAKS[3]} and {LEAKS[5]}"}],
        bluf_messages=needs.bluf_messages, parse_bluf=needs.parse_bluf, save_bluf=lambda *a: None)
    item = {"id": "kanban:b:t", "version": "1", "title": f"Fix {LEAKS[0]} in {LEAKS[4]}",
            "ask": " ".join(LEAKS[1:3] + LEAKS[6:]), "owner": "o"}
    api._run_bluf(n, ctx, item)
    assert seen, "the model was never called"
    blob = repr(seen)
    for leak in LEAKS:
        assert leak not in blob, leak
    for frag in ("hunter2hunter2", "alice", "bob", "carol"):
        assert frag not in blob, frag


def test_ub3_ask_history_is_capped_at_six_turns():
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i:02d}"} for i in range(40)]
    msgs = needs.build_ask_messages({"id": "t"}, history)
    turns = msgs[2:]
    assert len(turns) == needs.ASK_HISTORY_TURNS == 6
    assert turns[-1]["content"] == "turn 39"                     # newest kept


def test_ub3_forty_long_turns_stay_inside_the_budget():
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 8000} for i in range(40)]
    msgs = needs.build_ask_messages({"id": "t", "title": "y" * 60000}, history)
    assert sum(len(m["content"]) for m in msgs) <= needs.ASK_PROMPT_BUDGET
    assert len(msgs[2:]) <= 6


def test_ub3_the_turn_cap_is_a_setting():
    assert needs.ask_history_turns({"ask_history_turns": 2}) == 2
    assert needs.ask_history_turns({"ask_history_turns": "junk"}) == 6
    assert needs.ask_history_turns({"ask_history_turns": 0}) == 6
    msgs = needs.build_ask_messages({"id": "t"}, [{"role": "user", "content": str(i)} for i in range(9)], turns=2)
    assert len(msgs[2:]) == 2


