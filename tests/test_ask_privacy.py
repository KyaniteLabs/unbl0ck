"""F4: the Ask payload sent to the LLM must not carry the workspace path, the operator's
home directory, or secret-looking strings from run/failure errors, comments or events."""

import json
import os
import time

import needs

HOME = os.path.expanduser("~")
SENTINEL_KEY = "sk-" + "A" * 40
GH = "ghp_" + "b" * 36


def _card():
    return {
        "id": "kanban:b:t1",
        "title": "Deploy",
        "body": "please ship",
        "last_failure_error": f"Traceback in {HOME}/work/app/run.py: auth failed token={SENTINEL_KEY}",
        "workspace": {
            "kind": "worktree",
            "path": f"{HOME}/work/app",
            "branch": "feat/x",
            "project_id": "p1",
            "completion_contract": None,
        },
        "comments": [
            {
                "id": 1,
                "author": "a",
                "body": f"use Authorization: Bearer {GH}",
                "created_at": 1,
            }
        ],
        "events": [
            {
                "id": 2,
                "kind": "x",
                "payload": {"cmd": "git push https://user:hunter2@example.com/r.git"},
            }
        ],
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
        {
            "body": 'Authorization: Digest username="synthetic", nonce="synthetic-nonce-123456789"'
        },
        {"body": "Proxy-Authorization: ApiKey synthetic-proxy-key-abcdef0123456789"},
        {"body": "authorization=synthetic-auth-key-abcdef0123456789"},
    ]
    payload = json.dumps(needs.build_ask_messages(card, []), ensure_ascii=False)
    for secret in (
        "synthetic-api-key-abcdef0123456789",
        "synthetic-nonce-123456789",
        "synthetic-proxy-key-abcdef0123456789",
        "synthetic-auth-key-abcdef0123456789",
    ):
        assert secret not in payload


def test_json_secret_keys_and_quoted_values_redacted_from_ask_and_bluf():
    embedded = (
        '{"Authorization": "synthetic-json-auth-value-0001", '
        '"api_key": "synthetic-json-api-key-value-0002", '
        '"password": "synthetic quoted password value with spaces", '
        '"client_secret": "synthetic-json-client-secret-0005", '
        '"token": "synthetic-json-token-0006"} '
        "{'client_secret': 'synthetic-python-client-secret-0007'}"
    )
    card = {
        "title": "Review the deployment",
        "ask": embedded,
        "events": [
            {
                "payload": {
                    "Authorization": "synthetic-event-auth-value-0003",
                    "details": {"api_key": "synthetic-event-api-key-value-0004"},
                }
            }
        ],
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
        "synthetic-json-client-secret-0005",
        "synthetic-json-token-0006",
        "synthetic-python-client-secret-0007",
    )
    for label, payload in payloads.items():
        for secret in secrets:
            assert secret not in payload, f"{label} leaked {secret}"
    assert "synthetic-event-auth-value-0003" not in payloads["Ask"]
    assert "synthetic-event-api-key-value-0004" not in payloads["Ask"]


def test_auth_credential_keys_redacted_without_redacting_person_fields():
    card = {
        "author": "Ada",
        "authors": ["Ada", "Grace"],
        "authored": "by Ada",
        "authority": "owner",
        "auth": "synthetic-auth-value-0201",
        "auth_token": "synthetic-auth-token-0202",
        "authentication": "synthetic-authentication-value-0203",
        "authorization": "synthetic-authorization-value-0204",
    }

    safe = needs._ask_card_view(card)

    for key in ("author", "authors", "authored", "authority"):
        assert safe[key] == card[key]
    for key in ("auth", "auth_token", "authentication", "authorization"):
        assert safe[key] == "[redacted]"


def test_auth_key_detection_keeps_author_ids_modes_and_oauth_scopes():
    card = {
        "author_id": "person-123",
        "auth_mode": "oauth",
        "oauth_scopes": ["read:tasks", "write:comments"],
    }

    safe = needs._ask_card_view(card)

    assert safe == card


