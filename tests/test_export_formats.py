"""Exported content (issue #38): inert text from HTML-only mail, purpose from message metadata,
and honest notices for formats that are not read."""

import json
import unittest
from email.message import EmailMessage

from test_outside import OutsideFixture, needs

OUTLOOK_HTML = """<html xmlns:o="urn:schemas-microsoft-com:office:office"><head>
<meta http-equiv=Content-Type content="text/html; charset=utf-8">
<style>p.MsoNormal {margin:0} body {color:red}</style>
<!--[if gte mso 9]><xml><o:OfficeDocumentSettings>HIDDEN-MSO-XML</o:OfficeDocumentSettings></xml><![endif]-->
<title>HIDDEN-TITLE</title></head>
<body lang=EN-US><div class=WordSection1>
<p class=MsoNormal>Hi Dana,<o:p></o:p></p>
<p class=MsoNormal>Can you approve the&nbsp;Q3 budget &amp; sign off by Friday?<o:p></o:p></p>
<ul><li>Line one</li><li>Line two</li></ul>
<table><tr><td>Cost</td><td>$4,200</td></tr></table>
<p class=MsoNormal>Thanks,<br>Sam</p></div></body></html>"""


def mail(html=None, plain=None, **headers):
    msg = EmailMessage()
    msg["Subject"] = headers.pop("subject", "Budget")
    for k, v in headers.items():
        msg[k.replace("_", "-")] = v
    if plain is not None:
        msg.set_content(plain)
        if html is not None:
            msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(html, subtype="html")
    return msg


