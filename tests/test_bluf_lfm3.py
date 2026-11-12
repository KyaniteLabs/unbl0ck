"""LFM3 eval fixes: a BLUF must be a real sentence, never a placeholder or reasoning text."""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ub_needs_lfm3", ROOT / "needs.py")
needs = importlib.util.module_from_spec(spec)
sys.modules["ub_needs_lfm3"] = needs
spec.loader.exec_module(needs)


@pytest.mark.parametrize(
    "value", ["...", "…", "tbd", "TODO", "<bluf>", "n/a", "   ....  "]
)
def test_placeholder_blufs_are_rejected(value):
    reply = f'{{"bluf": "{value}", "why": "because the release waits"}}'
    with pytest.raises(ValueError):
        needs.parse_bluf(reply)


def test_placeholder_does_not_hide_a_real_answer_after_it():
    reply = '{"bluf": "...", "why": "..."} then {"bluf": "Approve the deploy now.", "why": "tests pass"}'
    assert needs.parse_bluf(reply)[0] == "Approve the deploy now."


@pytest.mark.parametrize(
    "reply",
    [
        '<think>The answer is {"bluf": "Approve the deploy now.", "why": "tests pass"}</think>',
        '<think>Draft: {"bluf": "Approve the deploy now.", "why": "tests pass"}',
        '<thinking>{"bluf": "Approve the deploy now.", "why": "ok"}</thinking>   ',
    ],
)
def test_json_only_inside_the_thought_does_not_parse(reply):
    with pytest.raises(ValueError):
        needs.parse_bluf(reply)


def test_bluf_token_budget_floor():
    src = (ROOT / "dashboard" / "plugin_api.py").read_text()
    ask = int(re.search(r"^ASK_MAX_TOKENS\s*=\s*(\d+)", src, re.MULTILINE).group(1))
    assert ask >= 3500  # LFM3 at 2048 fails ~3 of 12: reasoning eats the budget
    m = re.search(r"^BLUF_MAX_TOKENS\s*=\s*(\w+)", src, re.MULTILINE)
    assert m.group(1) == "ASK_MAX_TOKENS" or int(m.group(1)) >= 3500


@pytest.mark.parametrize("value", ["Merge PR.", "Pick A.", "Real"])
def test_short_real_blufs_are_accepted(value):
    reply = f'{{"bluf": "{value}", "why": "because the release waits"}}'
    assert needs.parse_bluf(reply)[0] == value