def test_cookie_shell_credentials_and_password_phrases_redacted_from_ask_and_bluf():
    sensitive = (
        "  Cookie: sessionid=synthetic-cookie-value-0001\n"
        "curl -u bob:synthetic-curl-password-0002 https://example.test/\n"
        "curl -H 'Authorization: ApiKey synthetic-curl-header-auth-0004'\n"
        "Use Authorization: ApiKey synthetic-inline-header-auth-0005\n"
        "Authorization: Digest username=bob, nonce=synthetic-digest-nonce-0008, "
        "response=synthetic-digest-response-0009\n"
        'curl -u "bob:synthetic curl secret phrase-0006" https://example.test/\n'
        'password is "synthetic password phrase-0007"\n'
        "password is synthetic-password-0003"
    )
    card = {"title": "Review deployment", "ask": sensitive}
    payloads = (
        json.dumps(needs.build_ask_messages(card, []), ensure_ascii=False),
        json.dumps(needs.bluf_messages(card), ensure_ascii=False),
    )
    for payload in payloads:
        for secret in (
            "synthetic-cookie-value-0001",
            "synthetic-curl-password-0002",
            "synthetic-password-0003",
            "synthetic-curl-header-auth-0004",
            "synthetic-inline-header-auth-0005",
            "synthetic-digest-nonce-0008",
            "synthetic-digest-response-0009",
            "synthetic curl secret phrase-0006",
            "synthetic password phrase-0007",
        ):
            assert secret not in payload


def test_ordinary_response_objects_are_not_secret_redacted():
    card = _card()
    card["events"] = [
        {"payload": {"response": {"status": 403, "body": "CEO approval missing"}}}
    ]
    payload = needs.build_ask_messages(card, [])[1]["content"]
    assert '"status": 403' in payload
    assert "CEO approval missing" in payload


def test_authorization_prose_survives_redaction_in_ask_and_bluf():
    prose = (
        "Authorization: header missing in request; deploy broke at step 3.\n"
        "The authorization: flow is broken; need CEO approval for the 12k spend."
    )
    card = {"title": "Deploy", "ask": prose}
    payloads = (
        json.dumps(needs.build_ask_messages(card, []), ensure_ascii=False),
        json.dumps(needs.bluf_messages(card), ensure_ascii=False),
    )
    for payload in payloads:
        assert "authorization" in payload.lower()
        assert "header missing in request" in payload
        assert "deploy broke at step 3" in payload
        assert "flow is broken" in payload
        assert "CEO approval" in payload


def test_authorization_prose_and_following_question_survive_redaction():
    prose = (
        "Authorization: needs CEO sign-off before deploy. Should we ship today?\n"
        "Awaiting authorization: the reviewer needs to sign the DPA before Friday.\n"
        "OAuth: the redirect needs review. Cookies: the banner needs review."
    )
    payloads = (
        json.dumps(
            needs.build_ask_messages({"title": "Deploy", "ask": prose}, []),
            ensure_ascii=False,
        ),
        json.dumps(
            needs.bluf_messages({"title": "Deploy", "ask": prose}), ensure_ascii=False
        ),
    )
    for payload in payloads:
        assert "needs CEO sign-off before deploy" in payload
        assert "Should we ship today?" in payload
        assert "the reviewer needs to sign the DPA before Friday" in payload
        assert "OAuth: the redirect needs review" in payload
        assert "Cookies: the banner needs review" in payload


def test_authorization_headers_with_any_scheme_and_nested_secret_values_are_redacted():
    sensitive = (
        'curl -H "Authorization: synthetic-no-scheme-auth-0101"\n'
        "curl -H 'Authorization: Bot synthetic-bot-auth-0102'\n"
        "Authorization: SSWS synthetic-ssws-auth-0103\n"
        "Authorization: Negotiate synthetic-negotiate-auth-0104\n"
        "Authorization: NTLM synthetic-ntlm-auth-0105\n"
        "Authorization: AWS4-HMAC-SHA256 Credential=synthetic-aws-auth-0106, Signature=synthetic-aws-signature-0107\n"
        "authorization: synthetic-bare-auth-0108\n"
        'tokens-json={"tokens": ["synthetic-nested-token-0109"]}\n'
        'credentials-json={"credentials": {"user": "bob", "pw": "synthetic-nested-pw-0110"}}\n'
        'log payload="{\\"tokens\\": [\\"synthetic-escaped-json-token-0111\\"]}"\n'
        "private_key: |\n  synthetic-yaml-private-key-0112\n"
    )
    card = {"title": "Review", "ask": sensitive}
    payloads = (
        json.dumps(needs.build_ask_messages(card, []), ensure_ascii=False),
        json.dumps(needs.bluf_messages(card), ensure_ascii=False),
    )
    for payload in payloads:
        for secret in (
            "synthetic-no-scheme-auth-0101",
            "synthetic-bot-auth-0102",
            "synthetic-ssws-auth-0103",
            "synthetic-negotiate-auth-0104",
            "synthetic-ntlm-auth-0105",
            "synthetic-aws-auth-0106",
            "synthetic-aws-signature-0107",
            "synthetic-bare-auth-0108",
            "synthetic-nested-token-0109",
            "synthetic-nested-pw-0110",
            "synthetic-escaped-json-token-0111",
            "synthetic-yaml-private-key-0112",
        ):
            assert secret not in payload, secret


