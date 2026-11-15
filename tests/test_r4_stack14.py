"""Round-4 review of #38 (HTML-only mail): the converter's hidden-content rules, failure
reporting, plain-alternative choice, summary invalidation and the documentation that describes
them. Every case runs through a real .eml file and the real queue."""

import unittest
from pathlib import Path

from test_export_formats import mail
from test_outside import OutsideFixture, needs

NOT_SHOWN = "Part of this HTML body is not shown"
ROOT = Path(__file__).resolve().parents[1]


class Mail(OutsideFixture):
    def text(self, html=None, plain=None, **kw):
        self.watch()
        for p in self.folder.glob("*.eml"):
            p.unlink()
        (self.folder / "m.eml").write_bytes(mail(html, plain, **kw).as_bytes())
        (item,) = needs.build_queue()["items"]
        return item["text"]


class VisibleFormText(Mail):
    def test_textarea_text_is_shown(self):
        t = self.text("<textarea readonly>Approval instructions</textarea>")
        self.assertIn("Approval instructions", t)
        self.assertNotIn("only attachments", t)


class HiddenContent(Mail):
    LEAKS = (
        '<span style="opacity:0!important">PRIVATE</span>',
        '<span style="opacity:0.0">PRIVATE</span>',
        '<span style="opacity:0%">PRIVATE</span>',
        '<span style="display/**/:/**/none">PRIVATE</span>',
        '<span style="DISPLAY : NONE ! important">PRIVATE</span>',
        '<span style="visibility:hidden">PRIVATE</span>',
        '<span style="font-size:0px">PRIVATE</span>',
        '<div style="width:0;height:0;overflow:hidden">PRIVATE</div>',
        '<div style="max-height:0;overflow:hidden">PRIVATE</div>',
        "<style>.hide{display:none}</style><div class=hide>PRIVATE</div>",
        "<style>div.hide, .other{display:none !important}</style><div class='x hide'>PRIVATE</div>",
        "<style>#gone{opacity:0}</style><p id=gone>PRIVATE</p>",
        "<style>/* c */ .a .b{display:none}</style><div class=a><p><span class=b>PRIVATE</span></p></div>",
        "<style>.a > .b{display:none}</style><div class=a><span class=b>PRIVATE</span></div>",
        "<style>p{display:none}</style><p>PRIVATE</p>",
        "<div hidden><span style='display:block'>PRIVATE</span></div>",
        "<div style='display:none'><span style='display:block'>PRIVATE</span></div>",
        "<div style='opacity:0'><span style='opacity:1'>PRIVATE</span></div>",
        "<div style='width:0;overflow:hidden'><span style='width:100px'>PRIVATE</span></div>",
    )

    def test_hidden_text_never_reaches_the_card(self):
        for html in self.LEAKS:
            with self.subTest(html=html):
                self.assertNotIn("PRIVATE", self.text("<p>Real</p>" + html + "<p>tail</p>"))

    def test_visible_text_next_to_hidden_text_stays(self):
        t = self.text("<style>.hide{display:none}</style><p>Real</p><div class=hide>PRIVATE</div><p>tail</p>")
        self.assertIn("Real", t)
        self.assertIn("tail", t)

    def test_a_property_name_is_matched_exactly_not_inside_another(self):
        for style in ("min-width:0", "border-width:0", "line-height:0", "--display:none", "--opacity:0",
                      "max-width:none;height:auto", "margin:0", "padding-width:0", "font-size:16px",
                      "x-visibility:hidden"):
            with self.subTest(style=style):
                self.assertIn("Approve budget", self.text(f'<div style="{style}">Approve budget</div>'))

    def test_a_visible_child_of_a_font_size_zero_or_invisible_parent_stays(self):
        for html in (
            '<div style="font-size:0"><span style="font-size:16px">Real body</span></div>',
            '<div style="visibility:hidden"><span style="visibility:visible">Real body</span></div>',
            '<div style="width:0;height:0"><span>Real body</span></div>',
            '<div style="max-height:0;overflow:visible">Real body</div>',
            "<style>.z{font-size:0}.z .k{font-size:14px}</style><div class=z><b class=k>Real body</b></div>",
        ):
            with self.subTest(html=html):
                self.assertIn("Real body", self.text(html))

    def test_zero_size_text_without_a_visible_override_is_dropped(self):
        t = self.text('<div style="font-size:0">PRIVATE<span>PRIVATE2</span></div><p>Real</p>')
        self.assertNotIn("PRIVATE", t)
        self.assertIn("Real", t)

    def test_hiding_rules_the_converter_cannot_evaluate_are_reported(self):
        t = self.text("<style>a[href]{display:none}</style><p>Body</p>")
        self.assertIn("Body", t)
        self.assertIn("not evaluated", t)
        t = self.text("<style>@media print{p{display:none}}</style><p>Body</p>")
        self.assertIn("not evaluated", t)

    def test_a_stylesheet_longer_than_is_read_is_reported(self):
        t = self.text("<style>/*" + "x" * 210_000 + "*/ .a{display:none}</style><p>Body</p>")
        self.assertIn("not evaluated", t)

    def test_more_rules_than_are_evaluated_are_reported(self):
        css = "".join(f".c{i}{{color:red}}.d{i}{{display:none}}" for i in range(2100))
        t = self.text(f"<style>{css}</style><p class=c1>Body</p>")
        self.assertIn("not evaluated", t)

    def test_a_stylesheet_rule_that_does_not_hide_is_not_reported(self):
        t = self.text("<style>p{color:red}@media print{p{color:blue}}</style><p>Body</p>")
        self.assertNotIn("not evaluated", t)


