"""Round-8 lane S regression tests: one per finding, each driven through an entry point the
round-7 code already had, so it fails there for the behavioural reason."""

import unittest

from test_outside import needs


class CssWhitespaceOnly(unittest.TestCase):
    def test_a_display_value_split_by_non_css_whitespace_does_not_show_hidden_text_silently(self):
        """R8-012: NBSP, vertical tab, em space or a Unicode case fold do not make a real display value."""
        for value in ("block flex", "block\u000bflex", "block flex", " block", "blocK"):
            text, notes = needs._html_convert(f'<div hidden style="display:{value}">HIDDEN</div>', 1000)
            self.assertTrue("HIDDEN" not in text or notes, (value, text, notes))
        for value in ("block\tflex", "block\fflex", "BLOCK", "block !important"):
            self.assertEqual(needs._html_convert(f'<div hidden style="display:{value}">SHOWN</div>', 1000), ("SHOWN", []))


if __name__ == "__main__":
    unittest.main()
