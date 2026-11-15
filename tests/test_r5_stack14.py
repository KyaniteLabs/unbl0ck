"""Round-5 review of #38 (HTML-only mail): CSS validity and cascade corners, optional end tags,
hidden-content and output-limit notices. Each case drives the real converter, and the
end-to-end ones a real .eml file through the real queue."""

import unittest
from pathlib import Path

from test_export_formats import mail
from test_outside import OutsideFixture, needs

ROOT = Path(__file__).resolve().parents[1]
NOT_SHOWN = "Part of this HTML body is not shown"
CUT = "longer than is shown"


def convert(markup, limit=10000):
    return needs._html_convert(markup, limit)


class InvalidDeclarations(unittest.TestCase):
    def test_an_invalid_declaration_does_not_override_a_valid_one(self):
        for css in (
            "p{display:none;display:invalid}",
            "p{display:none;display:}",
            "p{display:none;display:5px}",
            "p{opacity:0;opacity:abc}",
            "p{visibility:hidden;visibility:sometimes}",
            "p{font-size:0;font-size:big-ish}",
        ):
            with self.subTest(css=css):
                text, _ = convert(f"<style>{css}</style><p>HIDDEN</p><div>kept</div>")
                self.assertNotIn("HIDDEN", text)
                self.assertIn("kept", text)

    def test_an_invalid_inline_declaration_does_not_override_a_valid_one(self):
        text, _ = convert('<p style="display:none;display:invalid">HIDDEN</p><div>kept</div>')
        self.assertNotIn("HIDDEN", text)

    def test_a_valid_later_declaration_still_wins(self):
        text, _ = convert("<style>p{display:none;display:block}</style><p>SHOWN</p>")
        self.assertIn("SHOWN", text)


class SelectorMatching(unittest.TestCase):
    def test_descendant_match_backtracks_to_an_outer_ancestor(self):
        text, _ = convert(
            "<style>.a > .b .c{display:none}</style>"
            '<div class="a"><div class="b"><div><div class="b"><p class="c">HIDDEN</p></div></div></div></div>'
        )
        self.assertNotIn("HIDDEN", text)

    def test_backtracking_does_not_invent_a_match(self):
        text, _ = convert(
            "<style>.a > .b .c{display:none}</style>"
            '<div class="x"><div class="b"><p class="c">VISIBLE</p></div></div>'
        )
        self.assertIn("VISIBLE", text)

    def test_two_different_ids_in_one_compound_never_match(self):
        text, _ = convert('<style>#a#b{display:none}</style><p id="b">VISIBLE</p><p id="a">TOO</p>')
        self.assertIn("VISIBLE", text)
        self.assertIn("TOO", text)

    def test_the_same_id_repeated_still_matches(self):
        text, _ = convert('<style>#a#a{display:none}</style><p id="a">HIDDEN</p><div>kept</div>')
        self.assertNotIn("HIDDEN", text)


class InheritedResets(unittest.TestCase):
    def test_initial_and_friends_restore_what_a_parent_hid(self):
        for outer, inner in (
            ("visibility:hidden", "visibility:initial"),
            ("visibility:hidden", "visibility:revert"),
            ("font-size:0", "font-size:initial"),
            ("font-size:0", "font-size:revert"),
            ("font-size:0", "font-size:medium"),
        ):
            with self.subTest(inner=inner):
                text, _ = convert(f'<div style="{outer}"><span style="{inner}">VISIBLE</span></div>')
                self.assertIn("VISIBLE", text)

    def test_inherit_and_unset_keep_what_a_parent_hid(self):
        for outer, inner in (
            ("visibility:hidden", "visibility:inherit"),
            ("visibility:hidden", "visibility:unset"),
            ("font-size:0", "font-size:inherit"),
            ("font-size:0", "font-size:unset"),
        ):
            with self.subTest(inner=inner):
                text, _ = convert(f'<div style="{outer}"><span style="{inner}">HIDDEN</span></div><p>k</p>')
                self.assertNotIn("HIDDEN", text)


class InertTemplates(unittest.TestCase):
    def test_a_style_inside_a_template_applies_to_nothing(self):
        text, _ = convert("<template><style>p{display:none}</style></template><p>VISIBLE</p>")
        self.assertIn("VISIBLE", text)

    def test_a_style_after_a_template_still_applies(self):
        text, _ = convert("<template><p>x</p></template><style>p{display:none}</style><p>HIDDEN</p><div>k</div>")
        self.assertNotIn("HIDDEN", text)


class OptionalEndTags(unittest.TestCase):
    def test_body_text_ends_an_omitted_head(self):
        text, _ = convert("<head><title>Title</title>VISIBLE")
        self.assertIn("VISIBLE", text)
        self.assertNotIn("Title", text)

    def test_text_inside_an_open_title_is_still_the_title(self):
        text, _ = convert("<head><title>Title VISIBLE</title></head><p>body</p>")
        self.assertNotIn("VISIBLE", text)
        self.assertIn("body", text)

    def test_a_new_table_section_ends_the_open_one(self):
        text, _ = convert("<table><tbody hidden><tr><td>HIDDEN<tbody><tr><td>VISIBLE</table>")
        self.assertIn("VISIBLE", text)
        self.assertNotIn("HIDDEN", text)

    def test_head_and_foot_sections_end_each_other_too(self):
        text, _ = convert("<table><thead hidden><tr><td>HIDDEN<tfoot><tr><td>VISIBLE</table>")
        self.assertIn("VISIBLE", text)
        self.assertNotIn("HIDDEN", text)

    def test_a_nested_table_section_does_not_end_the_outer_one(self):
        text, _ = convert(
            "<table><tbody hidden><tr><td><table><tbody><tr><td>HIDDEN</table></table><p>after</p>"
        )
        self.assertNotIn("HIDDEN", text)


