"""Round-7 lane R regression tests: one per finding, each driven through an entry point the
round-6 code already had, so it fails there for the behavioural reason."""

import json
import time
import unittest
from unittest.mock import patch

import test_r4_stack11 as s11
from test_outside import needs


class ExplicitKeyBlockScalar(unittest.TestCase):
    def test_an_explicit_key_yaml_block_scalar_code_is_redacted(self):
        """R7-003: ``? otp`` / ``: |`` hides the code in the block, alone or inside a card body."""
        for text in (
            "? otp\n: |\n  123456\n",
            "Login help\n  ? otp\n  : >-\n    123456\nthanks\n",
            "- ? otp\n  : |\n    123456\n",
            '? pin\n: "123456"\n',
        ):
            out = needs._ask_redact(text)
            self.assertNotIn("123456", out, out)
            self.assertIn("[redacted]", out)
        self.assertIn("thanks", needs._ask_redact("? otp\n: |\n  123456\nthanks\n"))


class InvalidDisplayCombination(unittest.TestCase):
    def test_an_invalid_display_keyword_combination_does_not_show_hidden_text(self):
        """R7-012: ``display:block block`` is not a display value, so ``hidden`` still applies."""
        for value in ("block block", "flex grid", "table-row block", "inline list-item table"):
            text, notes = needs._html_convert(f'<div hidden style="display:{value}">HIDDEN</div>', 1000)
            self.assertTrue("HIDDEN" not in text or notes, (value, text, notes))
        for value in ("block flex", "inline flow-root", "block flow list-item", "list-item"):
            self.assertIn("SHOWN", needs.html_to_text(f'<div hidden style="display:{value}">SHOWN</div>', 1000))


class LostDismissalCounter(s11.QueueCase):
    def test_a_lost_counter_restarts_above_stored_orders_and_refuses_an_incomplete_scan(self):
        """R7-011: a missing counter restarts above every stored order; a scan it cannot finish,
        or a malformed counter, refuses the dismissal."""
        path = needs.data_dir(self.home) / needs.ORDER_FILE
        with patch.object(needs.time, "time", lambda: 1000.0):
            needs.hide_ordered("kanban:b:t", "1", self.home)
            _, last = needs.hide_ordered("kanban:b:t", "1", self.home)
            for damage in (None, {}):
                path.unlink(missing_ok=True) if damage is None else path.write_text(json.dumps(damage))
                _, order = needs.hide_ordered("kanban:b:t", "1", self.home)
                self.assertGreater(order, last, damage)
                last = order
            path.write_text(json.dumps({"last": "damaged"}))
            with self.assertRaisesRegex(RuntimeError, needs.ORDER_FILE):
                needs.hide_ordered("kanban:b:t", "1", self.home)
            path.unlink()
            outside = needs._outside_dir(self.home)
            (outside / "broken.json").write_text("{not json")
            with self.assertRaisesRegex(RuntimeError, needs.ORDER_FILE):
                needs.hide_ordered("kanban:b:t", "1", self.home)
            (outside / "broken.json").unlink()
        # A pre-order install (bare versions, no counter) still dismisses at the clock.
        needs._hidden_path(self.home).write_text(json.dumps({"kanban:b:old": "1"}))
        _, order = needs.hide_ordered("kanban:b:t", "1", self.home)
        self.assertGreaterEqual(order, int(time.time() * 1000) - 60_000)


if __name__ == "__main__":
    unittest.main()