def test_structured_redaction_keeps_text_after_the_secret_value():
    body = (
        "Authorization: Bearer synthetic-header-secret-0501\n"
        "Question: should we rotate? KEEPME\n"
        "Missing credentials: {aws, gcp}. Which provider should I use? KEEPME\n"
        "The API token: [expired] so calls fail. Team, renew? KEEPME\n"
        "api_key = [REDACTED]\nThe deploy failed. Keep this sentence."
    )
    payloads = (
        json.dumps(
            needs.build_ask_messages({"title": "Review", "ask": body}, []),
            ensure_ascii=False,
        ),
        json.dumps(
            needs.bluf_messages({"title": "Review", "ask": body}), ensure_ascii=False
        ),
    )
    for payload in payloads:
        assert "synthetic-header-secret-0501" not in payload
        for context in (
            "Question: should we rotate? KEEPME",
            "Which provider should I use? KEEPME",
            "Team, renew? KEEPME",
            "The deploy failed. Keep this sentence.",
        ):
            assert context in payload


def test_yaml_block_scalars_under_secret_keys_are_redacted_in_ask_and_bluf():
    body = (
        "password: |\n  R8_PASSWORD_BLOCK_SECRET\n"
        "api_token: >-\n  R8_API_TOKEN_BLOCK_SECRET\n"
        "client_secret: |\n  R8_CLIENT_SECRET_BLOCK_SECRET\n"
        "private_key: >\n  R8_PRIVATE_KEY_BLOCK_SECRET\n"
        "Should we rotate these credentials? Keep this question."
    )
    payloads = (
        json.dumps(
            needs.build_ask_messages({"title": "Review", "ask": body}, []),
            ensure_ascii=False,
        ),
        json.dumps(
            needs.bluf_messages({"title": "Review", "ask": body}), ensure_ascii=False
        ),
    )
    for payload in payloads:
        for secret in (
            "R8_PASSWORD_BLOCK_SECRET",
            "R8_API_TOKEN_BLOCK_SECRET",
            "R8_CLIENT_SECRET_BLOCK_SECRET",
            "R8_PRIVATE_KEY_BLOCK_SECRET",
        ):
            assert secret not in payload
        assert "Should we rotate these credentials? Keep this question." in payload


def test_named_environment_values_and_short_password_keys_are_redacted():
    body = (
        "env:\n  - name: API_TOKEN\n    value: R8_ENV_VALUE_SECRET\n"
        '{"name":"API_TOKEN","value":"R8_JSON_ENV_SECRET"}\n'
        "{'creds': {'user':'bob','pw':'R8_PW_SECRET'}}\n"
        "pass: R8_PASS_SECRET\npwd=R8_PWD_SECRET"
    )
    payloads = (
        json.dumps(
            needs.build_ask_messages({"title": "Review", "ask": body}, []),
            ensure_ascii=False,
        ),
        json.dumps(
            needs.bluf_messages({"title": "Review", "ask": body}), ensure_ascii=False
        ),
    )
    for payload in payloads:
        for secret in (
            "R8_ENV_VALUE_SECRET",
            "R8_JSON_ENV_SECRET",
            "R8_PW_SECRET",
            "R8_PASS_SECRET",
            "R8_PWD_SECRET",
        ):
            assert secret not in payload


def test_deeply_nested_json_is_redacted_without_crashing_ask_or_bluf():
    nested = "[" * 1100 + '{"token":"R8_NESTED_SECRET"}' + "]" * 1100
    ask = json.dumps(
        needs.build_ask_messages({"title": "Review", "ask": nested}, []),
        ensure_ascii=False,
    )
    bluf = json.dumps(
        needs.bluf_messages({"title": "Review", "ask": nested}), ensure_ascii=False
    )

    assert "[redacted]" in ask
    assert "[redacted]" in bluf
    assert "R8_NESTED_SECRET" not in ask
    assert "R8_NESTED_SECRET" not in bluf


