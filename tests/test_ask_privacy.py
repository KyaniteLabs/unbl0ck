"""F4: the Ask payload sent to the LLM must not carry the workspace path, the operator's
home directory, or secret-looking strings from run/failure errors, comments or events."""
import json
import os

import needs

HOME = os.path.expanduser("~")
SENTINEL_KEY = "sk-" + "A" * 40
GH = "ghp_" + "b" * 36


def _card():
    return {
        "id": "kanban:b:t1", "title": "Deploy", "body": "please ship",
        "last_failure_error": f"Traceback in {HOME}/work/app/run.py: auth failed token={SENTINEL_KEY}",
        "workspace": {"kind": "worktree", "path": f"{HOME}/work/app", "branch": "feat/x",
                      "project_id": "p1", "completion_contract": None},
        "comments": [{"id": 1, "author": "a", "body": f"use Authorization: Bearer {GH}", "created_at": 1}],
        "events": [{"id": 2, "kind": "x", "payload": {"cmd": "git push https://user:hunter2@example.com/r.git"}}],
        "last_run": {"summary": "ok", "error": f"password=hunter2 at {HOME}/.config/x"},
        "block": {"reason": "needs a decision"},
    }


def _payload():
    msgs = needs.build_ask_messages(_card(), [])
    return msgs[1]["content"]


def test_workspace_path_not_sent():
    p = _payload()
    assert f"{HOME}/work/app" not in p
    assert '"path"' not in p


def test_home_dir_never_sent():
    assert HOME not in _payload()


def test_secrets_redacted():
    p = _payload()
    for s in (SENTINEL_KEY, GH, "hunter2"):
        assert s not in p, s
    assert "[redacted]" in p


def test_non_bearer_authorization_headers_redacted():
    card = _card()
    card["comments"] = [
        {"body": "Authorization: ApiKey synthetic-api-key-abcdef0123456789"},
        {"body": 'Authorization: Digest username="synthetic", nonce="synthetic-nonce-123456789"'},
        {"body": "Proxy-Authorization: ApiKey synthetic-proxy-key-abcdef0123456789"},
        {"body": "authorization=synthetic-auth-key-abcdef0123456789"},
    ]
    payload = json.dumps(needs.build_ask_messages(card, []), ensure_ascii=False)
    for secret in ("synthetic-api-key-abcdef0123456789", "synthetic-nonce-123456789",
                   "synthetic-proxy-key-abcdef0123456789", "synthetic-auth-key-abcdef0123456789"):
        assert secret not in payload


def test_json_secret_keys_and_quoted_values_redacted_from_ask_and_bluf():
    embedded = (
        '{"Authorization": "synthetic-json-auth-value-0001", '
        '"api_key": "synthetic-json-api-key-value-0002", '
        '"password": "synthetic quoted password value with spaces"}'
    )
    card = {
        "title": "Review the deployment",
        "ask": embedded,
        "events": [{"payload": {
            "Authorization": "synthetic-event-auth-value-0003",
            "details": {"api_key": "synthetic-event-api-key-value-0004"},
        }}],
    }
    comments = [{"body": embedded}]
    payloads = {
        "Ask": json.dumps(needs.build_ask_messages(card, []), ensure_ascii=False),
        "BLUF": json.dumps(needs.bluf_messages(card, comments), ensure_ascii=False),
    }
    secrets = (
        "synthetic-json-auth-value-0001",
        "synthetic-json-api-key-value-0002",
        "synthetic quoted password value with spaces",
    )
    for label, payload in payloads.items():
        for secret in secrets:
            assert secret not in payload, f"{label} leaked {secret}"
    assert "synthetic-event-auth-value-0003" not in payloads["Ask"]
    assert "synthetic-event-api-key-value-0004" not in payloads["Ask"]


def test_useful_context_kept():
    p = _payload()
    for s in ("Deploy", "please ship", "needs a decision", "feat/x", "auth failed"):
        assert s in p, s


def test_caller_card_not_mutated():
    c = _card()
    before = json.dumps(c, sort_keys=True)
    needs.build_ask_messages(c, [])
    assert json.dumps(c, sort_keys=True) == before
