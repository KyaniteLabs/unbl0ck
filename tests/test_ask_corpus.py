"""Ask detection corpus and report (issue #40).

Run ``python tests/test_ask_corpus.py`` to print the false-positive / false-negative report."""

import collections
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from ask_corpus import CASES, KNOWN_GAPS, PRINCIPAL  # noqa: E402
from test_needs import make_home, needs  # noqa: E402


def detect(det, text, kind):
    if det == "chat":
        return needs.chat_asks_you(text)
    with patch.object(needs, "_settings", lambda: {"principal_name": PRINCIPAL}):
        return bool(needs.kanban_needs_you(text, kind))


def report():
    """{'false_positives': [...], 'false_negatives': [...], 'counts': {...}} over the corpus."""
    fp, fn = [], []
    for cid, det, text, kind, expected, tags in CASES:
        got = detect(det, text, kind)
        if got and not expected:
            fp.append(cid)
        elif expected and not got:
            fn.append(cid)
    return {
        "cases": len(CASES),
        "false_positives": sorted(fp),
        "false_negatives": sorted(fn),
        "by_tag": dict(collections.Counter(t for c in CASES for t in c[5])),
    }


class Corpus(unittest.TestCase):
    def test_every_case_is_unique_and_well_formed(self):
        ids = [c[0] for c in CASES]
        self.assertEqual(len(ids), len(set(ids)))
        for cid, det, text, kind, expected, tags in CASES:
            self.assertIn(det, ("chat", "kanban"), cid)
            self.assertIsInstance(expected, bool, cid)
            self.assertGreaterEqual(len(tags), 4, cid)
        self.assertTrue(KNOWN_GAPS <= set(ids))

    def test_corpus_spans_sources_platforms_languages_and_shapes(self):
        tags = set(t for c in CASES for t in c[5])
        self.assertTrue({"chat", "kanban"} == {c[1] for c in CASES})
        self.assertTrue({"cli", "desktop", "discord", "telegram", "kanban"} <= tags)
        self.assertTrue({"en", "es", "fr", "de", "ja"} <= tags)
        self.assertTrue({"named", "role", "none"} <= tags)
        self.assertTrue(
            {"direct", "indirect", "group", "packet", "report", "relayed", "historical", "self_answered", "signoff", "stuck"} <= tags
        )

    def test_both_error_directions_are_reported_and_cases_exist_for_each(self):
        r = report()
        self.assertTrue(r["false_negatives"], "known misses are listed, not hidden")
        self.assertTrue(r["false_positives"], "known false alarms are listed, not hidden")
        self.assertEqual(r["cases"], len(CASES))

    def test_misses_are_exactly_the_known_gaps(self):
        r = report()
        actual = set(r["false_positives"]) | set(r["false_negatives"])
        self.assertEqual(
            actual,
            KNOWN_GAPS,
            "a case that now passes must leave KNOWN_GAPS; a new miss is a regression",
        )

    def test_named_cases_contain_the_configured_name_and_fail_without_it(self):
        """11-009: breaking literal principal-name matching must not still pass the corpus."""
        named = [c for c in CASES if "named" in c[5] and c[1] == "kanban"]
        literal = [c for c in named if PRINCIPAL.lower() in c[2].lower()]
        self.assertGreaterEqual(len(literal), 6)
        self.assertTrue(any(c[4] for c in literal) and any(not c[4] for c in literal))
        with patch.object(needs, "principal_names", lambda settings=None: []):
            needs._PATTERN_CACHE.clear()
            broken = {c[0] for c in literal if detect(c[1], c[2], c[3]) != c[4]}
        needs._PATTERN_CACHE.clear()
        self.assertTrue(
            {"kb-named-gate", "kb-named-waiting", "kb-named-possessive", "kb-named-input"} <= broken,
            "the corpus must go red when the configured name stops matching",
        )
        self.assertEqual({c[0] for c in literal if detect(c[1], c[2], c[3]) != c[4]}, set())

    def test_already_fixed_protections_stay_fixed(self):
        # Ask vs stuck, relayed orders, historical packets, self-answered questions, sign-offs.
        protected = [c for c in CASES if "relayed" in c[5] or "historical" in c[5]
                     or "self_answered" in c[5] or "signoff" in c[5] or "stuck" in c[5]]
        self.assertGreaterEqual(len(protected), 8)
        for cid, det, text, kind, expected, tags in protected:
            if cid in KNOWN_GAPS:
                continue
            self.assertEqual(detect(det, text, kind), expected, cid)


if __name__ == "__main__":
    if "--report" in sys.argv or len(sys.argv) == 1 and sys.stdout.isatty():
        r = report()
        print(f"cases {r['cases']}")
        print("false positives:", ", ".join(r["false_positives"]) or "none")
        print("false negatives:", ", ".join(r["false_negatives"]) or "none")
    else:
        unittest.main()
