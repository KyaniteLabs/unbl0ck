"""Inbox regressions: exported mail bodies, honest previews and local CLI output."""

import io
import json
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from email.message import EmailMessage
from unittest.mock import patch

from test_outside import OutsideFixture, needs, plugin


class ExportedMail(OutsideFixture):
    def test_html_only_body_does_not_fall_back_to_text_attachment(self):
        self.watch()
        msg = EmailMessage()
        msg.set_content("<p>The draft is HTML-only.</p>", subtype="html")
        msg.add_attachment("Unrelated attachment", filename="appendix.txt")
        (self.folder / "draft.eml").write_bytes(msg.as_bytes())
        (item,) = needs.build_queue()["items"]
        self.assertEqual(item["text"], "(No text/plain body; HTML is not displayed.)")

    def test_attached_message_is_not_the_draft_body(self):
        self.watch()
        msg = EmailMessage()
        msg.set_content("<p>HTML draft</p>", subtype="html")
        attached = EmailMessage()
        attached.set_content("An entirely different message")
        msg.add_attachment(attached)
        (self.folder / "draft.eml").write_bytes(msg.as_bytes())
        (item,) = needs.build_queue()["items"]
        self.assertNotIn("entirely different", item["text"])
        self.assertIn("No text/plain body", item["text"])

    def test_attachment_before_the_body_is_skipped(self):
        self.watch()
        msg = EmailMessage()
        msg.set_content("The actual draft body")
        msg.add_attachment("Attachment first", filename="appendix.txt")
        msg.set_payload(list(reversed(msg.get_payload())))
        (self.folder / "draft.eml").write_bytes(msg.as_bytes())
        (item,) = needs.build_queue()["items"]
        self.assertEqual(item["text"].strip(), "The actual draft body")

    def test_inline_named_text_file_does_not_replace_an_html_body(self):
        self.watch()
        msg = EmailMessage()
        msg.set_content("<p>Actual HTML body</p>", subtype="html")
        msg.add_attachment("APPENDIX TEXT", subtype="plain", filename="appendix.txt", disposition="inline")
        (self.folder / "draft.eml").write_bytes(msg.as_bytes())
        (item,) = needs.build_queue()["items"]
        self.assertEqual(item["text"], "(No text/plain body; HTML is not displayed.)")

    def test_inline_named_part_before_real_plain_alternative_is_skipped(self):
        self.watch()
        msg = EmailMessage()
        msg.set_content("Actual plain body")
        msg.add_alternative("<p>Actual HTML body</p>", subtype="html")
        msg.add_attachment("APPENDIX TEXT", filename="appendix.txt", disposition="inline")
        msg.set_payload(list(reversed(msg.get_payload())))
        (self.folder / "draft.eml").write_bytes(msg.as_bytes())
        (item,) = needs.build_queue()["items"]
        self.assertEqual(item["text"].strip(), "Actual plain body")

    def test_related_resource_is_not_selected_in_place_of_its_root(self):
        self.watch()
        msg = EmailMessage()
        msg.set_content("<p>Actual HTML body</p>", subtype="html")
        msg.add_related("Inline text resource", subtype="plain", cid="resource")
        (self.folder / "draft.eml").write_bytes(msg.as_bytes())
        (item,) = needs.build_queue()["items"]
        self.assertEqual(item["text"], "(No text/plain body; HTML is not displayed.)")

    def test_preview_reports_truncation_only_when_bytes_were_omitted(self):
        self.watch()
        for size in (needs._OUTSIDE_READ_LIMIT, needs._OUTSIDE_READ_LIMIT + 1):
            with self.subTest(size=size):
                (self.folder / "notes.txt").write_bytes(b"a" * size)
                (item,) = needs.build_queue()["items"]
                self.assertEqual(item["truncated"], size > needs._OUTSIDE_READ_LIMIT)
                self.assertLessEqual(len(item["text"]), needs._OUTSIDE_READ_LIMIT)


class CommandOutput(unittest.TestCase):
    def test_empty_incomplete_queue_never_claims_all_clear(self):
        q = {"count": 0, "items": [], "errors": ["outside: unreadable folder"]}
        with patch.object(plugin, "_needs", return_value=types.SimpleNamespace(build_queue=lambda: q)):
            local = plugin.slash_unbl0ck(platform="cli")
            remote = plugin.slash_unbl0ck(platform="telegram")
        self.assertIn("incomplete", local.lower())
        self.assertIn("outside: unreadable folder", local)
        self.assertIn("incomplete", remote.lower())
        self.assertNotIn("unreadable folder", remote)
        self.assertNotIn("Nothing is blocked", local + remote)

    def test_json_remains_parseable_when_also_writing_a_pack(self):
        q = {"count": 0, "items": [], "errors": []}
        n = types.SimpleNamespace(build_queue=lambda: q, write_pocket_pack=lambda q: "pack.json")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(plugin, "_needs", return_value=n), redirect_stdout(stdout), redirect_stderr(stderr):
            plugin._cli(types.SimpleNamespace(json=True, pack=True))
        self.assertEqual(json.loads(stdout.getvalue()), q)
        self.assertIn("pack.json", stderr.getvalue())
