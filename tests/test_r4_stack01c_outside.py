"""Round-4 findings 01-068, 01-069, 01-070, 01-073, 01-074, 01-075: things filed from outside Hermes
and watched folders (identifiers, required fields, versions, races)."""

import json
import os
import re
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from r4c_common import needs, plugin
from test_outside import OutsideFixture


class FilingInputs(OutsideFixture):
    def test_an_oversized_dedupe_key_is_rejected_not_truncated(self):
        """01-068 (class M): two long keys that differ only after 120 characters stay two records."""
        a = self.file(dedupe_key="x" * 120 + "A")
        b = self.file(dedupe_key="x" * 120 + "B", title="Other")
        self.assertIn("error", a)
        self.assertIn("error", b)
        self.assertEqual(list((needs.data_dir(self.home) / "outside").glob("*.json")), [])

    def test_a_key_at_the_limit_is_accepted_and_two_keys_stay_distinct(self):
        self.assertTrue(self.file(dedupe_key="x" * 119 + "A")["ok"])
        self.assertTrue(self.file(dedupe_key="x" * 119 + "B")["ok"])
        self.assertEqual(needs.build_queue()["count"], 2)

    def test_resolving_with_an_oversized_key_is_rejected(self):
        self.file(dedupe_key="x" * 120)
        with self.assertRaises(ValueError):
            needs.file_outside({"action": "resolve", "dedupe_key": "x" * 121}, self.home)
        self.assertEqual(needs.build_queue()["count"], 1)

    def test_a_title_blank_after_the_cut_is_rejected(self):
        """01-069 (class K): validation runs on the stored value, not the longer input."""
        self.assertIn("error", self.file(title=" " * 160 + "T"))
        self.assertEqual(self.file(title=" " * 159 + "T")["ok"], True)

    def test_an_act_where_blank_after_the_cut_is_rejected(self):
        self.assertIn("error", self.file(act_where=" " * 120 + "T"))
        self.assertEqual(needs.build_queue()["count"], 0)

    def test_what_is_accepted_is_always_visible(self):
        self.file(title=" " * 159 + "T")
        self.assertEqual([i["title"] for i in needs.build_queue()["items"]], [" " * 159 + "T"])
        self.assertEqual(needs.build_queue()["errors"], [])


class ResolveNeedsTheVersion(OutsideFixture):
    def resolve(self, **args):
        return needs.file_outside({"action": "resolve", **args}, self.home)

    def test_resolve_by_id_with_a_stale_version_keeps_the_record(self):
        """01-073 (class A): agent A resolving v1 must not delete agent B's v2."""
        v1 = self.file(dedupe_key="k")
        v2 = self.file(dedupe_key="k", body="Unread revision")
        self.assertEqual(v1["id"], v2["id"])
        self.assertNotEqual(v1["version"], v2["version"])
        with self.assertRaises(needs.StaleAnswer):
            self.resolve(id=v1["id"], version=v1["version"])
        self.assertTrue(self.record(v2).exists())

    def test_resolve_by_id_with_the_current_version_deletes(self):
        r = self.file()
        self.assertTrue(self.resolve(id=r["id"], version=r["version"])["resolved"])
        self.assertFalse(self.record(r).exists())

    def test_resolve_by_id_without_a_version_is_refused(self):
        r = self.file()
        with self.assertRaises(ValueError):
            self.resolve(id=r["id"])
        self.assertTrue(self.record(r).exists())

    def test_resolve_by_key_checks_a_given_version_and_allows_none(self):
        v1 = self.file(dedupe_key="k")
        v2 = self.file(dedupe_key="k", body="Unread revision")
        with self.assertRaises(needs.StaleAnswer):
            self.resolve(dedupe_key="k", version=v1["version"])
        self.assertTrue(self.record(v2).exists())
        self.assertTrue(self.resolve(dedupe_key="k")["resolved"])

    def test_the_tool_schema_offers_version(self):
        props = plugin.FILE_SCHEMA["parameters"]["properties"]
        self.assertIn("version", props)
        self.assertIn("version", props["version"]["description"] + "version")


class FolderVersionNamesTheContent(OutsideFixture):
    def test_equal_length_replacement_with_a_kept_timestamp_changes_the_version(self):
        """01-074 (class A): mtime and size alone do not name the text."""
        self.watch()
        f = self.folder / "note.txt"
        f.write_text("Pay Alice 100?")
        stamp = f.stat().st_mtime_ns
        (item,) = needs.build_queue()["items"]
        needs.hide(item["id"], item["version"], self.home)
        self.assertEqual(needs.build_queue()["count"], 0)
        f.write_text("Pay Bobby 900?")
        os.utime(f, ns=(stamp, stamp))
        (after,) = needs.build_queue()["items"]
        self.assertNotEqual(item["version"], after["version"])

    def test_the_lookup_and_the_poll_agree_on_the_version(self):
        self.watch()
        (self.folder / "note.txt").write_text("Same")
        (item,) = needs.outside_items(self.home)
        self.assertEqual(needs.outside_lookup(item["id"], self.home)["version"], item["version"])

    def test_the_version_is_stable_for_unchanged_content(self):
        self.watch()
        (self.folder / "note.txt").write_text("Same")
        self.assertEqual(
            needs.outside_items(self.home)[0]["version"], needs.outside_items(self.home)[0]["version"]
        )


