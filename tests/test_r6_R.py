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


if __name__ == "__main__":
    unittest.main()
