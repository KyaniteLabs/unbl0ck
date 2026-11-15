"""The shared redaction check fails on a partial leak and on a lost label, not only on the whole secret."""

import unittest

from _redaction import assert_redacted, secret_pieces


class RedactionHelper(unittest.TestCase):
    def test_a_partial_leak_fails(self):
        for text in ("password=PART[redacted]", "x SECRxx", "CRET"):
            with self.subTest(text=text), self.assertRaises(AssertionError):
                assert_redacted(text, "PARTSECRET")

    def test_a_lost_label_fails(self):
        with self.assertRaises(AssertionError):
            assert_redacted("[redacted]", "PARTSECRET", label="password")

    def test_a_clean_redaction_passes(self):
        assert_redacted("password=[redacted]", "PARTSECRET", label="password")

    def test_short_secrets_are_checked_whole(self):
        self.assertEqual(secret_pieces("abc"), {"abc"})
        self.assertEqual(secret_pieces("abcde"), {"abcd", "bcde"})


if __name__ == "__main__":
    unittest.main()
