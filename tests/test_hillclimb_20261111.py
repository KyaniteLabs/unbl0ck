"""Hill-climb 2026-11-11: BLUF parsing on reasoning-model replies (F09), bounded BLUF
retries (F12), and redaction of the BLUF payload (F4, same leak class as Ask)."""
import json
import os

import pytest

import jobs
import needs

# --- F09: parse_bluf on the five reply shapes a small or reasoning model produces
REPLIES = {
    "plain": '{"bluf": "Approve the deploy.", "why": "tests pass"}',
    "fenced": '```json\n{"bluf": "Approve the deploy.", "why": "tests pass"}\n```',
    "think_with_braces": '<think>maybe {x} or {"bluf": "no"}?</think>\n{"bluf": "Approve the deploy.", "why": "tests pass"}',
    "truncated_think": '<think>I wonder whether {"bluf": "draft"} is',
    "two_objects": '{"note": 1}\n{"bluf": "Approve the deploy.", "why": "tests pass"}',
}


@pytest.mark.parametrize("name", ["plain", "fenced", "think_with_braces", "two_objects"])
def test_parse_bluf_shapes(name):
    bluf, why = needs.parse_bluf(REPLIES[name])
    assert bluf == "Approve the deploy." and why == "tests pass"


def test_parse_bluf_truncated_think_is_a_clean_failure():
    with pytest.raises(ValueError):
        needs.parse_bluf(REPLIES["truncated_think"])


def test_bluf_max_tokens_leaves_room_for_reasoning():
    src = open(os.path.join(os.path.dirname(__file__), "..", "dashboard", "plugin_api.py")).read()
    assert "max_tokens=200" not in src


# --- F12: retries back off and stop
class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def test_bluf_retries_back_off_and_stop():
    c = Clock()
    j = jobs.BlufJobs(clock=c)
    k = ("i", "v1")
    waits = []
    for attempt in range(jobs.BlufJobs.MAX_ATTEMPTS):
        assert j.start(k), attempt
        j.fail(k, "boom")
        w = 0
        while not j.can_start(k) and w < 10 * 86400:
            c.t += 60
            w += 60
        waits.append(w)
    # after MAX_ATTEMPTS failures, it never starts again for this version
    assert waits[-1] >= 10 * 86400
    assert waits[0] >= jobs.BlufJobs.RETRY_S
    assert waits[1] > waits[0]
    assert j.attempts(k) == jobs.BlufJobs.MAX_ATTEMPTS


def test_new_version_starts_fresh():
    c = Clock()
    j = jobs.BlufJobs(clock=c)
    for _ in range(jobs.BlufJobs.MAX_ATTEMPTS):
        j.start(("i", "v1"))
        j.fail(("i", "v1"), "x")
        c.t += 10 * 86400
    assert j.start(("i", "v2"))


def test_success_after_failure_clears():
    c = Clock()
    j = jobs.BlufJobs(clock=c)
    k = ("i", "v1")
    j.start(k); j.fail(k, "x"); c.t += jobs.BlufJobs.RETRY_S
    assert j.start(k)
    j.succeed(k)
    assert j.state(k) is None


# --- F4 (BLUF path): same redaction as Ask
def test_bluf_payload_redacted():
    home = os.path.expanduser("~")
    item = {"title": "Deploy", "ask": f"failed in {home}/w/app: token=abcdef123456", "owner": "o"}
    comments = [{"author": "a", "body": "Authorization: Bearer ghp_" + "c" * 36}]
    content = needs.bluf_messages(item, comments)[1]["content"]
    assert home not in content and "abcdef123456" not in content and "ghp_" not in content
    assert "Deploy" in content and "failed in" in content
