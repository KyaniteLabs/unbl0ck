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


def _round9_payloads(body):
    card = {"title": "Review deployment", "body": body}
    comments = [{"body": body}]
    return (
        json.dumps(needs.build_ask_messages(card, []), ensure_ascii=False),
        json.dumps(needs.bluf_messages(card, comments), ensure_ascii=False),
    )


def test_round9_camel_glued_suffix_json_and_flag_secret_keys_are_redacted():
    body = (
        "accessToken: R9_ACCESS_TOKEN_SECRET\n"
        "clientSecret=R9_CLIENT_SECRET_SECRET\n"
        "dbPassword: R9_DB_PASSWORD_SECRET\n"
        "authToken=R9_AUTH_TOKEN_SECRET\n"
        "mysecret=R9_GLUED_SECRET\n"
        "dbpassword=R9_GLUED_PASSWORD_SECRET\n"
        "SECRET_KEY: R9_SECRET_KEY_SECRET\n"
        "SECRET_KEY_BASE=R9_SECRET_KEY_BASE_SECRET\n"
        "password1: R9_PASSWORD1_SECRET\n"
        "token_value: R9_TOKEN_VALUE_SECRET\n"
        "password_plain=R9_PASSWORD_PLAIN_SECRET\n"
        "--token=R9_FLAG_TOKEN_SECRET\n"
        "--password=R9_FLAG_PASSWORD_SECRET\n"
        "--password R9_FLAG_PASSWORD_SPACE_SECRET\n"
        "--api-key=R9_FLAG_API_KEY_SECRET\n"
        "--client-secret=R9_FLAG_CLIENT_SECRET\n"
        "PGPASSWORD=R9_PG_PASSWORD_SECRET psql\n"
        '--password={"nested":{"sid":"R9_NESTED_FLAG_SECRET"}}\n'
        '{"accessToken":"R9_JSON_ACCESS_TOKEN_SECRET", '
        '"secretKey":"R9_JSON_SECRET_KEY_SECRET", '
        '"client_secret_key":"R9_JSON_CLIENT_SECRET_KEY_SECRET"}'
    )
    secrets = (
        "R9_ACCESS_TOKEN_SECRET",
        "R9_CLIENT_SECRET_SECRET",
        "R9_DB_PASSWORD_SECRET",
        "R9_AUTH_TOKEN_SECRET",
        "R9_GLUED_SECRET",
        "R9_GLUED_PASSWORD_SECRET",
        "R9_SECRET_KEY_SECRET",
        "R9_SECRET_KEY_BASE_SECRET",
        "R9_PASSWORD1_SECRET",
        "R9_TOKEN_VALUE_SECRET",
        "R9_PASSWORD_PLAIN_SECRET",
        "R9_FLAG_TOKEN_SECRET",
        "R9_FLAG_PASSWORD_SECRET",
        "R9_FLAG_PASSWORD_SPACE_SECRET",
        "R9_FLAG_API_KEY_SECRET",
        "R9_FLAG_CLIENT_SECRET",
        "R9_PG_PASSWORD_SECRET",
        "R9_NESTED_FLAG_SECRET",
        "R9_JSON_ACCESS_TOKEN_SECRET",
        "R9_JSON_SECRET_KEY_SECRET",
        "R9_JSON_CLIENT_SECRET_KEY_SECRET",
    )
    for payload in _round9_payloads(body):
        for secret in secrets:
            assert secret not in payload, secret


def test_round9_authorization_schemes_and_short_values_are_redacted():
    schemes = (
        "Splunk R9_SPLUNK_SECRET",
        "JWT R9_JWT_SECRET",
        "Api-Key R9_API_KEY_SECRET",
        "OAuth R9_OAUTH_SECRET",
        "SharedKey acct:R9_SHARED_KEY_SECRET",
        "AWS AKID:R9_AWS_SECRET",
        "PVEAPIToken=u@pam!t=R9_PVE_SECRET",
        "Bearer: R9_BEARER_SECRET",
        "R9_SHORT_RAW_SECRET",
    )
    body = "\n".join(f"Authorization: {value}" for value in schemes)
    body += (
        "\nAuthorization: pending legal review"
        "\nAuthorization: Ada must approve the spend before Friday"
        "\nAuthorization: none"
        "\nauthorization: required from Ada"
    )
    for payload in _round9_payloads(body):
        for value in schemes:
            secret = value.rsplit(" ", 1)[-1].split("=")[-1].split(":")[-1]
            assert secret not in payload, secret
        assert "Authorization: pending legal review" in payload
        assert "Authorization: Ada must approve the spend before Friday" in payload
        assert "Authorization: none" in payload
        assert "authorization: required from Ada" in payload


