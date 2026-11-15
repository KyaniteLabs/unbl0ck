"""Round-5 findings for the Ask payload and the model-bound text cuts (stack 01)."""

import json
import unittest

from test_outside import needs
from _redaction import assert_redacted

SYNTH = "Zq81LmK4vP"
BS = chr(92)


def bluf_payload(ask):
    item = {"id": "x", "version": "v", "title": "T", "ask": ask, "owner": "o", "source": "chat"}
    messages = needs.bluf_messages(item)
    return json.dumps(messages)


class AnUnterminatedPrivateKeyIsStillAPrivateKey(unittest.TestCase):
    def test_a_pem_cut_before_its_end_marker_is_withheld(self):
        """R5-needs-002 (class F): the end marker lies beyond the model cut."""
        body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" * 4000  # well over the cut
        pem = f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----"
        self.assertGreater(len(pem), 120_000)
        payload = bluf_payload(pem + "\nShould I merge it now?")
        self.assertNotIn("MIIEvQIBADANBg", payload)

    def test_redaction_alone_withholds_an_unterminated_pem(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC"
        assert_redacted(needs._ask_redact(text), "MIIEvQ")


class NumericCredentialsUnderTokenKeys(unittest.TestCase):
    def test_numeric_auth_token_is_redacted(self):
        """R5-needs-003 (class F): token and authentication keys keep their digits private."""
        self.assertEqual(needs._ask_card_view({"auth_token": 123456})["auth_token"], "[redacted]")
        assert_redacted(needs._ask_redact("auth_token=123456"), "123456")
        for text, digits in (
            ("access_token: 998877", "998877"),
            ("refresh_token=5551212", "5551212"),
            ("bearer_token = 424242", "424242"),
        ):
            with self.subTest(text=text):
                assert_redacted(needs._ask_redact(text), digits)

    def test_counts_under_token_keys_stay_readable(self):
        for text in ("max_tokens: 4000", "token_count=5", "tokens: 12"):
            with self.subTest(text=text):
                self.assertEqual(needs._ask_redact(text), text)


class AnExhaustedJsonScanWithholdsEscapedSecrets(unittest.TestCase):
    def test_escaped_structured_secret_after_many_failed_decodes(self):
        s = "{x" * 129 + json.dumps('{"' + BS + 'u0070assword":"%s"}' % SYNTH)
        s = "{x" * 129 + json.dumps(BS + 'u0070assword' + '":"%s"}' % SYNTH).replace('{', '{', 1) if False else "{x" * 129 + json.dumps('{"' + BS + 'u0070assword":"%s"}' % SYNTH)
        assert_redacted(needs._ask_redact(s), SYNTH)

    def test_escaped_secret_in_a_plain_json_string_after_noise(self):
        s = "{x" * 300 + json.dumps({"log": '{"' + BS + 'u0070assword":"%s"}' % SYNTH})
        assert_redacted(needs._ask_redact(s), SYNTH)


class WorkspacePathIsScrubbedFromKeys(unittest.TestCase):
    def test_a_path_used_as_a_key_does_not_reach_the_model(self):
        """R5-needs-016 (class F): keys are text too."""
        view = needs._ask_card_view(
            {
                "workspace": {"path": "/srv/private/repo"},
                "events": [{"payload": {"/srv/private/repo/file": "safe"}}],
            }
        )
        out = json.dumps(view)
        self.assertNotIn("/srv/private/repo", out)
        self.assertIn("safe", out)

    def test_a_scrubbed_key_keeps_every_value(self):
        view = needs._ask_card_view(
            {
                "workspace": {"path": "/srv/private/repo"},
                "m": {"/srv/private/repo/a": 1, "[workspace]/a": 2},
            }
        )
        self.assertEqual(sorted(view["m"].values()), [1, 2])


class TheAskTestsLoadWithoutHermes(unittest.TestCase):
    def test_the_pure_payload_tests_import_when_hermes_is_absent(self):
        """R5-needs-043 (class L): the Kanban fixture defines its names on the import-failure path."""
        import subprocess
        import sys
        from pathlib import Path

        code = (
            "import sys; sys.modules['hermes_cli'] = None; "
            "import test_r4_stack01d_ask; "
            "print('ok')"
        )
        env = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(Path(__file__).parent)}
        done = subprocess.run(
            [sys.executable, "-W", "ignore", "-c", code],
            capture_output=True, text=True, env={**__import__("os").environ, **env},
        )
        self.assertEqual(done.returncode, 0, done.stderr[-600:])
        self.assertIn("ok", done.stdout)


if __name__ == "__main__":
    unittest.main()
