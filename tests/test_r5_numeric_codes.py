"""Digit-only codes under code keys (otp, pin, passcode, bearer, session id, signing key) are
redacted in key position only; the same words in prose, counts and longer names survive."""

import unittest

import needs

REDACTED = [
    "otp=123456",
    "totp=123456",
    "pin=1234",
    "PIN: 4821",
    "passcode: 98765432",
    "bearer=123456",
    "sessionid=123456",
    "session_id=123456",
    "signing_key=123456",
    "mfa.otp=654321",
    "login_pin = 2468",
    '"otp": "123456"',
    '{"pin": 1234}',
]

KEPT = [
    "pin the version",
    "bearer of bad news",
    "PIN 4 of the connector",
    "pin 1234",
    "the otp is 123456 digits?",
    "pin=3",
    "pin_count=3",
    "otp_length=6",
    "signing_key_version=2",
    "session_count=12345",
    "pinned=1",
    "spin=1234",
    "opinion=5555",
    "pinout=1234",
]


class NumericCodeRedaction(unittest.TestCase):
    def test_a_numeric_code_in_key_position_is_redacted(self):
        """Extra (round-5 sweep): digit-only codes under code keys were left in the Ask context."""
        for text in REDACTED:
            with self.subTest(text=text):
                out = needs._ask_redact(text)
                self.assertIn("[redacted]", out)
                self.assertNotRegex(out, r"\d{4,}")

    def test_the_same_words_in_prose_and_counts_survive(self):
        """Extra (round-5 sweep): the code-key match never touches prose, short counts or longer names."""
        for text in KEPT:
            with self.subTest(text=text):
                self.assertEqual(needs._ask_redact(text), text)

    def test_a_numeric_code_field_in_a_card_is_redacted(self):
        """Extra (round-5 sweep): a structured card field holding a code is redacted, a count is not."""
        view = needs._ask_card_view({"pin": 1234, "otp": "123456", "pins": 2, "pin_count": 3}, None, None, ())
        self.assertEqual(view, {"pin": "[redacted]", "otp": "[redacted]", "pins": 2, "pin_count": 3})


if __name__ == "__main__":
    unittest.main()