def test_round9_bare_auth_scheme_tokens_are_redacted_from_ask_and_bluf():
    cases = (
        ("Api-Key SYNTH_X2", "Api-Key [redacted]"),
        ("SharedKey acct:SYNTH_X4", "SharedKey [redacted]"),
        ("AWS AKID:SYNTH_X5", "AWS [redacted]"),
        ("Bearer: SYNTH_X7", "Bearer: [redacted]"),
        ("OAuth SYNTH_X9", "OAuth [redacted]"),
        ("JWT SYNTH_Y1", "JWT [redacted]"),
        ("Key SYNTH_KEY_SECRET", "Key [redacted]"),
        ("Key: SYNTH_KEY_COLON_SECRET", "Key: [redacted]"),
        ("Bearer S", "Bearer [redacted]"),
    )
    body = "\n".join(line for line, _ in cases)

    for payload in _round9_payloads(body):
        for line, redacted_line in cases:
            assert line not in payload
            assert redacted_line in payload


def test_round9_bare_auth_scheme_prose_remains_readable_in_ask_and_bluf():
    prose = (
        "Bearer bones\n"
        "Key point\n"
        "OAuth is an authorization framework\n"
        "JWT tokens are short-lived"
    )

    for payload in _round9_payloads(prose):
        for line in prose.splitlines():
            assert line in payload


def test_round9_existing_short_credential_forms_remain_redacted():
    cases = (
        ("Authorization: Splunk S", "Authorization: [redacted]"),
        ("Api-Key: S", "Api-Key: [redacted]"),
        ("X-Api-Key: S", "X-Api-Key: [redacted]"),
        ("PVEAPIToken=u@pam!t=S", "PVEAPIToken=[redacted]"),
        ("PGPASSWORD=S", "PGPASSWORD=[redacted]"),
    )
    body = "\n".join(line for line, _ in cases)

    for payload in _round9_payloads(body):
        for line, redacted_line in cases:
            assert line not in payload
            assert redacted_line in payload


def test_round9_balanced_credentials_redaction_keeps_following_questions():
    body = (
        'credentials: {"primary":{"user":"bob"}, '
        '"secondary":{"sid":"R9_NESTED_CREDENTIAL_SECRET"}}\n'
        "credentials = {'a': {'b': 1}, 'k': 'R9_SINGLE_QUOTE_SECRET'}\n"
        'log payload={\\"token\\": \\"R9_ESCAPED_JSON_SECRET\\"} KEEPME\n'
        "Question: should we keep this question?"
    )
    for payload in _round9_payloads(body):
        assert "R9_NESTED_CREDENTIAL_SECRET" not in payload
        assert "R9_SINGLE_QUOTE_SECRET" not in payload
        assert "R9_ESCAPED_JSON_SECRET" not in payload
        assert "KEEPME" in payload
        assert "Question: should we keep this question?" in payload


def test_round9_yaml_block_scalar_headers_blank_lines_and_sequence_items_are_redacted():
    body = (
        "password: | # from the vault\n  R9_YAML_COMMENT_SECRET\n\n"
        "password: |2\n  R9_YAML_INDENT_SECRET\n"
        "password: |\n  first\n\n  R9_YAML_BLANK_SECRET\n"
        "- password: |\n    R9_YAML_SEQUENCE_SECRET\n"
        "tokens:\n  - R9_YAML_TOKEN_LIST_SECRET\n  - other\n"
        "secrets:\n  api: R9_YAML_SECRET_MAP_SECRET\n"
        'tokens: [\n  "R9_YAML_FLOW_LIST_SECRET"\n]\n'
        "Question: keep this YAML question?"
    )
    for payload in _round9_payloads(body):
        for secret in (
            "R9_YAML_COMMENT_SECRET",
            "R9_YAML_INDENT_SECRET",
            "R9_YAML_BLANK_SECRET",
            "R9_YAML_SEQUENCE_SECRET",
            "R9_YAML_TOKEN_LIST_SECRET",
            "R9_YAML_SECRET_MAP_SECRET",
            "R9_YAML_FLOW_LIST_SECRET",
        ):
            assert secret not in payload, secret
        assert "Question: keep this YAML question?" in payload


