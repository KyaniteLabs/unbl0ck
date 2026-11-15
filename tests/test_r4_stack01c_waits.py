"""Round-4 findings 01-058, 01-061, 01-062, 01-063, 01-064: live-prompt records, their excerpt,
their version and what happens when the hook cannot write or clear them."""

import json
import os
import re
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from r4c_common import RID, HomeCase, needs

DOC_ROOT = Path(__file__).resolve().parents[1]


class WaitCase(HomeCase):
    def setUp(self):
        super().setUp()
        needs._WAIT_FAULTS.clear()
        self.addCleanup(needs._WAIT_FAULTS.clear)

    def record(self, prompt, kind="clarify", rid=RID, **extra):
        needs.record_wait(
            self.home, request_id=rid, kind=kind, prompt=prompt, platform="cli", **extra
        )

    def item(self, **kw):
        (item,) = needs.wait_items(self.home, **kw)
        return item

    def rewrite(self, **changes):
        path = needs._waits_dir(self.home) / f"{RID}.json"
        rec = json.loads(path.read_text())
        rec.update(changes)
        path.write_text(json.dumps(rec))


class ExcerptIsTheTail(WaitCase):
    def test_a_long_prompt_shows_its_real_last_characters(self):
        """01-058 (class I): the excerpt is the last 480 characters of the whole prompt."""
        self.record("x" * 21000 + " FINAL QUESTION?")
        item = self.item()
        self.assertTrue(item["ask"].endswith("FINAL QUESTION?"), item["ask"][-40:])
        self.assertTrue(item["ask_truncated"])

    def test_a_password_prompt_excerpt_is_the_tail_but_never_expanded(self):
        self.record("y" * 9000 + " Password for admin:", kind="sudo")
        item = self.item(full=True)
        self.assertTrue(item["ask"].endswith("Password for admin:"))
        self.assertNotIn("text", item)
        with self.assertRaises(PermissionError):
            needs.source_text(item["id"], item["version"])

    def test_a_short_prompt_is_unchanged(self):
        self.record("Pick one?")
        item = self.item(full=True)
        self.assertEqual((item["ask"], item["text"], item["ask_truncated"]), ("Pick one?", "Pick one?", False))

    def test_the_docs_still_say_the_last_480_characters(self):
        guide = (DOC_ROOT / "docs" / "operator-guide.md").read_text()
        self.assertIn("last 480 characters", guide)
        self.assertIn("last 2,000 characters kept beside it", guide)
        self.assertIn(f"{needs.WAIT_TAIL_CHARS:,}", guide)


class CutPromptsAreMarked(WaitCase):
    def test_a_prompt_cut_by_the_store_marks_the_ask_truncated(self):
        """01-063 (class E): ask_truncated follows prompt_cut, so Desktop fetches and warns."""
        self.record("A" + " " * 21000 + " QUESTION?")
        item = self.item(full=True)
        self.assertTrue(item["ask_truncated"])
        self.assertTrue(item["text_cut"])

    def test_a_legacy_record_with_a_4000_prefix_is_marked_cut(self):
        """01-064 (class E): an older record has no prompt_cut field; a full-length prefix is a cut one."""
        self.record("short")
        self.rewrite(prompt="p" * 4000)
        path = needs._waits_dir(self.home) / f"{RID}.json"
        rec = json.loads(path.read_text())
        rec.pop("prompt_cut", None)
        path.write_text(json.dumps(rec))
        item = self.item(full=True)
        self.assertTrue(item["ask_truncated"])
        self.assertTrue(item["text_cut"])
        self.assertTrue(needs.source_text(item["id"], item["version"])["truncated"])

    def test_a_legacy_short_record_is_not_marked_cut(self):
        self.record("short")
        path = needs._waits_dir(self.home) / f"{RID}.json"
        rec = json.loads(path.read_text())
        rec.pop("prompt_cut", None)
        path.write_text(json.dumps(rec))
        item = self.item(full=True)
        self.assertFalse(item["ask_truncated"] or item["text_cut"])


class VersionNamesTheRecord(WaitCase):
    def test_the_version_is_stable_for_one_record(self):
        self.record("Pick one?")
        self.assertEqual(self.item()["version"], self.item()["version"])

    def test_replacing_the_text_changes_the_version(self):
        """01-061 (class A): same request id, new prompt: the old token must not open it."""
        self.record("Pick one?")
        before = self.item()
        self.rewrite(prompt="Delete everything?")
        after = self.item()
        self.assertNotEqual(before["version"], after["version"])
        with self.assertRaises(needs.StaleAnswer):
            needs.source_text(before["id"], before["version"])
        self.assertEqual(needs.source_text(after["id"], after["version"])["text"], "Delete everything?")

    def test_changing_profile_or_session_changes_the_version(self):
        self.record("Pick one?", session_id="a")
        base = self.item()["version"]
        for change in ({"profile": "other"}, {"session_id": "b"}, {"platform": "discord"}):
            self.rewrite(**change)
            self.assertNotEqual(self.item()["version"], base, change)
            self.rewrite(profile="default", session_id="a", platform="cli")
            self.assertEqual(self.item()["version"], base)


class HookFailuresAreVisible(WaitCase):
    def test_a_failed_write_is_reported_then_reconciled(self):
        """01-062 (class G): the wait could not be recorded; polling says so and retries it."""
        with patch.object(needs, "_write_json", side_effect=OSError("disk full")):
            self.record("Pick one?")
        errors = []
        needs.wait_items(self.home, errors=errors)  # storage is back: the retry succeeds
        self.assertEqual(errors, [])
        self.assertEqual(self.item()["ask"], "Pick one?")

    def test_an_unrecoverable_write_stays_in_errors(self):
        with patch.object(needs, "_write_json", side_effect=OSError("disk full")):
            self.record("Pick one?")
            errors = []
            self.assertEqual(needs.wait_items(self.home, errors=errors), [])
        self.assertTrue(any(e.startswith("prompt") and RID in e for e in errors), errors)
        queue_errors = []
        with patch.object(needs, "_write_json", side_effect=OSError("disk full")):
            needs.wait_items(self.home, errors=queue_errors)
        self.assertTrue(queue_errors)

    def test_a_failed_clear_is_retried_not_left_listed(self):
        self.record("Pick one?")
        with patch.object(needs, "_unlink_wait", side_effect=OSError("busy")):
            needs.clear_wait(self.home, request_id=RID)
        self.assertEqual(needs.wait_items(self.home), [])  # the retry removes it first

    def test_an_unrecoverable_clear_is_reported(self):
        self.record("Pick one?")
        with patch.object(needs, "_unlink_wait", side_effect=OSError("busy")):
            needs.clear_wait(self.home, request_id=RID)
            errors = []
            needs.wait_items(self.home, errors=errors)
        self.assertTrue(any(RID in e for e in errors), errors)

    def test_the_fault_list_is_bounded(self):
        with patch.object(needs, "_write_json", side_effect=OSError("disk full")):
            for n in range(200):
                self.record("q?", rid=f"{n:016x}")
        self.assertLessEqual(len(needs._WAIT_FAULTS), 50)

    def test_a_clean_hook_leaves_no_fault(self):
        self.record("Pick one?")
        needs.clear_wait(self.home, request_id=RID)
        self.assertEqual(needs._WAIT_FAULTS, {})


if __name__ == "__main__":
    unittest.main()
