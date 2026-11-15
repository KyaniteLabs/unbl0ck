"""Round 5, lane H: HTML mail text is never deleted by a guess about hiding, every left-out or cut part
is noted."""

import unittest

from test_outside import needs

UNSURE = "not evaluated"
NOT_SHOWN = "Part of this HTML body is not shown"


def convert(markup, limit=1000):
    text, notes = needs._html_convert(markup, limit)
    return text, " ".join(notes)


class HiddenTextIsNotGuessed(unittest.TestCase):
    def assertShownOrNoted(self, markup, word="HIDDEN"):
        text, notes = convert(markup)
        self.assertTrue(word not in text or UNSURE in notes, (text, notes))

    def test_a_stylesheet_that_disagrees_with_itself_is_noted(self):
        """R5-needs-033: an invalid later declaration must not silently undo a valid display:none."""
        self.assertShownOrNoted("<style>p{display:none;display:invalid}</style><p>HIDDEN</p>")

    def test_a_descendant_selector_the_converter_cannot_follow_is_noted(self):
        """R5-needs-034: `.a > .b .c` matches through the outer `.b`; text it hides is never shown unnoted."""
        self.assertShownOrNoted(
            "<style>.a > .b .c{display:none}</style>"
            "<div class=a><div class=b><div><div class=b><p class=c>HIDDEN</p></div></div></div></div>"
        )

    def test_an_impossible_selector_does_not_delete_visible_text(self):
        """R5-needs-035: `#a#b` matches nothing, so the paragraph stays."""
        self.assertIn("VISIBLE", convert('<style>#a#b{display:none}</style><p id="b">VISIBLE</p>')[0])

    def test_initial_resets_inherited_visibility_and_font_size(self):
        """R5-needs-036: `initial` is a reset, not an unknown value."""
        for markup in (
            '<div style="visibility:hidden"><span style="visibility:initial">VISIBLE</span></div>',
            '<div style="font-size:0"><span style="font-size:initial">VISIBLE</span></div>',
        ):
            with self.subTest(markup=markup):
                self.assertIn("VISIBLE", convert(markup)[0])

    def test_styles_inside_a_template_do_not_hide_the_page(self):
        """R5-needs-037: a template's content is inert, its <style> included."""
        self.assertIn("VISIBLE", convert("<template><style>p{display:none}</style></template><p>VISIBLE</p>")[0])

    def test_body_text_after_an_omitted_head_end_is_shown(self):
        """R5-needs-038: text after the head's content starts the body even with no start tag."""
        self.assertIn("VISIBLE", convert("<head><title>Title</title>VISIBLE")[0])

    def test_a_new_table_section_ends_a_hidden_one(self):
        """R5-needs-039: the second <tbody> implies the end of the hidden first one."""
        text, notes = convert("<table><tbody hidden><tr><td>HIDDEN<tbody><tr><td>VISIBLE</table>")
        self.assertIn("VISIBLE", text)
        self.assertTrue("HIDDEN" not in text or UNSURE in notes, (text, notes))

    def test_text_moved_out_of_a_hidden_table_row_is_shown(self):
        """R5-needs-039 (sweep): text directly in a table row is placed before the table by HTML."""
        self.assertIn("VISIBLE", convert("<table><tr hidden>VISIBLE<td>cell</td></tr></table>")[0])

    def test_hidden_attribute_with_an_inline_display_is_shown(self):
        """R5-needs-035 (sweep): an inline display value overrides the hidden attribute."""
        self.assertIn("VISIBLE", convert('<div hidden style="display:block">VISIBLE</div>')[0])


class LeftOutTextIsNoted(unittest.TestCase):
    def test_a_closed_hidden_element_is_noted(self):
        """R5-needs-040: the docs promise a note for hidden text, closed or not."""
        text, notes = convert("<div hidden>omitted</div><p>visible</p>")
        self.assertEqual(text, "visible")
        self.assertIn(NOT_SHOWN, notes)

    def test_a_cut_at_the_limit_is_noted_and_whitespace_does_not_spend_it(self):
        """R5-needs-041: reaching the limit says so, and emitted spacing cannot use up the limit."""
        text, notes = convert("<p>" + "x" * 40 + "</p>", 20)
        self.assertEqual(text, "x" * 20)
        self.assertIn(NOT_SHOWN, notes)
        text, notes = convert("<div>" * 30 + "<p>hello</p>", 20)
        self.assertEqual(text, "hello")


if __name__ == "__main__":
    unittest.main()
