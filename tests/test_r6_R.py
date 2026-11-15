"""Round-6 lane R: one numeric-code classifier on every Ask path, inert elements inside uncertain
hidden ones, unknown inline display values beside ``hidden``, and colliding scrubbed keys."""

import unittest

import needs


class NumericCodesOnEveryAskPath(unittest.TestCase):
    def test_a_code_named_by_a_name_value_pair_or_a_yaml_block_is_redacted(self):
        """R6-002: ``{"name":"otp","value":123456}`` and ``otp: |`` blocks kept the code; counts survive."""
        self.assertEqual(needs._ask_redact_value({"name": "otp", "value": 123456})["value"], "[redacted]")
        self.assertEqual(needs._ask_redact_value({"key": "login_pin", "val": "2468"})["val"], "[redacted]")
        for text in (
            'log {"name":"otp","value":123456} end',
            "otp: |\n  123456\n",
            "passcode: >-\n  98765432\n",
            "name: otp\nvalue: 123456\n",
            "{name: totp, value: 654321}",
            "? otp\n: 123456\n",
            "--otp 123456",
            "--pin=1234",
        ):
            with self.subTest(text=text):
                out = needs._ask_redact(text)
                self.assertIn("[redacted]", out)
                self.assertNotRegex(out, r"\d{4,}")
        # Prose, short counts and longer names still survive on those paths.
        self.assertEqual(needs._ask_redact_value({"name": "otp", "value": 12}), {"name": "otp", "value": 12})
        self.assertEqual(needs._ask_redact_value({"name": "pin_count", "value": 1234})["value"], 1234)
        for text in (
            "pin: |\n  the version\n",
            "pin_count: |\n  1234\n",
            "name: pin\nvalue: 3\n",
            '{"name":"pin_count","value":1234}',
            "pin the version; bearer of bad news",
            "--pin 12",
            "run --pin-count 1234",
        ):
            with self.subTest(text=text):
                self.assertEqual(needs._ask_redact(text), text)


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