def test_round9_json_scan_budget_keeps_checklists_links_logs_and_questions():
    bodies = (
        ("-[x]" * 140) + " token:TOKEN12345 Question: kept?",
        ("[]()" * 130) + " token:TOKEN12345 Question: kept?",
        ("[I]{0}" * 70) + " token:TOKEN12345 Question: kept?",
    )
    for body in bodies:
        for payload in _round9_payloads(body):
            assert "TOKEN12345" not in payload
            assert "Question: kept?" in payload
            assert body[:3] in payload


def test_round9_cookie_values_redact_without_equals_but_keep_prose():
    body = (
        "cookies: R9_COOKIE_SECRET\n"
        "Cookies: session-R9_SESSION_COOKIE_SECRET\n"
        "cookies: sid:R9_SID_COOKIE_SECRET\n"
        "Cookies: the banner needs review\n"
        "cookies: banner copy pending KEEPME"
    )
    for payload in _round9_payloads(body):
        for secret in (
            "R9_COOKIE_SECRET",
            "R9_SESSION_COOKIE_SECRET",
            "R9_SID_COOKIE_SECRET",
        ):
            assert secret not in payload
        assert "Cookies: the banner needs review" in payload
        assert "cookies: banner copy pending KEEPME" in payload


def test_round9_secret_labels_preserve_cookie_and_authorization_prose():
    body = (
        "Cookie: banner copy needs legal approval\n"
        "Pending authorization: legal review of the vendor contract\n"
        "Need authorization: may I spend $200 on the API?\n"
        "auth_method: oauth2\n"
        "Token: the design token names changed\n"
        "Secret: none of this is urgent"
    )
    for payload in _round9_payloads(body):
        assert "Cookie: banner copy needs legal approval" in payload
        assert "Pending authorization: legal review of the vendor contract" in payload
        assert "Need authorization: may I spend $200 on the API?" in payload
        assert "auth_method: oauth2" in payload
        assert "Token: the design token names changed" in payload
        assert "Secret: none of this is urgent" in payload


def test_round9_mysql_password_flags_cover_case_and_client_variants():
    body = (
        "MYSQL -pR9_MYSQL_SECRET db\n"
        "mysqldump -pR9_DUMP_SECRET db\n"
        "mariadb -p R9_MARIADB_SECRET db\n"
        "Docker login -p R9_DOCKER_SECRET\n"
        "mysql -P 3306 db"
    )
    for payload in _round9_payloads(body):
        for secret in (
            "R9_MYSQL_SECRET",
            "R9_DUMP_SECRET",
            "R9_MARIADB_SECRET",
            "R9_DOCKER_SECRET",
        ):
            assert secret not in payload
        assert "mysql -P 3306 db" in payload


def test_round9_pass_and_pw_require_credential_shaped_values():
    body = (
        "pass: 12 fail: 0\n"
        "tests pass: yes\n"
        "pw: n/a\n"
        "pass: R9_PASS_CREDENTIAL_SECRET\n"
        "pw: R9_PW_CREDENTIAL_SECRET"
    )
    for payload in _round9_payloads(body):
        assert "pass: 12 fail: 0" in payload
        assert "tests pass: yes" in payload
        assert "pw: n/a" in payload
        assert "R9_PASS_CREDENTIAL_SECRET" not in payload
        assert "R9_PW_CREDENTIAL_SECRET" not in payload