class TreeShape(Mail):
    def test_an_omitted_head_end_tag_does_not_hide_the_body(self):
        t = self.text("<head><title>T</title><body><p>Real message</p>")
        self.assertIn("Real message", t)

    def test_head_content_without_a_body_tag_ends_the_head(self):
        self.assertIn("Real message", self.text("<head><title>T</title><p>Real message</p>"))

    def test_a_self_closing_script_still_opens_a_script(self):
        t = self.text("<p>Real</p><script/>SCRIPT_CONTENT</script><p>after</p>")
        self.assertNotIn("SCRIPT_CONTENT", t)
        self.assertIn("after", t)

    def test_other_self_closing_non_void_hidden_elements_open_too(self):
        t = self.text("<style/>STYLE_TEXT</style><p>after</p>")
        self.assertNotIn("STYLE_TEXT", t)

    def test_deep_nesting_inside_a_hidden_element_cannot_unhide_the_tail(self):
        html = "<div hidden>" + "<div>" * 2000 + "</div>" * 2000 + "PRIVATE_TAIL</div>"
        t = self.text("<p>Start</p>" + html + "<p>end</p>")
        self.assertNotIn("PRIVATE_TAIL", t)
        self.assertIn(NOT_SHOWN, t)

    def test_deep_nesting_in_visible_text_reports_what_was_left_out(self):
        t = self.text("<p>Start</p>" + "<div>" * 5000 + "deep" + "</div>" * 5000 + "<p>after</p>")
        self.assertIn("Start", t)
        self.assertIn(NOT_SHOWN, t)

    def test_ordinary_nesting_is_not_reported(self):
        t = self.text("<table>" * 50 + "<tr><td>Cell</td></tr>" + "</table>" * 50)
        self.assertIn("Cell", t)
        self.assertNotIn(NOT_SHOWN, t)


class FailureReported(Mail):
    def test_an_oversized_character_reference_does_not_drop_the_tail(self):
        t = self.text("<p>Start</p>&#" + "9" * 5000 + ";<p>Important tail</p>")
        self.assertIn("Important tail", t)

    def test_a_hex_reference_that_overflows_does_not_drop_the_tail(self):
        t = self.text("<p>Start</p>&#x" + "f" * 5000 + ";<p>Important tail</p>")
        self.assertIn("Important tail", t)

    def test_an_unterminated_comment_is_reported(self):
        t = self.text("<p>Start</p><!-- never closed <p>Important tail</p>")
        self.assertIn("Start", t)
        self.assertIn(NOT_SHOWN, t)

    def test_an_unterminated_quoted_attribute_is_reported(self):
        t = self.text('<p>Start</p><a href="never closed <p>Important tail</p>')
        self.assertIn("Start", t)
        self.assertIn(NOT_SHOWN, t)

    def test_a_converter_failure_is_reported_not_swallowed(self):
        from unittest.mock import patch

        real = needs._InertText.feed

        def boom(self, data):
            real(self, "<p>Start</p>")
            raise RuntimeError("parser failed")

        with patch.object(needs._InertText, "feed", boom):
            t = self.text("<p>Start</p><p>Important tail</p>")
        self.assertIn("Start", t)
        self.assertIn(NOT_SHOWN, t)

    def test_a_complete_message_is_not_reported(self):
        self.assertNotIn(NOT_SHOWN, self.text("<p>Start</p><p>Tail</p>"))


