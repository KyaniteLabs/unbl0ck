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


def test_bluf_payload_redacts_non_bearer_authorization_headers():
    item = {"title": "Deploy", "ask": "Authorization: ApiKey synthetic-api-key-abcdef0123456789 "
            "authorization=synthetic-auth-key-abcdef0123456789"}
    comments = [
        {"body": 'Authorization: Digest username="synthetic", nonce="synthetic-nonce-123456789"'},
        {"body": "Proxy-Authorization: ApiKey synthetic-proxy-key-abcdef0123456789"},
    ]
    content = needs.bluf_messages(item, comments)[1]["content"]
    for secret in ("synthetic-api-key-abcdef0123456789", "synthetic-nonce-123456789",
                   "synthetic-proxy-key-abcdef0123456789", "synthetic-auth-key-abcdef0123456789"):
        assert secret not in content


# --- F26: the plugin no longer calls kanban_db's private slug normalizer

SLUG_INPUTS = ["demo", " Demo ", "UPPER", "a", "0a", "a-b_c", "-lead", "_lead", "", "   ", None, "a" * 64,
               "a" * 65, "has space", "../x", "a/b", "caf\u00e9", "x\n", "a.b"]


def _outcome(fn, v):
    try:
        return ("ok", fn(v))
    except ValueError as exc:
        return ("err", str(exc))


def test_plugin_api_does_not_use_the_private_slug_normalizer():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py").read_text()
    assert "_normalize_board_slug" not in src


def test_vendored_slug_normalizer_rules():
    assert needs.normalize_board_slug(" Demo ") == "demo"
    assert needs.normalize_board_slug(None) is None and needs.normalize_board_slug("  ") is None
    for bad in ("-x", "_x", "a/b", "../x", "a" * 65, "has space", "x\n.y"):
        with pytest.raises(ValueError):
            needs.normalize_board_slug(bad)


def test_vendored_slug_normalizer_matches_hermes_when_importable():
    pytest.importorskip("hermes_cli.kanban_db")
    from hermes_cli import kanban_db
    for v in SLUG_INPUTS:
        assert _outcome(needs.normalize_board_slug, v) == _outcome(kanban_db._normalize_board_slug, v), v


# --- F31: profile.yaml is read once per change, not once per poll

def _profile(home, name, title):
    f = home / "profiles" / name / "profile.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"ui_meta:\n  hermes-bots:\n    title: {title}\n")
    return f


def test_bot_name_is_cached_until_the_file_changes(tmp_path, monkeypatch):
    needs._BOT_NAME_CACHE.clear()
    f = _profile(tmp_path, "ada", "Ada")
    other = _profile(tmp_path, "bo", "Bo")
    reads = []
    real = type(f).read_text
    monkeypatch.setattr(type(f), "read_text", lambda self, *a, **k: (reads.append(self.parent.name), real(self, *a, **k))[1])

    assert needs._bot_name(tmp_path, "ada") == "Ada"
    assert needs._bot_name(tmp_path, "bo") == "Bo"
    assert sorted(reads) == ["ada", "bo"]
    for _ in range(5):                                   # five more polls: nothing is re-read
        assert needs._bot_name(tmp_path, "ada") == "Ada" and needs._bot_name(tmp_path, "bo") == "Bo"
    assert sorted(reads) == ["ada", "bo"]

    _profile(tmp_path, "ada", "Ada Prime")                # touched: only that profile is re-read
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    assert needs._bot_name(tmp_path, "ada") == "Ada Prime"
    assert needs._bot_name(tmp_path, "bo") == "Bo"
    assert sorted(reads) == ["ada", "ada", "bo"]


def test_bot_name_falls_back_when_the_file_is_missing_or_removed(tmp_path):
    needs._BOT_NAME_CACHE.clear()
    assert needs._bot_name(tmp_path, "ghost") == "ghost" and needs._bot_name(tmp_path, "default") == "Your agent"
    f = _profile(tmp_path, "ada", "Ada")
    assert needs._bot_name(tmp_path, "ada") == "Ada"
    f.unlink()
    assert needs._bot_name(tmp_path, "ada") == "ada"