def test_round9_secret_name_value_layouts_and_token_prefixes_are_redacted():
    body = (
        "  - name: SECRET_KEY\n    valueFrom: secretKeyRef\n    value: R9_NAME_VALUE_SECRET\n"
        "  value: R9_VALUE_BEFORE_NAME_SECRET\n  name: DB_PASSWORD\n"
        "{name: DB_PASSWORD, value: R9_FLOW_VALUE_SECRET}\n"
        "Name: API_TOKEN\nvalueFrom: secretKeyRef\nValue: R9_CAPITAL_VALUE_SECRET\n"
        "Name: API_TOKEN\nValue: |2\n  R9_NAMED_BLOCK_SECRET\n"
        '{"Value":"R9_JSON_VALUE_BEFORE_SECRET", "Name":"DB_PASSWORD"}\n'
        '{"key":"DB_PASSWORD", "value":"R9_JSON_KEY_VALUE_SECRET"}\n'
        '{"name":"DB_PASSWORD", "val":"R9_JSON_VAL_SECRET"}\n'
        '{"name":"DB_PASSWORD", "default":"R9_JSON_DEFAULT_SECRET"}\n'
        "hf_R9HF12345678\nSG.R9SG12345678\n"
        "SG.R9SGDOT12345678.R9SGSECRET12345678\n"
        "dop_v1_R9DOP12345678\nwhsec_R9WHSEC12345678"
    )
    secrets = (
        "R9_NAME_VALUE_SECRET",
        "R9_VALUE_BEFORE_NAME_SECRET",
        "R9_FLOW_VALUE_SECRET",
        "R9_CAPITAL_VALUE_SECRET",
        "R9_NAMED_BLOCK_SECRET",
        "R9_JSON_VALUE_BEFORE_SECRET",
        "R9_JSON_KEY_VALUE_SECRET",
        "R9_JSON_VAL_SECRET",
        "R9_JSON_DEFAULT_SECRET",
        "hf_R9HF12345678",
        "SG.R9SG12345678",
        "R9SGDOT12345678",
        "R9SGSECRET12345678",
        "dop_v1_R9DOP12345678",
        "whsec_R9WHSEC12345678",
    )
    for payload in _round9_payloads(body):
        for secret in secrets:
            assert secret not in payload, secret

    safe = needs._ask_card_view(
        {"Name": "DB_PASSWORD", "Value": "R9_DIRECT_CAPITAL_SECRET"}
    )
    assert safe["Value"] == "[redacted]"


def test_round10_short_pass_values_and_strongest_secret_key_are_redacted():
    body = """DB_PASS=hunter2
ADMIN_PASS=
pass:
pw=
wifi_pass:
SMTP_PASS: 'hunter2'
SMTP_PASS: 'correct horse battery'
pass: p@ss!
bypass_token: S
passport_secret: S
compass_api_key: S
passenger_password: S
passthrough_token: S
sso_bypass_secret: S
encompass_token: S
upw_token: S
cpw_secret: S"""
    for payload in _round9_payloads(body):
        for secret in (
            "hunter2",
            "correct horse battery",
            "p@ss!",
        ):
            assert secret not in payload
        assert "[redacted]" in payload
        for key in (
            "bypass_token",
            "passport_secret",
            "compass_api_key",
            "passenger_password",
            "passthrough_token",
            "sso_bypass_secret",
            "encompass_token",
            "upw_token",
            "cpw_secret",
        ):
            assert f"{key}: S" not in payload

    safe = needs._ask_card_view(
        {
            "env": {"DB_PASS": "hunter2"},
            "config": {"bypass_token": "S"},
            "body": "DB_PASS=hunter2",
        }
    )
    assert "hunter2" not in json.dumps(safe)
    assert safe["config"]["bypass_token"] == "[redacted]"


def test_round10_structured_next_line_and_explicit_yaml_values_are_redacted():
    secrets = (
        "R10_DEPLOY_TOKEN_SENTINEL",
        "R10_COMMENT_PASSWORD_SENTINEL",
        "R10_OAUTH_SENTINEL",
        "R10_PASS_SENTINEL",
        "R10_PW_SENTINEL",
        "R10_PASSWORD_SENTINEL",
        "R10_NAMED_FLOW_SENTINEL",
        "R10_YAML_BLOCK_SENTINEL",
        "R10_EXPLICIT_KEY_SENTINEL",
        "R10_MISINDENTED_VALUE_SENTINEL",
        "R10_COOKIE_SENTINEL",
    )
    body = (
        "Here is the deploy token:\nR10_DEPLOY_TOKEN_SENTINEL\n"
        "oauth: {access_token: 'R10_OAUTH_SENTINEL'}\n"
        "pass: R10_PASS_SENTINEL extra\n"
        'pass: "a b R10_PW_SENTINEL"\n'
        '{"pw": {"v": "R10_PW_SENTINEL"}}\n'
        "password: {a: 'it's', b: R10_PASSWORD_SENTINEL}\n"
        "- name: DB_PASSWORD\n  value: {key: R10_NAMED_FLOW_SENTINEL}\n"
        "password: |\n    first\n  R10_YAML_BLOCK_SENTINEL\n"
        "password: |2\n    first\n  R10_EXPLICIT_KEY_SENTINEL\n"
        "? db_password\n: R10_EXPLICIT_KEY_SENTINEL\n"
        "- name: DB_PASSWORD\n value: R10_MISINDENTED_VALUE_SENTINEL\n"
        "Cookie: the session value is R10_COOKIE_SENTINEL\n"
        'password: {a: 1}, "b": "R10_NAMED_FLOW_SENTINEL"\n'
        "Question: should we rotate these credentials? KEEP_THIS_QUESTION"
    )
    comments = [{"body": "password =\n  R10_COMMENT_PASSWORD_SENTINEL"}]
    payloads = (
        json.dumps(
            needs.build_ask_messages({"title": "Review", "ask": body}, []),
            ensure_ascii=False,
        ),
        json.dumps(
            needs.bluf_messages({"title": "Review", "ask": body}, comments),
            ensure_ascii=False,
        ),
    )
    for payload in payloads:
        for secret in secrets:
            assert secret not in payload, secret
        assert "KEEP_THIS_QUESTION" in payload