class PlainAlternative(Mail):
    def test_an_invisible_plain_part_does_not_win_over_the_html_body(self):
        for plain in ("​", "﻿", "\x00", "​ ‌\n", "⁠ ", "­"):
            with self.subTest(plain=repr(plain)):
                t = self.text("<p>Actual body</p>", plain=plain)
                self.assertIn("Actual body", t)

    def test_a_real_plain_part_still_wins(self):
        t = self.text("<p>HTML version</p>", plain="Plain version")
        self.assertIn("Plain version", t)
        self.assertNotIn("HTML version", t)

    def test_an_invisible_only_message_says_so(self):
        t = self.text(plain="​")
        self.assertNotIn("​", t)


class SearchAndSummary(Mail):
    """The downstream consumers of a card made from HTML: full-text search and the model prompt."""

    def test_search_finds_html_only_text_and_not_hidden_text(self):
        self.text("<p>The zebra clause is HTML only</p><div hidden>PRIVATEWORD</div>")
        hit = needs.search_text("zebra")
        self.assertEqual(len(hit["matches"]), 1)
        self.assertEqual(hit["matches"][0]["source"], "outside")
        self.assertEqual(needs.search_text("PRIVATEWORD")["matches"], [])

    def test_the_summary_request_carries_the_converted_text_only(self):
        self.text("<p>The zebra clause is HTML only</p><div hidden>PRIVATEWORD</div>"
                  "<a href='https://track.example/c?u=SECRETID'>click</a>")
        (item,) = needs.build_queue()["items"]
        content = needs.bluf_messages(item)[1]["content"]
        self.assertIn("zebra clause", content)
        self.assertNotIn("PRIVATEWORD", content)
        self.assertNotIn("SECRETID", content)
        self.assertNotIn("<p>", content)


class SummaryCache(Mail):
    def test_a_summary_written_from_the_old_interpretation_is_not_attached(self):
        self.text("<p>Body</p>")
        (item,) = needs.build_queue()["items"]
        old = dict(item, ask=needs._NO_BODY)  # what the card said before HTML was converted
        needs.save_bluf(item["id"], item["version"], "Old summary", "why", fp=needs.summary_fingerprint(old))
        (again,) = needs.attach_blufs(needs.build_queue()["items"])
        self.assertNotIn("bluf", again)


class Docs(unittest.TestCase):
    guide = (ROOT / "docs" / "operator-guide.md").read_text()
    readme = (ROOT / "README.md").read_text()
    manifest = (ROOT / "plugin.yaml").read_text()

    def test_the_readme_no_longer_promises_the_old_notice_for_html_only_mail(self):
        self.assertNotIn("HTML-only drafts keep the no-plain-text-body notice", self.readme)
        self.assertIn("HTML-only", self.readme)

    def test_the_guide_does_not_claim_tracking_addresses_never_reach_the_card(self):
        self.assertNotIn("a\ntracking address never reaches the card", self.guide)
        self.assertNotIn("tracking address never reaches", self.guide.replace("\n", " "))
        flat = self.guide.replace("\n", " ")
        self.assertIn("printed as text", flat)

    def test_the_draft_marker_precedence_is_stated_the_same_everywhere(self):
        flat = self.guide.replace("\n", " ")
        self.assertNotIn("and is never called a draft", flat)
        self.assertNotIn("is never called a draft", self.manifest)
        self.assertIn("unsent marker wins", flat)
        self.assertIn("even with received-mail headers", self.manifest)

    def test_the_guide_lists_what_the_converter_evaluates_and_leaves_alone(self):
        flat = self.guide.replace("\n", " ")
        for phrase in ("textarea", "stylesheet", "not evaluated", "nested too deeply"):
            self.assertIn(phrase, flat, phrase)


class DraftPrecedence(Mail):
    def test_an_unsent_marker_wins_over_received_headers(self):
        self.watch()
        (self.folder / "m.eml").write_bytes(
            mail(plain="B", X_Unsent="1", Received="from x by y; now").as_bytes())
        (item,) = needs.build_queue()["items"]
        self.assertEqual(item["kind"], "draft")


if __name__ == "__main__":
    unittest.main()