class HtmlOnly(OutsideFixture):
    def one(self, msg, name="m.eml"):
        self.watch()
        (self.folder / name).write_bytes(msg if isinstance(msg, bytes) else msg.as_bytes())
        (item,) = needs.build_queue()["items"]
        return item

    def test_outlook_html_becomes_readable_text(self):
        it = self.one(mail(OUTLOOK_HTML))
        t = it["text"]
        self.assertIn("Hi Dana,", t)
        self.assertIn("Can you approve the Q3 budget & sign off by Friday?", t)
        self.assertIn("Line one", t)
        self.assertIn("Cost", t)
        self.assertIn("$4,200", t)
        self.assertIn("Thanks,\nSam", t)
        for hidden in ("HIDDEN-MSO-XML", "HIDDEN-TITLE", "margin:0", "<p", "MsoNormal", "&amp;", "&nbsp;"):
            self.assertNotIn(hidden, t)

    def test_text_says_it_was_converted_from_html(self):
        self.assertIn("Converted from HTML", self.one(mail("<p>Body</p>"))["text"])

    def test_scripts_styles_frames_and_hidden_content_are_dropped(self):
        html = ("<p>Visible</p><script>alert('SCRIPT-TEXT')</script><style>.a{}STYLE-TEXT</style>"
                "<iframe src='https://evil.example/x'>IFRAME-TEXT</iframe><object>OBJECT-TEXT</object>"
                "<noscript>NOSCRIPT-TEXT</noscript><template>TEMPLATE-TEXT</template>"
                "<div style='display:none'>DISPLAYNONE-TEXT</div><span hidden>HIDDEN-ATTR-TEXT</span>"
                "<div style='visibility: hidden'>VISIBILITY-TEXT</div><p style='font-size:0'>FONT0-TEXT</p>"
                "<svg><text>SVG-TEXT</text></svg><p>After</p>")
        t = self.one(mail(html))["text"]
        self.assertIn("Visible", t)
        self.assertIn("After", t)
        for bad in ("SCRIPT-TEXT", "STYLE-TEXT", "IFRAME-TEXT", "OBJECT-TEXT", "NOSCRIPT-TEXT", "TEMPLATE-TEXT",
                    "DISPLAYNONE-TEXT", "HIDDEN-ATTR-TEXT", "VISIBILITY-TEXT", "FONT0-TEXT", "SVG-TEXT"):
            self.assertNotIn(bad, t)

    def test_links_images_and_tracking_never_reach_the_text(self):
        html = ("<p>Read <a href='https://track.example/c?u=SECRET-ID'>the report</a></p>"
                "<img src='https://track.example/pixel.gif?u=SECRET-ID' alt='PIXEL-ALT'>"
                "<img src='cid:logo' alt=''><link rel=stylesheet href='https://track.example/x.css'>")
        it = self.one(mail(html))
        blob = json.dumps(it)
        self.assertIn("the report", it["text"])
        for bad in ("track.example", "SECRET-ID", "PIXEL-ALT", "cid:logo", "http"):
            self.assertNotIn(bad, blob)

    def test_no_network_or_file_access_while_reading(self):
        import socket
        from unittest.mock import patch
        self.watch()
        (self.folder / "m.eml").write_bytes(mail("<img src='http://127.0.0.1:9/x'><p>Body</p>").as_bytes())
        with patch.object(socket, "socket", side_effect=AssertionError("network used")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
            (it,) = needs.build_queue()["items"]
        self.assertIn("Body", it["text"])

    def test_plain_alternative_still_wins_over_html(self):
        it = self.one(mail(html="<p>HTML-VERSION</p>", plain="Plain version"))
        self.assertIn("Plain version", it["text"])
        self.assertNotIn("HTML-VERSION", it["text"])
        self.assertNotIn("Converted from HTML", it["text"])

    def test_gmail_style_alternative_and_related_inline_image(self):
        msg = EmailMessage()
        msg["Subject"] = "Photos"
        msg.set_content("Plain text of the Gmail message")
        msg.add_alternative("<div dir='ltr'>Gmail <b>html</b><img src='cid:abc'></div>", subtype="html")
        msg.get_payload()[1].add_related(b"\x89PNG", "image", "png", cid="<abc>", filename="logo.png", disposition="inline")
        it = self.one(msg)
        self.assertIn("Plain text of the Gmail message", it["text"])

    def test_html_only_with_inline_image_part_reads_the_html_not_the_image(self):
        msg = mail("<p>Inline picture below</p><img src='cid:abc'>")
        msg.add_related(b"\x89PNG-BYTES", "image", "png", cid="<abc>", filename="logo.png", disposition="inline")
        t = self.one(msg)["text"]
        self.assertIn("Inline picture below", t)
        self.assertNotIn("PNG-BYTES", t)

    def test_an_html_attachment_is_never_substituted_for_the_body(self):
        msg = EmailMessage()
        msg["Subject"] = "Only attachments"
        msg.set_content("   ")
        msg.add_attachment("<p>ATTACHED-HTML</p>", subtype="html", filename="report.html")
        msg.add_attachment("<p>INLINE-NAMED-HTML</p>", subtype="html", filename="x.html", disposition="inline")
        t = self.one(msg)["text"]
        self.assertNotIn("ATTACHED-HTML", t)
        self.assertNotIn("INLINE-NAMED-HTML", t)

    def test_a_message_with_no_readable_body_says_so(self):
        msg = EmailMessage()
        msg["Subject"] = "Pic"
        msg.set_content(b"\x89PNG", maintype="image", subtype="png", filename="a.png")
        self.assertIn("No readable text body", self.one(msg)["text"])

    def test_charset_variation(self):
        raw = (b"From: a@example.test\r\nSubject: Caf\xe9\r\nMIME-Version: 1.0\r\n"
               b'Content-Type: text/html; charset="windows-1252"\r\nContent-Transfer-Encoding: 8bit\r\n\r\n'
               b"<p>Reuni\xf3n \x93quoted\x94 \x80 5</p>\r\n")
        t = self.one(raw)["text"]
        self.assertIn("Reunión “quoted” € 5", t)
        raw2 = (b"Subject: x\r\nContent-Type: text/html; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n"
                + __import__("base64").b64encode("<p>日本語 Zürich</p>".encode()) + b"\r\n")
        (self.folder / "m.eml").unlink()
        self.assertIn("日本語 Zürich", self.one(raw2, "n.eml")["text"])

    def test_unknown_charset_and_bad_bytes_do_not_break_the_queue(self):
        raw = (b"Subject: odd\r\nContent-Type: text/html; charset=x-no-such-charset\r\n\r\n<p>Bad \xff\xfe bytes</p>\r\n")
        t = self.one(raw)["text"]
        self.assertIn("Bad", t)

    def test_malformed_and_hostile_html_is_bounded_and_does_not_raise(self):
        cases = [
            "<p>unclosed <b><i>tags <div>",
            "<<<>>> & &#x110000; &#99999999; &bogus; <a href=>",
            "<p>" + "<div>" * 5000 + "deep" + "</div>" * 5000,
            "<scr<script>ipt>x</scr</script>ipt>MIXED",
            "<!-- never closed comment <p>COMMENT-TEXT",
            "<style>never closed STYLE-TEXT <p>after",
            "\x00\x01<p>control</p>",
        ]
        for html in cases:
            (self.folder / "m.eml").unlink(missing_ok=True)
            it = self.one(mail(html))
            self.assertNotIn("<script", it["text"].lower(), html[:30])
            self.assertNotIn("COMMENT-TEXT", it["text"])
            self.assertNotIn("STYLE-TEXT", it["text"])

    def test_text_swallowed_by_an_unclosed_hidden_element_is_reported_not_called_attachments(self):
        # Class sweep (E): an element that never closes hides the rest of the body. The queue
        # must say text was left out, not that the message has only attachments.
        for html in ("<div hidden>The whole real message", "<div style='display:none'>All of it",
                     "<style>never closed <p>real words</p>"):
            (self.folder / "m.eml").unlink(missing_ok=True)
            t = self.one(mail(html))["text"]
            self.assertNotIn("only attachments", t, html)
            self.assertIn("hidden or never closed", t, html)
            self.assertNotIn("real message", t)

    def test_a_note_is_added_when_some_text_survives_but_the_rest_was_swallowed(self):
        t = self.one(mail("<p>Visible start</p><div hidden>Lost tail"))["text"]
        self.assertIn("Visible start", t)
        self.assertIn("hidden or never closed", t)
        self.assertNotIn("Lost tail", t)

    def test_a_normal_message_carries_no_swallowed_note(self):
        t = self.one(mail(OUTLOOK_HTML))["text"]
        self.assertNotIn("hidden or never closed", t)

    def test_output_is_bounded_by_the_read_limit(self):
        big = "<p>" + ("word " * 200_000) + "</p>"  # about 1 MB
        it = self.one(mail(big))
        self.assertTrue(it["truncated"])
        self.assertLessEqual(len(it["text"]), needs._OUTSIDE_READ_LIMIT + 200)

    def test_search_and_summary_text_use_the_extracted_text(self):
        it = self.one(mail("<p>The zebra clause is HTML only</p>"))
        self.assertNotIn("<p>", it["text"])
        self.assertIn("zebra clause", it["text"])


class Purpose(OutsideFixture):
    def kind(self, msg):
        self.watch()
        for p in self.folder.glob("*.eml"):
            p.unlink()
        (self.folder / "m.eml").write_bytes(msg.as_bytes())
        (it,) = needs.build_queue()["items"]
        return it["kind"], it["act_where"]

    def test_explicit_draft_markers_are_drafts(self):
        for hdr in ({"X-Unsent": "1"}, {"X-Mozilla-Draft-Info": "internal/draft; vcard=0"},
                    {"X-Uniform-Type-Identifier": "com.apple.mail-draft"}):
            self.settings["outside_eml_kind"] = "other"  # the marker beats the operator default
            self.assertEqual(self.kind(mail(plain="Body", **hdr))[0], "draft", hdr)

    def test_a_received_message_is_not_called_a_draft(self):
        for hdr in ({"Received": "from mx.example by host; Tue, 3 Nov 2026 10:00:00 +0000"},
                    {"Return-Path": "<a@example.test>"}, {"Delivered-To": "me@example.test"}):
            kind, where = self.kind(mail(plain="Body", **hdr))
            self.assertEqual(kind, "other", hdr)
            self.assertNotIn("Drafts", where)

    def test_a_draft_marker_wins_over_received_headers(self):
        self.assertEqual(self.kind(mail(plain="B", X_Unsent="1", Received="from x by y; now"))[0], "draft")

    def test_unlabelled_mail_uses_the_operator_setting_and_defaults_to_draft(self):
        self.assertEqual(self.kind(mail(plain="Body")), ("draft", "Your mail app → Drafts"))
        self.settings["outside_eml_kind"] = "notes"
        self.assertEqual(self.kind(mail(plain="Body"))[0], "notes")
        self.settings["outside_eml_kind"] = "approval"
        self.assertEqual(self.kind(mail(plain="Body"))[0], "approval")

    def test_a_bad_setting_falls_back_to_draft(self):
        for bad in ("", "banana", 5, None, ["draft"]):
            self.settings["outside_eml_kind"] = bad
            self.assertEqual(self.kind(mail(plain="Body"))[0], "draft", bad)

    def test_the_label_never_changes_what_the_text_says(self):
        self.settings["outside_eml_kind"] = "other"
        self.watch()
        (self.folder / "m.eml").write_bytes(mail(plain="Body text").as_bytes())
        (it,) = needs.build_queue()["items"]
        self.assertIn("Body text", it["text"])


class UnsupportedFormats(OutsideFixture):
    def errors(self):
        self.watch()
        return [e for e in needs.build_queue()["errors"] if "unsupported" in e.lower()]

    def test_msg_mbox_and_documents_are_reported_not_silently_ignored(self):
        for n in ("a.msg", "b.MBOX", "c.pdf", "d.docx", "e.m4a", "f.html", "g.htm"):
            (self.folder / n).write_bytes(b"x")
        (self.folder / "ok.txt").write_text("Readable")
        (errs,) = self.errors()
        self.assertIn("7 files", errs)
        for ext in (".msg", ".mbox", ".pdf", ".docx", ".m4a"):
            self.assertIn(ext, errs)
        for name in ("a.msg", "b.MBOX", "ok.txt"):
            self.assertNotIn(name, errs)  # counts and extensions only, never file names
        self.assertEqual(needs.build_queue()["count"], 1)

    def test_unlisted_extensions_and_dotfiles_stay_quiet(self):
        (self.folder / "pic.png").write_bytes(b"x")
        (self.folder / ".hidden.msg").write_bytes(b"x")
        (self.folder / "notes.txt").write_text("Readable")
        self.assertEqual(self.errors(), [])

    def test_no_item_is_created_for_an_unsupported_file(self):
        (self.folder / "a.msg").write_bytes(b"Subject: x\r\n\r\nbody")
        self.watch()
        self.assertEqual(needs.build_queue()["items"], [])

    def test_the_format_matrix_is_published_and_matches_the_code(self):
        guide = (needs.Path(__file__).resolve().parents[1] / "docs/operator-guide.md").read_text()
        self.assertIn("Supported exported formats", guide)
        for ext in (*needs._OUTSIDE_EXTS, *needs._OUTSIDE_UNSUPPORTED):
            self.assertIn(f"`{ext}`", guide, ext)


if __name__ == "__main__":
    unittest.main()
