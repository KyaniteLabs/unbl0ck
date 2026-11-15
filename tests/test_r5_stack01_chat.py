"""Round-5 findings for the chat reader (stack 01): redaction of a cut message, link replacement
between the safety check and the open, whole-text versions, embedded NULs and group-name matching."""

import os
import sqlite3
import unittest

from r4c_common import ASK, HomeCase, chat_db, needs


class ACutMessageDoesNotLeakItsOpeningSecret(HomeCase):
    def test_a_tail_cut_inside_a_long_secret_is_not_shown(self):
        """R5-needs-001 (class F): the credential label lies before the cut, the value runs past it."""
        secret = "S3cr3tV4lue"
        text = "password=" + secret * 11000 + " " + ASK
        self.assertGreater(len(text), needs.CHAT_READ_CHARS)
        chat_db(self.home / "state.db", [("s", "cli", "u", text, "dm")])
        (item,) = needs.chat_items(self.home, full=True)
        self.assertNotIn(secret, item["ask"])
        self.assertNotIn(secret, item["text"])
        self.assertIn(ASK, item["ask"])

    def test_a_tail_cut_inside_a_private_key_body_is_not_shown(self):
        body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" * 5000
        pem = f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----\n{ASK}"
        chat_db(self.home / "state.db", [("s", "cli", "u", pem, "dm")])
        (item,) = needs.chat_items(self.home, full=True)
        self.assertNotIn("MIIEvQIBADANBg", item["ask"] + item["text"])


class AWrappedKeyCutMidBodyShowsNoKeyLines(HomeCase):
    def test_wrapped_key_lines_and_a_short_last_line_are_dropped(self):
        line = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj"
        lines = "\n".join([line] * 2500 + ["Zk9QmTLastShortLn=="])
        pem = f"-----BEGIN PRIVATE KEY-----\n{lines}\n-----END PRIVATE KEY-----\n{ASK}"
        self.assertGreater(len(pem), needs.CHAT_READ_CHARS)
        chat_db(self.home / "state.db", [("s", "cli", "u", pem, "dm")])
        (item,) = needs.chat_items(self.home, full=True)
        shown = item["ask"] + item["text"]
        self.assertNotIn("MIIEvQIB", shown)
        self.assertNotIn("Zk9QmTLast", shown)
        self.assertIn(ASK, item["ask"])


class ALinkPlacedAfterTheCheckIsNotRead(HomeCase):
    def test_a_database_replaced_by_a_link_before_it_opens_is_refused(self):
        """R5-needs-008 (class C): the identity checked is the identity opened."""
        other = self.base / "other-home"
        chat_db(other / "state.db", [("foreign", "cli", "u", "Foreign " + ASK, "dm")])
        chat_db(self.home / "state.db", [("mine", "cli", "u", ASK, "dm")])
        real = needs._state_dbs

        def swap(*args, **kwargs):
            found = real(*args, **kwargs)
            os.replace(self.home / "state.db", self.base / "moved.db")
            os.symlink(other / "state.db", self.home / "state.db")
            return found

        errors: list[str] = []
        from unittest.mock import patch

        with patch.object(needs, "_state_dbs", swap):
            items = needs.chat_items(self.home, errors=errors)
        self.assertEqual([i["session_id"] for i in items], [])
        self.assertTrue(any("not searched" in e for e in errors), errors)


class AVersionNamesTheWholeMessage(HomeCase):
    def edit(self, text):
        db = sqlite3.connect(self.home / "state.db")
        db.execute("UPDATE messages SET content=? WHERE id=1", (text,))
        db.commit()
        db.close()

    def test_an_edit_before_the_retained_tail_changes_the_version(self):
        """R5-needs-020 (class A): equal length and an identical tail are not an identical message."""
        filler = "filler words go here. " * 6000  # more than the retained tail
        question = " Should I merge it now?"
        chat_db(self.home / "state.db", [("s", "cli", "u", "Pay Alice. " + filler + question, "dm")])
        (before,) = needs.chat_items(self.home)
        self.edit("Pay Bobby. " + filler + question)
        (after,) = needs.chat_items(self.home)
        self.assertNotEqual(before["version"], after["version"])

    def test_an_edit_in_the_middle_changes_the_version(self):
        filler = "x" * 150_000
        chat_db(self.home / "state.db", [("s", "cli", "u", "A" + filler + "B" + filler + "C " + ASK, "dm")])
        (before,) = needs.chat_items(self.home)
        self.edit("A" + filler + "Z" + filler + "C " + ASK)
        (after,) = needs.chat_items(self.home)
        self.assertNotEqual(before["version"], after["version"])

    def test_an_untouched_message_keeps_its_version(self):
        filler = "filler words go here. " * 6000
        chat_db(self.home / "state.db", [("s", "cli", "u", "Pay Alice. " + filler + ASK, "dm")])
        (one,) = needs.chat_items(self.home)
        (two,) = needs.chat_items(self.home)
        self.assertEqual(one["version"], two["version"])


class AnEmbeddedNulDoesNotHideTheAsk(HomeCase):
    def test_text_after_a_nul_byte_is_still_read(self):
        """R5-needs-030 (class E): SQLite text functions stop at the first NUL."""
        chat_db(self.home / "state.db", [("s", "cli", "u", "Status\x00" + ASK, "dm")])
        (item,) = needs.chat_items(self.home, full=True)
        self.assertIn(ASK, item["ask"])
        self.assertIn(ASK, item["text"])
        self.assertNotIn("\x00", item["ask"] + item["text"])

    def test_a_huge_message_with_a_nul_is_still_cut_from_its_end(self):
        text = "head\x00" + "filler " * 40_000 + ASK
        chat_db(self.home / "state.db", [("s", "cli", "u", text, "dm")])
        (item,) = needs.chat_items(self.home, full=True)
        self.assertTrue(item["ask"].endswith(ASK))
        self.assertTrue(item["ask_truncated"])


class GroupNamesAreMatchedWhereAskDetectionLooked(HomeCase):
    def test_a_name_inside_a_url_does_not_name_you(self):
        """R5-needs-031 (class F): URLs are stripped before the ask is found, and before the name is."""
        chat_db(
            self.home / "state.db",
            [("g", "discord", "u", "Bob, should you merge it? https://example.test/Ada", "group")],
        )
        self.assertEqual(needs.chat_items(self.home), [])

    def test_a_long_link_after_a_named_ask_does_not_hide_the_name(self):
        link = "https://example.test/" + "a" * 1500
        chat_db(
            self.home / "state.db",
            [("g", "discord", "u", "Ada, should you merge it? " + link, "group")],
        )
        (item,) = needs.chat_items(self.home)
        self.assertEqual(item["session_id"], "g")


class AnIndentedPromptIsMarkedAsExcerpted(unittest.TestCase):
    def test_indentation_that_the_excerpt_removes_counts_as_excerpted(self):
        """R5-needs-042 (class K): the excerpt and the excerpt flag agree about what changed."""
        self.assertTrue(needs._is_excerpted("    print(1)"))
        self.assertEqual(needs._excerpt("    print(1)"), "print(1)")

    def test_plain_short_text_is_not_excerpted(self):
        self.assertFalse(needs._is_excerpted("Should I merge it?"))
        self.assertFalse(needs._is_excerpted("Should I merge it?\n"))
        self.assertFalse(needs._is_excerpted("\n\nShould I merge it?"))


if __name__ == "__main__":
    unittest.main()