def test_round10_authorization_headers_and_nested_oauth_values_are_redacted():
    body = (
        "The request used Authorization: Splunk R10_AUTH_SPLUNK_SENTINEL\n"
        "> Authorization: Bearer R10_AUTH_BULLET_SENTINEL\n"
        "| Authorization: R10_AUTH_TABLE_SENTINEL |\n"
        "`Authorization: R10_AUTH_BACKTICK_SENTINEL`\n"
        '"Authorization: Basic R10_AUTH_QUOTED_SENTINEL"\n'
        "Authorization:\n  R10_AUTH_NEXT_LINE_SENTINEL\n"
        "Authorization: the token is R10_AUTH_PROSE_SENTINEL\n"
        "Authorization: failed token R10_AUTH_FAILED_SENTINEL\n"
        "Authorization: failed with R10_AUTH_FAILED_WITH_SENTINEL\n"
        "oauth=R10_OAUTH_ASSIGNMENT_SENTINEL\n"
        '{"oauth": "R10_OAUTH_JSON_SENTINEL"}\n'
        'oauth: {access_token: "R10_OAUTH_CONTAINER_SENTINEL"}\n'
        'password: {"a": 1}, "b": "R10_PASSWORD_SIBLING_SENTINEL"\n'
        "Authorization: granted token to Ada on Monday\n"
        "Authorization: tokens are redacted again\n"
        "Authorization: TBD\n"
        "Question: keep this question? KEEP_AUTH_QUESTION"
    )
    secrets = (
        "R10_AUTH_SPLUNK_SENTINEL",
        "R10_AUTH_BULLET_SENTINEL",
        "R10_AUTH_TABLE_SENTINEL",
        "R10_AUTH_BACKTICK_SENTINEL",
        "R10_AUTH_QUOTED_SENTINEL",
        "R10_AUTH_NEXT_LINE_SENTINEL",
        "R10_AUTH_PROSE_SENTINEL",
        "R10_AUTH_FAILED_SENTINEL",
        "R10_AUTH_FAILED_WITH_SENTINEL",
        "R10_OAUTH_ASSIGNMENT_SENTINEL",
        "R10_OAUTH_JSON_SENTINEL",
        "R10_OAUTH_CONTAINER_SENTINEL",
        "R10_PASSWORD_SIBLING_SENTINEL",
    )
    payloads = (
        json.dumps(
            needs.build_ask_messages({"title": "Review", "ask": body}, []),
            ensure_ascii=False,
        ),
        json.dumps(
            needs.bluf_messages({"title": "Review", "ask": body}, [{"body": body}]),
            ensure_ascii=False,
        ),
    )
    for payload in payloads:
        for secret in secrets:
            assert secret not in payload, secret
        assert "Authorization: granted token to Ada on Monday" in payload
        assert "Authorization: tokens are redacted again" in payload
        assert "Authorization: TBD" in payload
        assert "KEEP_AUTH_QUESTION" in payload