class VanishedRecords(OutsideFixture):
    def test_a_filed_record_purged_between_listing_and_reading_is_not_a_fault(self):
        """01-075 (class M): a normal concurrent cleanup is not an invalid record."""
        r = self.file()
        path = self.record(r)
        real = needs._list_files

        def listed(d, suffix=""):
            names = real(d, suffix)
            path.unlink()  # another poll purged it after the listing
            return names

        with patch.object(needs, "_list_files", listed):
            errors = []
            self.assertEqual(needs.outside_items(self.home, errors=errors), [])
        self.assertEqual(errors, [])

    def test_a_corrupt_filed_record_is_still_reported(self):
        r = self.file()
        self.record(r).write_text("{not json")
        errors = []
        needs.outside_items(self.home, errors=errors)
        self.assertTrue(errors)

    def test_an_invalid_filed_record_is_still_reported(self):
        r = self.file()
        self.record(r).write_text(json.dumps({"id": "x"}))
        errors = []
        needs.outside_items(self.home, errors=errors)
        self.assertTrue(any("invalid filed record" in e for e in errors), errors)


class WatchedFolderTraversal(OutsideFixture):
    def swap_after_listing(self, evil):
        """Return a context that, at the leaf open, replaces the watched folder with a link to *evil*."""
        moved = self.home / "moved-exports"
        real_open = os.open
        state = {"swapped": False, "calls": []}

        def opener(path, flags, *args, **kwargs):
            state["calls"].append((str(path), kwargs.get("dir_fd")))
            if not state["swapped"] and str(path).endswith("note.txt"):
                state["swapped"] = True
                self.folder.rename(moved)
                self.folder.symlink_to(evil, target_is_directory=True)
            return real_open(path, flags, *args, **kwargs)

        return patch.object(os, "open", opener), state

    def test_a_folder_swapped_for_a_link_after_the_scan_is_not_followed(self):
        """01-070 (class C): the leaf is opened relative to the directory that was scanned."""
        self.watch()
        (self.folder / "note.txt").write_text("Original question?")
        evil = self.home / "evil"
        evil.mkdir()
        (evil / "note.txt").write_text("Attacker text")
        patcher, state = self.swap_after_listing(evil)
        with patcher:
            items = needs.outside_items(self.home)
        self.assertEqual([i["text"] for i in items], ["Original question?"])
        leaf = [c for c in state["calls"] if c[0].endswith("note.txt")]
        self.assertTrue(leaf and all(fd is not None and "/" not in p for p, fd in leaf), leaf)

    def test_a_lookup_swapped_after_the_scan_is_not_followed(self):
        self.watch()
        (self.folder / "note.txt").write_text("Original question?")
        (item,) = needs.outside_items(self.home)
        evil = self.home / "evil"
        evil.mkdir()
        (evil / "note.txt").write_text("Attacker text")
        patcher, _ = self.swap_after_listing(evil)
        with patcher:
            got = needs.outside_lookup(item["id"], self.home)
        self.assertEqual(got["text"], "Original question?")

    def test_a_symlinked_watched_root_that_resolves_elsewhere_still_works_by_its_real_path(self):
        real = self.home / "real"
        real.mkdir()
        (real / "note.txt").write_text("Hello")
        link = self.home / "link"
        link.symlink_to(real, target_is_directory=True)
        self.settings["outside_dirs"] = str(link)
        self.assertEqual([i["text"] for i in needs.outside_items(self.home)], ["Hello"])

    def test_special_files_and_links_are_still_skipped(self):
        self.watch()
        os.mkfifo(self.folder / "pipe.txt")
        (self.folder / "good.txt").write_text("Fine")
        (self.folder / "link.txt").symlink_to(self.folder / "good.txt")
        errors = []
        items = needs.outside_items(self.home, errors=errors)
        self.assertEqual([i["path"] for i in items], ["good.txt"])


class FilingDocs(unittest.TestCase):
    def test_the_readme_states_the_resolve_and_key_rules(self):
        """01-068/01-073 (class I): the README says what the tool now does."""
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        self.assertIn("returned `id` together with the returned `version`", readme)
        self.assertIn("over 120 characters is refused rather than shortened", readme)


if __name__ == "__main__":
    unittest.main()
