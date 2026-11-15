"""Round-5 documentation findings: each claim is pinned to the behavior it describes."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from test_outside import needs

ROOT = Path(__file__).resolve().parents[1]
ARCH = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
GUIDE = (ROOT / "docs/operator-guide.md").read_text(encoding="utf-8")


class DocsStateTheCompatibilityExceptions(unittest.TestCase):
    def test_a_legacy_summary_without_a_fingerprint_attaches_and_the_doc_says_so(self):
        """R5-desk-014 (class I): the cache guarantee names the entry saved before the hash existed."""
        item = {"id": "kanban:b:t", "version": "1", "title": "Old title", "ask": "q", "owner": "o", "source": "kanban"}
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            data = needs.data_dir(home)
            data.mkdir(parents=True, exist_ok=True)
            (data / "bluf.json").write_text(
                json.dumps({"kanban:b:t": {"version": "1", "bluf": "Old summary", "why": ""}}), encoding="utf-8"
            )
            edited = dict(item, title="A different question entirely")
            needs.attach_blufs([edited], home)
            self.assertEqual(edited.get("bluf"), "Old summary")
        self.assertIn("saved before that hash existed is bound to the item version alone", ARCH)

    def test_release_without_a_marker_when_the_store_is_full_is_documented(self):
        """R5-needs-051 (class I): a full marker store never refuses the answer."""
        self.assertIn("when the store is full the answer is still posted without a marker", ARCH)
        self.assertNotIn("is always recoverable", ARCH)
        self.assertIn("A full store is not a reason to refuse the answer", (ROOT / "needs.py").read_text(encoding="utf-8"))

    def test_a_planted_temp_link_is_removed_not_refused_and_the_doc_says_so(self):
        """R5-needs-053 (class I): the write continues over a private file; the link target is untouched."""
        self.assertIn("is removed, never followed, and the write continues", ARCH)
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            data = needs.data_dir(home)
            data.mkdir(parents=True, exist_ok=True)
            victim = home / "victim.txt"
            victim.write_text("keep me", encoding="utf-8")
            target = data / "hidden.json"
            tmp = target.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.symlink_to(victim)
            needs.hide("chat:p:s", "1", home)  # succeeds: the link is removed, not an error
            self.assertEqual(victim.read_text(encoding="utf-8"), "keep me")
            self.assertFalse(target.is_symlink())
            self.assertEqual(needs.load_hidden(home), {"chat:p:s": "1"})


class OperatorGuideStatesWhichFollowUpsStay(unittest.TestCase):
    def test_only_a_client_error_other_than_busy_or_timeout_rolls_the_follow_up_back(self):
        """R5-desk-030 (class I): the client keeps a follow-up after 429 and 408, so the guide says so."""
        self.assertIn("a client error other than busy or timeout", GUIDE)
        self.assertIn("After a busy (429) or timeout answer it stays", GUIDE)
        src = (ROOT / "desktop/src/plugin.src.js").read_text(encoding="utf-8")
        self.assertIn("status !== 408 && status !== 429", src)


if __name__ == "__main__":
    unittest.main()