class Notices(unittest.TestCase):
    def test_a_closed_hidden_element_is_reported(self):
        for markup in (
            "<div hidden>omitted</div><p>visible</p>",
            '<span style="display:none">omitted</span><p>visible</p>',
            '<span style="visibility:hidden">omitted</span><p>visible</p>',
            "<style>.h{opacity:0}</style><p class=h>omitted</p><p>visible</p>",
        ):
            with self.subTest(markup=markup):
                text, notes = convert(markup)
                self.assertEqual(text, "visible")
                self.assertEqual(notes, [needs._HTML_SWALLOWED])

    def test_empty_hidden_elements_and_dropped_tags_are_not_reported(self):
        for markup in (
            "<div hidden></div><p>visible</p>",
            "<div hidden>  \n </div><p>visible</p>",
            "<script>var a</script><style>p{}</style><p>visible</p>",
            "<head><title>T</title></head><p>visible</p>",
        ):
            with self.subTest(markup=markup):
                self.assertEqual(convert(markup), ("visible", []))

    def test_reaching_the_output_limit_is_reported(self):
        text, notes = convert("<p>" + "x" * 40 + "</p>", 20)
        self.assertEqual(text, "x" * 20)
        self.assertEqual(notes, [needs._HTML_TRUNCATED])

    def test_text_that_fits_exactly_is_not_reported(self):
        self.assertEqual(convert("<p>" + "x" * 20 + "</p>", 20), ("x" * 20, []))

    def test_leading_whitespace_does_not_use_up_the_limit(self):
        text, notes = convert("<p>\n  \n  </p>" + " " * 500 + "<p>readable</p>", 20)
        self.assertEqual((text, notes), ("readable", []))
        text, notes = convert("​" * 100 + "<p>readable text</p>", 20)
        self.assertEqual((text, notes), ("readable text", []))

    def test_whitespace_runs_count_once(self):
        text, notes = convert("<p>" + "a    b" * 5 + "</p>", 12)
        self.assertEqual(text, "a ba ba ba ba b"[:12])
        self.assertEqual(notes, [needs._HTML_TRUNCATED])


class JoinersSurvive(unittest.TestCase):
    def test_zero_width_joiners_that_spell_words_and_emoji_are_kept(self):
        persian = "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"
        family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
        text, _ = convert(f"<p>{persian}</p><p>{family}</p>")
        self.assertIn(persian, text)
        self.assertIn(family, text)

    def test_other_invisible_format_characters_are_still_dropped(self):
        text, _ = convert("<p>a\u200bb\u202ec\ufeffd</p>")
        self.assertEqual(text, "abcd")


class StyleWorkIsBudgeted(unittest.TestCase):
    def test_many_rules_against_many_elements_stop_at_a_budget_and_say_so(self):
        from unittest.mock import patch

        calls = []
        real = needs._InertText._matches

        def counting(self, *args):
            calls.append(1)
            return real(self, *args)

        css = "p{display:block}" * 2000
        with patch.object(needs._InertText, "_matches", counting):
            text, notes = convert("<style>" + css + "</style>" + "<p>x" * 1500)
        self.assertLessEqual(len(calls), 400_000)
        self.assertIn("x", text)
        self.assertIn(needs._HTML_STYLE_NOTE, notes)

    def test_deep_ancestor_walks_count_against_the_same_budget(self):
        from unittest.mock import patch

        calls = []
        real = needs._InertText._frame_is

        def counting(*args):
            calls.append(1)
            return real(*args)

        css = ".a .b .c .d{display:none}" * 100
        with patch.object(needs._InertText, "_frame_is", staticmethod(counting)):
            _, notes = convert("<style>" + css + "</style>" + "<div class=a>" * 200 + "<p class=d>x</p>" * 200)
        self.assertLessEqual(len(calls), 400_000)
        self.assertIn(needs._HTML_STYLE_NOTE, notes)

    def test_a_small_stylesheet_is_not_reported(self):
        self.assertEqual(convert("<style>p{display:block}</style><p>x</p>"), ("x", []))


class EndToEnd(OutsideFixture):
    def text(self, html):
        self.watch()
        for p in self.folder.glob("*.eml"):
            p.unlink()
        (self.folder / "m.eml").write_bytes(mail(html, None).as_bytes())
        (item,) = needs.build_queue()["items"]
        return item["text"]

    def test_the_card_carries_the_notices(self):
        t = self.text("<div hidden>omitted</div><p>visible</p>")
        self.assertIn("visible", t)
        self.assertIn(NOT_SHOWN, t)

    def test_omitted_head_text_reaches_the_card(self):
        self.assertIn("VISIBLE", self.text("<head><title>Title</title>VISIBLE"))


class Docs(unittest.TestCase):
    def test_the_guide_describes_reported_hidden_text_and_the_output_limit(self):
        guide = (ROOT / "docs" / "operator-guide.md").read_text(encoding="utf-8")
        flat = " ".join(guide.split())
        self.assertIn("Hidden text is reported even when the element closes", flat)
        self.assertIn("longer than the card shows", flat)
        self.assertIn(CUT, " ".join(needs._HTML_TRUNCATED.split()))
        self.assertIn("Invalid declarations are discarded", flat)


if __name__ == "__main__":
    unittest.main()
