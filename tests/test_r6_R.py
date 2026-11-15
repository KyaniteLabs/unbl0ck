"""Round-6 lane R: one numeric-code classifier on every Ask path, inert elements inside uncertain
hidden ones, unknown inline display values beside ``hidden``, and colliding scrubbed keys."""

import unittest

import needs


class HtmlHiding(unittest.TestCase):
    def test_a_script_inside_an_uncertain_hidden_element_is_still_dropped(self):
        """R6-011: the script inherited the hidden ancestor's doubt and its text was shown."""
        text, _ = needs._html_convert(
            "<div hidden><script>SECRET</script></div><style>div{display:block}</style>", 1000
        )
        self.assertNotIn("SECRET", text)

    def test_an_unknown_display_value_does_not_silently_override_hidden(self):
        """R6-012: ``display:bogus`` showed hidden text with no note."""
        text, notes = needs._html_convert('<div hidden style="display:bogus">HIDDEN</div>', 1000)
        self.assertTrue("HIDDEN" not in text or needs._HTML_STYLE_NOTE in notes, (text, notes))
        text, notes = needs._html_convert('<div hidden style="display:block">SHOWN</div>', 1000)
        self.assertEqual((text, notes), ("SHOWN", []))


if __name__ == "__main__":
    unittest.main()
