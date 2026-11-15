"""Round 7, lane B: behaviour the other round-5 fix set had and this one lacked."""

import unittest

from test_r5_H import convert


class HtmlKeepsJoiners(unittest.TestCase):
    """The HTML converter removes invisible format characters, which can hide text, but the
    zero-width joiner and non-joiner are part of how words and emoji are spelled."""

    def test_joiners_that_spell_words_and_emoji_are_kept(self):
        persian = "می‌خواهم"  # ZWNJ inside one word
        family = "\U0001f468‍\U0001f469‍\U0001f467"  # ZWJ emoji sequence
        text, _ = convert(f"<p>{persian}</p><p>{family}</p>")
        self.assertIn(persian, text)
        self.assertIn(family, text)

    def test_other_invisible_format_characters_are_still_dropped(self):
        text, _ = convert("<p>a​b‮c﻿d⁦e</p>")
        self.assertEqual(text, "abcde")


if __name__ == "__main__":
    unittest.main()
