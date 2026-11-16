"""Round-4 findings 01-052, 01-055, 01-056, 01-067, 01-065, 01-066 (chat half): who a chat is with,
how much of a message is read, which text a version names, and how one chat is looked up."""

import sqlite3
import unittest
from unittest.mock import patch

from r4c_common import ASK, HomeCase, chat_db, needs


class OwnerScope(HomeCase):
    def test_a_chat_with_an_unknown_source_and_a_stranger_is_not_shown(self):
        """01-052 (class F): the owner filter must not be skipped when the source is unknown."""
        self.settings["owner_user_ids"] = "owner"
        chat_db(
            self.home / "state.db",
            [
                ("stranger", None, "stranger", "needle " + ASK, "dm"),
                ("mine", None, "owner", ASK, "dm"),
                ("legacy", None, None, ASK, "dm"),
                ("them", "discord", "stranger", ASK, "dm"),
            ],
        )
        shown = sorted(i["session_id"] for i in needs.chat_items(self.home))
        self.assertEqual(shown, ["legacy", "mine"])

    def test_an_empty_source_with_a_stranger_is_not_shown(self):
        self.settings["owner_user_ids"] = "owner"
        chat_db(self.home / "state.db", [("stranger", "", "stranger", ASK, "dm")])
        self.assertEqual(needs.chat_items(self.home), [])


class BoundedRead(HomeCase):
    def test_a_huge_message_is_cut_before_it_is_processed(self):
        """01-055 (class D): the row limit bounds what Python receives, not only what it prints."""
        huge = "filler " * 700_000 + ASK  # about 4.9 million characters
        chat_db(self.home / "state.db", [("big", "cli", "u", huge, "dm")])
        seen = []
        real = needs._ask_redact
        with patch.object(needs, "_ask_redact", lambda text: seen.append(len(text)) or real(text)):
            (item,) = needs.chat_items(self.home, full=True)
        # The read end plus the lead redacted with it (#73), never the whole message.
        self.assertTrue(seen and max(seen) <= needs.CHAT_READ_CHARS + needs.CHAT_LEAD_CHARS, seen)
        self.assertTrue(item["ask"].endswith(ASK))
        self.assertTrue(item["ask_truncated"])
        self.assertTrue(item["text_cut"])
        self.assertTrue(item["text"].endswith(ASK))
        self.assertLessEqual(len(item["text"]), needs.FULL_TEXT_LIMIT)

    def test_a_message_within_the_limit_is_unchanged(self):
        body = "Opening line.\n\n" + "filler " * 200 + "\n" + ASK
        chat_db(self.home / "state.db", [("s", "cli", "u", body, "dm")])
        (item,) = needs.chat_items(self.home, full=True)
        self.assertEqual(item["text"], body)
        self.assertFalse(item["text_cut"])


class NamedGroupAsk(HomeCase):
    def test_a_name_in_a_long_option_packet_still_names_you(self):
        """01-056 (class E): the name check looks where ask detection looked."""
        body = "Ada, options for you:\n" + "1. choose a region, then wait.\n" * 45
        self.assertTrue(needs.chat_asks_you(body))
        chat_db(self.home / "state.db", [("g", "discord", "u", body, "group")])
        (item,) = needs.chat_items(self.home)
        self.assertEqual(item["session_id"], "g")

    def test_an_unnamed_group_ask_is_still_skipped(self):
        body = "Bob, options for you:\n" + "1. choose a region, then wait.\n" * 45
        chat_db(self.home / "state.db", [("g", "discord", "u", body, "group")])
        self.assertEqual(needs.chat_items(self.home), [])

    def test_a_name_far_before_a_short_question_does_not_name_you(self):
        body = "Ada was mentioned long ago. " + "x " * 600 + "\nShould you merge it?"
        chat_db(self.home / "state.db", [("g", "discord", "u", body, "group")])
        self.assertEqual(needs.chat_items(self.home), [])


class ChatVersion(HomeCase):
    def edit(self, text):
        db = sqlite3.connect(self.home / "state.db")
        db.execute("UPDATE messages SET content=? WHERE id=1", (text,))
        db.commit()
        db.close()

    def setUp(self):
        super().setUp()
        chat_db(self.home / "state.db", [("s1", "cli", "u", "Original text. " + ASK, "dm")])

    def test_an_in_place_rewrite_changes_the_version(self):
        """01-067 (class A): the version names the text, not only the message row."""
        (before,) = needs.chat_items(self.home)
        (same,) = needs.chat_items(self.home)
        self.assertEqual(before["version"], same["version"])
        self.edit("Rewritten text. Should I deploy now?")
        (after,) = needs.chat_items(self.home)
        self.assertNotEqual(before["version"], after["version"])

    def test_the_old_version_no_longer_opens_full_text(self):
        (before,) = needs.chat_items(self.home)
        self.edit("Rewritten text. Should I deploy now?")
        with self.assertRaises(needs.StaleAnswer):
            needs.source_text(before["id"], before["version"])

    def test_the_current_version_opens_full_text(self):
        (item,) = needs.chat_items(self.home)
        reply = needs.source_text(item["id"], item["version"])
        self.assertIn("Original text.", reply["text"])


class LookupWork(HomeCase):
    def make(self, n=30):
        chat_db(
            self.home / "state.db",
            [(f"s{i}", "cli", "u", f"Chat {i}. " + ASK, "dm") for i in range(n)],
        )

    def test_one_chat_is_expanded_not_every_chat(self):
        """01-065 (class D): only the requested record is expanded to full text."""
        self.make()
        (item,) = [i for i in needs.chat_items(self.home) if i["session_id"] == "s7"]
        calls = []
        real = needs._full_fields
        with patch.object(needs, "_full_fields", lambda t: calls.append(1) or real(t)):
            reply = needs.source_text(item["id"], item["version"])
        self.assertIn("Chat 7.", reply["text"])
        self.assertEqual(len(calls), 1)

    def test_a_malformed_reference_opens_no_database(self):
        self.make()
        opened = []
        real = sqlite3.connect
        with patch.object(sqlite3, "connect", lambda *a, **kw: opened.append(a) or real(*a, **kw)):
            for bad in ("chat:default", "chat::", "chat:../x:s1"):
                with self.assertRaises(LookupError):
                    needs.source_text(bad, "1")
        self.assertEqual(opened, [])

    def test_a_deleted_chat_is_a_404_even_when_another_profile_is_corrupt(self):
        """01-066 (class M, source-failure coupling): an unrelated broken profile cannot hide a missing one."""
        chat_db(self.home / "profiles" / "good" / "state.db", [("s1", "cli", "u", ASK, "dm")])
        bad = self.home / "profiles" / "bad"
        bad.mkdir(parents=True)
        (bad / "state.db").write_bytes(b"this is not a database" * 50)
        queue = needs.build_queue(self.home)
        (item,) = [i for i in queue["items"] if i["source"] == "chat"]
        db = sqlite3.connect(self.home / "profiles" / "good" / "state.db")
        db.execute("DELETE FROM sessions")
        db.commit()
        db.close()
        with self.assertRaises(LookupError):
            needs.source_text(item["id"], item["version"])

    def test_a_corrupt_target_profile_is_unavailable(self):
        chat_db(self.home / "profiles" / "good" / "state.db", [("s1", "cli", "u", ASK, "dm")])
        (item,) = needs.chat_items(self.home)
        (self.home / "profiles" / "good" / "state.db").write_bytes(b"this is not a database" * 50)
        with self.assertRaises(needs.SourceUnavailable):
            needs.source_text(item["id"], item["version"])


if __name__ == "__main__":
    unittest.main()
