"""Round-9 regressions: the two secret leaks Sol found in the round-8 fixes (R9-001, R9-002)."""

import json
import unittest

from test_r4_stack01d_kanban import needs
from test_r7_M import comments_table


class CutQuoteIsClosed(unittest.TestCase):
    def test_a_cut_comment_never_shows_part_of_a_credential_with_spaces(self):
        """R9-001: a quoted credential, cut before its closing quote or beside an escaped quote, is not sent."""
        for body in (
            'curl -u "alice:PARTSECRET ' + "b " * 24000 + '" https://example.test',
            'first line\ncurl -u "alice:PARTSECRET ' + "b " * 24000 + '" https://example.test',
            'curl -u "alice:PARTSECRET\n' + "b " * 24000 + '" https://example.test',
            'echo \\"; curl -u "alice:PARTSECRET b" ' + "word " * 6000,
        ):
            with self.subTest(body[:12]):
                self.assertNotIn("PARTSECRET", needs._ask_redact(body))
                (row,), _ = needs._newest_comments(comments_table(body), None, "t", 5)
                card = {"title": "t", "comments": [{"author": row[1], "body": row[2]}]}
                self.assertNotIn("PARTSECRET", json.dumps(needs.build_ask_messages(card, [])))
                self.assertIn("truncated", row[2])
        first = 'first line\ncurl -u "alice:PARTSECRET ' + "b " * 24000
        (row,), _ = needs._newest_comments(comments_table(first), None, "t", 5)
        self.assertTrue(row[2].startswith("first line\ncurl -u "), row[2][:40])


class SingleQuoteBackslashIsLiteral(unittest.TestCase):
    def test_a_single_quoted_value_ending_in_a_backslash_is_redacted(self):
        """Sol r9: in the shell a backslash inside single quotes is literal, so 'PARTSECRET\\' ends the value."""
        for text in (
            "curl -u 'alice:PARTSECRET\\' https://example.test",
            "curl -b 'sid=PARTSECRET\\' https://example.test",
            "mysql -u root -p'PARTSECRET\\' db",
            "keytool -list -storepass 'PARTSECRET\\' -v",
            "the password is 'PARTSECRET\\' ok",
        ):
            self.assertNotIn("PARTSECRET", needs._ask_redact(text), text)
        self.assertNotIn("PARTSECRET", needs._ask_redact("curl -u 'alice:PART\\'SECRET' x"))
        for text in (  # Sol r9c: the shell joins the quote to the rest of its word
            "keytool -list -storepass 'prefix\\'PARTSECRET -v",
            "mysql -u root -p'prefix\\'PARTSECRET db",
            "curl -u 'alice:pre\\'PARTSECRET https://example.test",
            "curl -b 'sid=pre\\'PARTSECRET https://example.test",
            "curl -u 'alice:pre\\'password=\"PARTSECRET\" x",  # Sol r9d: the word runs on through quotes
            "curl -u 'alice:pre\\'password=\"PART SECRET\" x",
        ):
            self.assertNotIn("PARTSECRET", needs._ask_redact(text), text)


class QuotedExplicitKeys(unittest.TestCase):
    def test_a_quoted_explicit_key_continued_across_lines_still_redacts(self):
        """R9-002: ``? "o\\`` then ``tp"`` is the key otp, so its value is redacted."""
        for text in (
            '? "o\\\n  tp"\n: |\n  123456\n',
            '? "pass\\\n  word"\n: 123456\n',
        ):
            out = needs._ask_redact(text)
            self.assertNotIn("123456", out, text)
            self.assertIn("[redacted]", out, text)
        self.assertEqual(needs._ask_redact('? "no\\\n  te"\n: hello\n'), '? "no\\\n  te"\n: hello\n')


if __name__ == "__main__":
    unittest.main()