def test_round10_credential_shape_keeps_ordinary_scheme_and_key_prose():
    ordinary = (
        "Key takeaways from the meeting\n"
        "AWS migration needs CEO approval\n"
        "OAuth 2.0 flow\n"
        "JWT expiry: 15 minutes\n"
        "token_count: 5\n"
        "tokenization: bpe\n"
        "bypass_cache: true\n"
        "passenger: waiting\n"
        "passport: issued\n"
        "secretary: Jane\n"
        "Co-authored-by: Ada\n"
        "auth_provider: okta\n"
        "cookie_banner: enabled\n"
        "token_ttl: 3600\n"
        "private_key_path: src/keys/private-key-v2.pem\n"
        "--token-ttl 3600\n"
        "--auth-mode basic\n"
        "AWS us-east-1 is down\n"
        "Key 1: choose option A\n"
        "JWT 2 hours\n"
        "Bearer 3 notes\n"
        "Authorization: granted token to Ada on Monday\n"
        "Authorization: tokens are redacted again"
    )
    safe = needs._ask_redact(ordinary)
    assert safe == ordinary

    credential = "Bearer R10_BARE_SCHEME_SECRET_0123"
    redacted = needs._ask_redact(credential)
    assert credential not in redacted
    assert "Bearer [redacted]" in redacted


def test_round10_remaining_common_credential_forms_are_redacted():
    body = """password => R10_SQL_ASSIGNMENT_SENTINEL
API key: R10_API_KEY_SENTINEL
-p R10_SHORT_FLAG_SENTINEL
IDENTIFIED BY 'R10_SQL_PASSWORD_SENTINEL'
redis://:R10_REDIS_PASSWORD_SENTINEL@cache.example.test
keytool -storepass R10_STOREPASS_SENTINEL
:_authToken R10_AUTH_TOKEN_SENTINEL
auth=("bob","R10_BASIC_TUPLE_SENTINEL")
HTTPBasicAuth("bob", "R10_HTTP_BASIC_SENTINEL")"""
    secrets = (
        "R10_SQL_ASSIGNMENT_SENTINEL",
        "R10_API_KEY_SENTINEL",
        "R10_SHORT_FLAG_SENTINEL",
        "R10_SQL_PASSWORD_SENTINEL",
        "R10_REDIS_PASSWORD_SENTINEL",
        "R10_STOREPASS_SENTINEL",
        "R10_AUTH_TOKEN_SENTINEL",
        "R10_BASIC_TUPLE_SENTINEL",
        "R10_HTTP_BASIC_SENTINEL",
    )
    safe = needs._ask_redact(body)
    for secret in secrets:
        assert secret not in safe, secret
    assert needs._ask_redact('auth=("bob","S")') == "auth=[redacted]"
    assert needs._ask_redact('HTTPBasicAuth("bob", "S")') == (
        'HTTPBasicAuth("bob", [redacted])'
    )
    assert needs._ask_redact("IDENTIFIED BY 'S'") == "IDENTIFIED BY [redacted]"


def test_round10_short_values_are_redacted_across_structured_forms():
    cases = {
        "pass: S extra": "pass: [redacted]",
        'pass: "a b S"': 'pass: "[redacted]"',
        '{"pw": {"v": "S"}}': '{"pw": "[redacted]"}',
        "password: {a: 'it's', b: S}": "password: [redacted]",
        "- name: DB_PASSWORD\n  value: {key: S}": (
            "- name: DB_PASSWORD\n  value: [redacted]"
        ),
        "Here is the deploy token:\nS": "Here is the deploy token:\n[redacted]",
        "password =\n  S": "password =\n  [redacted]",
        "Cookie: the session value is S": "Cookie: [redacted]",
        "The request used Authorization: Splunk S": (
            "The request used Authorization: [redacted]"
        ),
        "Authorization: the token is S": "Authorization: [redacted]",
        "Authorization: failed token S": "Authorization: [redacted]",
        'password: {"a": 1}, "b": "S"': "password: [redacted]",
        "oauth=S": "oauth=[redacted]",
        '{"oauth": "S"}': '{"oauth": "[redacted]"}',
        "password => S": "password => [redacted]",
        "API key: S": "API key: [redacted]",
        "-p S": "-p [redacted]",
        "IDENTIFIED BY 'S'": "IDENTIFIED BY [redacted]",
        "redis://:S@host": "redis://[redacted]@host",
        "keytool -storepass S": "keytool -storepass [redacted]",
        ":_authToken S": ":_authToken [redacted]",
        'auth=("bob","S")': "auth=[redacted]",
        'HTTPBasicAuth("bob", "S")': 'HTTPBasicAuth("bob", [redacted])',
    }
    for source, expected in cases.items():
        assert needs._ask_redact(source) == expected, source
