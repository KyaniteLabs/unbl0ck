"""#73: a cut chat message is redacted with the text before its cut, so a credential whose label lies
before the cut is still redacted."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_ask_coverage import ASK, make_chats, settings
from test_outside import needs

SECRET = "orange-tulip-42"


def _cut_between_label_and_value(label):
    end = f" {SECRET} and then some notes.\n" + ASK
    end = " " * (needs.CHAT_READ_CHARS - len(end)) + end  # the read end starts right after the label
    return "x " * (needs.CHAT_READ_BYTES // 2) + label + end


class CutLabel(unittest.TestCase):
    def test_a_value_just_past_the_cut_is_redacted_by_its_label_before_it(self):
        for label in ("password:", "DB_PASSWORD=", "Authorization: Bearer"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as d, patch.object(
                needs, "_settings", settings()
            ):
                home = make_chats(Path(d), [("s0", "cli", "dm", "u", 1, _cut_between_label_and_value(label))])
                (item,) = needs.chat_items(home, full=True)
                self.assertTrue(item["ask_cut"])
                self.assertNotIn(SECRET, item["ask"] + item["text"])
                self.assertIn("Should I open the PR now?", item["ask"])

    def test_text_before_the_cut_never_shows_when_redaction_shortens_the_read_end(self):
        """#73 review: a quoted value that starts before the cut and runs past it redacts to a short
        placeholder; the read end must not then be refilled with text from before the cut."""
        before = "PRIVATE_NOTE\npassword=\""
        message = "x " * (needs.CHAT_READ_BYTES // 2) + before + "a " * 50010 + "\"\n" + ASK
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "_settings", settings()):
            home = make_chats(Path(d), [("s0", "cli", "dm", "u", 1, message)])
            (item,) = needs.chat_items(home, full=True)
            self.assertNotIn("PRIVATE_NOTE", item["ask"] + item["text"])
            self.assertIn("Should I open the PR now?", item["ask"])


if __name__ == "__main__":
    unittest.main()