def test_stripe_gitlab_google_and_npm_tokens_are_redacted():
    secrets = (
        "sk_live_" + "A" * 24,
        "rk_test_" + "B" * 24,
        "glpat-" + "C" * 24,
        "AIza" + "D" * 24,
        "npm_" + "E" * 24,
    )
    body = " ".join(secrets)
    payloads = (
        json.dumps(
            needs.build_ask_messages({"title": "Review", "ask": body}, []),
            ensure_ascii=False,
        ),
        json.dumps(
            needs.bluf_messages({"title": "Review", "ask": body}), ensure_ascii=False
        ),
    )
    for payload in payloads:
        for secret in secrets:
            assert secret not in payload, secret


def test_cookie_and_command_line_credentials_are_redacted():
    sensitive = (
        "Set-Cookie: session=synthetic-set-cookie-0201; HttpOnly\n"
        "curl -b 'session=synthetic-curl-cookie-0202' https://example.test/\n"
        'curl --cookie "session=synthetic-long-cookie-0203" https://example.test/\n'
        "curl -ubob:synthetic-glued-user-0204 https://example.test/\n"
        "curl --user=alice:synthetic-equals-user-0205 https://example.test/\n"
        "mysql -psynthetic-mysql-password-0206 database\n"
        "sshpass -p synthetic-sshpass-password-0207 ssh server\n"
        "docker login -p synthetic-docker-password-0208\n"
        'config={"auth": "synthetic-auth-value-0209", "passphrase": "synthetic-passphrase-0210"}\n'
    )
    card = {"title": "Review", "ask": sensitive}
    payloads = (
        json.dumps(needs.build_ask_messages(card, []), ensure_ascii=False),
        json.dumps(needs.bluf_messages(card), ensure_ascii=False),
    )
    for payload in payloads:
        for secret in (
            "synthetic-set-cookie-0201",
            "synthetic-curl-cookie-0202",
            "synthetic-long-cookie-0203",
            "synthetic-glued-user-0204",
            "synthetic-equals-user-0205",
            "synthetic-mysql-password-0206",
            "synthetic-sshpass-password-0207",
            "synthetic-docker-password-0208",
            "synthetic-auth-value-0209",
            "synthetic-passphrase-0210",
        ):
            assert secret not in payload, secret


def test_command_password_flags_do_not_redact_ports_or_neighboring_commands():
    safe = needs._ask_redact(
        "mysql -h db -P 3306; docker run -p 8080:80\n"
        "sshpass -p synthetic-ssh-password-0601 ssh host"
    )

    assert "mysql -h db -P 3306" in safe
    assert "docker run -p 8080:80" in safe
    assert "synthetic-ssh-password-0601" not in safe


def test_cookie_keys_in_recursive_and_embedded_mappings_are_redacted():
    sensitive = (
        '{"Cookie": "session=synthetic-json-cookie-0301"} '
        "{'Cookie': 'session=synthetic-python-cookie-0302'}"
    )
    card = {
        "title": "Review",
        "ask": sensitive,
        "events": [{"payload": {"Cookie": "session=synthetic-map-cookie-0303"}}],
    }
    payloads = (
        json.dumps(needs.build_ask_messages(card, []), ensure_ascii=False),
        json.dumps(needs.bluf_messages(card), ensure_ascii=False),
    )
    for payload in payloads:
        for secret in (
            "synthetic-json-cookie-0301",
            "synthetic-python-cookie-0302",
            "synthetic-map-cookie-0303",
        ):
            assert secret not in payload, secret


def test_large_harmless_card_body_redacts_within_budget():
    card = {"title": "Review", "body": "x" * 16000}
    started = time.perf_counter()
    needs.build_ask_messages(card, [])
    elapsed = time.perf_counter() - started
    assert elapsed < 0.05, f"16k harmless body took {elapsed:.3f}s"


def test_clipped_embedded_secret_assignments_are_redacted_before_sending():
    secret = "synthetic-clipped-nested-token-0401"
    body = '{"tokens": ["' + secret + '"], "note": "' + "x" * 25000 + '"}'
    payload = json.dumps(
        needs.build_ask_messages({"title": "Review", "body": body}, []),
        ensure_ascii=False,
    )
    assert secret not in payload


def test_useful_context_kept():
    p = _payload()
    for s in ("Deploy", "please ship", "needs a decision", "feat/x", "auth failed"):
        assert s in p, s


def test_caller_card_not_mutated():
    c = _card()
    before = json.dumps(c, sort_keys=True)
    needs.build_ask_messages(c, [])
    assert json.dumps(c, sort_keys=True) == before
