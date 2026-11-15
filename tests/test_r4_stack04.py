"""Round-4 review findings on the outside-paging branch (#37): dismissal caps and their size,
scan budgets and continuation, partial results, read backfill, hidden counts and settings snapshots."""

import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_outside import ROOT, OutsideFixture, needs


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Base(OutsideFixture):
    def make(self, name, text="Review this", age=0, folder=None):
        f = (folder or self.folder) / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        t = time.time() - age
        os.utime(f, (t, t))
        return f

    def folder_id(self, path):
        import hashlib

        return "outside:folder:" + hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()


class DismissalCaps(Base):
    def test_not_for_me_on_folder_files_is_documented_with_the_folder_bound(self):
        """04-001 (class I): Not for me on a folder file lands in the folder bound, so the docs
        must say the 1,000 cap is for every other dismissal."""
        for rel in ("README.md", "docs/architecture.md"):
            text = " ".join((ROOT / rel).read_text().split())
            self.assertIn("Not for me", text, rel)
            self.assertRegex(text, r"watched-folder files.{0,120}5,?000|5,?000.{0,160}watched-folder", rel)
        # and the behavior the docs describe: 1,001 folder dismissals all stay (under the folder bound)
        with patch.object(needs, "_HIDDEN_KEEP", 5), patch.object(needs, "_HIDDEN_FOLDER_KEEP", 50):
            for n in range(11):
                needs.hide("outside:folder:" + f"{n:064x}", "1:1:" + "0" * 16, self.home)
            self.assertEqual(len(needs.load_hidden(self.home)), 11)

    def test_two_watched_folders_do_not_evict_each_others_completions(self):
        """04-002 (class H): the Done bound grows with the number of watched folders."""
        other = self.home / "other"
        other.mkdir()
        self.settings["outside_dirs"] = f"{self.folder},{other}"
        with patch.object(needs, "_HIDDEN_FOLDER_KEEP", 30):
            ids = ["outside:folder:" + f"{n:064x}" for n in range(51)]  # 30 + 21 across two folders
            for item_id in ids:
                needs.hide(item_id, "1:1:" + "0" * 16, self.home)
            hidden = needs.load_hidden(self.home)
            self.assertEqual(len(hidden), 51)
            self.assertIn(ids[0], hidden)
            # one folder keeps the single-folder bound
            self.settings["outside_dirs"] = str(self.folder)
            needs.hide("outside:folder:" + "f" * 64, "1:1:" + "0" * 16, self.home)
            self.assertEqual(len(needs.load_hidden(self.home)), 30)

    def test_the_folder_bound_has_a_ceiling_so_the_store_stays_readable(self):
        roots = [self.home / f"r{n}" for n in range(20)]
        for r in roots:
            r.mkdir()
        self.settings["outside_dirs"] = ",".join(str(r) for r in roots)
        self.assertEqual(needs._hidden_folder_cap(), needs._HIDDEN_FOLDER_KEEP * needs._HIDDEN_MAX_ROOTS)


class StoreSize(Base):
    def test_a_compact_utf8_store_stays_readable_after_a_hide(self):
        """04-003 (class G): what is written must be readable under the same size limit."""
        path = needs._hidden_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        emoji = "\U0001F600" * 300
        data = {"outside:folder:" + f"{n:064x}": emoji for n in range(5000)}
        path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        self.assertLess(path.stat().st_size, needs.MAX_STATE_BYTES)
        needs.hide("chat:one", "v", self.home)
        self.assertLessEqual(path.stat().st_size, needs.MAX_STATE_BYTES)
        needs._load_hidden_raw(self.home, strict=True)  # the strict read still passes
        self.assertEqual(len(needs.load_hidden(self.home)), 5001)

    def test_an_oversized_store_drops_the_oldest_dismissals_instead_of_becoming_unreadable(self):
        with patch.object(needs, "MAX_STATE_BYTES", 6000):
            for n in range(40):
                needs.hide(f"chat:{n:03d}", "v" * 300, self.home)
            path = needs._hidden_path(self.home)
            self.assertLessEqual(path.stat().st_size, 6000)
            hidden = needs.load_hidden(self.home, strict=True)
            self.assertIn("chat:039", hidden)
            self.assertNotIn("chat:000", hidden)

    def test_one_dismissal_that_alone_exceeds_the_limit_is_refused_and_nothing_is_lost(self):
        needs.hide("chat:keep", "v", self.home)
        with patch.object(needs, "MAX_STATE_BYTES", 400):
            with self.assertRaises(ValueError):
                needs.hide("chat:huge", "v" * 2000, self.home)
        self.assertIn("chat:keep", needs.load_hidden(self.home, strict=True))

    def test_write_json_never_writes_a_file_it_could_not_read_back(self):
        target = self.home / "x.json"
        with patch.object(needs, "MAX_STATE_BYTES", 100):
            with self.assertRaises(OSError):
                needs._write_json(target, {"a": "b" * 500})
        self.assertFalse(target.exists())


class ScanProgress(Base):
    def test_finished_files_do_not_use_up_the_entry_budget(self):
        """04-004 (class E/D): the file past the entry bound becomes reachable once the files before it are done."""
        self.watch()
        for n in range(11):
            self.make(f"n{n:02d}.txt", age=n)
        with patch.object(needs, "OUTSIDE_SCAN_ENTRIES", 10), patch.object(needs, "_OUTSIDE_LIMIT", 4):
            seen = set()
            for _ in range(10):
                items = needs.build_queue()["items"]
                if not items:
                    break
                for it in items:
                    seen.add(it["path"])
                    needs.done_outside(it["id"], it["version"], self.home)
        self.assertEqual(len(seen), 11, sorted(seen))

    def test_a_full_scan_is_still_reported_when_the_entry_bound_is_hit(self):
        self.watch()
        for n in range(30):
            self.make(f"n{n}.txt")
        with patch.object(needs, "OUTSIDE_SCAN_ENTRIES", 5):
            errors = needs.build_queue()["errors"]
        self.assertTrue(any("scan examined 5 entries" in e for e in errors), errors)

    def test_the_time_bound_is_reported_when_the_last_directory_overruns(self):
        """04-005 (class D): the final directory open can be the slow step; it is still reported."""
        self.watch()
        clock = FakeClock()
        real = os.scandir

        def slow(path, *a, **k):
            clock.now += 3.0
            return real(path, *a, **k)

        with patch.object(needs.time, "monotonic", clock), patch.object(needs.os, "scandir", slow):
            q = needs.build_queue()
        self.assertTrue(any("stopped after" in e for e in q["errors"]), q["errors"])

    def test_a_listing_that_fails_midway_keeps_what_was_read(self):
        """04-006 (class E): one failing iterator step does not discard the healthy candidates."""
        self.watch()
        self.make("healthy.txt")
        real = os.scandir

        class Breaking:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                self.inner.__enter__()
                return self

            def __exit__(self, *exc):
                return self.inner.__exit__(*exc)

            def __iter__(self):
                return self

            def __next__(self):
                try:
                    return next(self.inner)
                except StopIteration:
                    raise OSError("listing failed")

        with patch.object(needs.os, "scandir", lambda path, *a, **k: Breaking(real(path, *a, **k))):
            q = needs.build_queue()
        self.assertEqual([i["path"] for i in q["items"]], ["healthy.txt"])
        self.assertTrue(any(e.startswith("outside:") and "OSError" in e for e in q["errors"]), q["errors"])


class ReadWindow(Base):
    def test_unreadable_newest_files_do_not_starve_older_readable_ones(self):
        """04-007 (class E): the window backfills past files that cannot be read."""
        self.watch()
        for n in range(5):
            self.make(f"deny{n}.txt", age=n)
        self.make("readable.txt", age=100)
        real = needs._folder_outside_item

        def deny(f, root, st, *rest):
            if f.name.startswith("deny"):
                raise PermissionError("denied")
            return real(f, root, st, *rest)

        with patch.object(needs, "_OUTSIDE_LIMIT", 3), patch.object(needs, "_folder_outside_item", deny):
            q = needs.build_queue()
        self.assertEqual([i["path"] for i in q["items"]], ["readable.txt"])
        self.assertTrue(any("denied" in e or "PermissionError" in e for e in q["errors"]), q["errors"])
        self.assertLessEqual(sum("deny" in e for e in q["errors"]), 6)

    def test_many_unreadable_files_are_summarised_not_listed_one_by_one(self):
        self.watch()
        for n in range(40):
            self.make(f"deny{n}.txt", age=n)
        self.make("readable.txt", age=500)

        real = needs._folder_outside_item

        def deny(f, root, st, *rest):
            if f.name.startswith("deny"):
                raise PermissionError("denied")
            return real(f, root, st, *rest)

        with patch.object(needs, "_folder_outside_item", deny):
            q = needs.build_queue()
        self.assertEqual([i["path"] for i in q["items"]], ["readable.txt"])
        self.assertLess(len(q["errors"]), 10, q["errors"])
        self.assertTrue(any("40" in e and "could not be read" in e for e in q["errors"]), q["errors"])

    def test_reading_stops_at_the_time_budget_and_says_so(self):
        """01-077 (class D): the budget also bounds the read phase, with a notice."""
        self.watch()
        for n in range(6):
            self.make(f"n{n}.txt", age=n)
        clock = FakeClock()
        real = needs._folder_outside_item

        def slow(f, root, st, *rest):
            clock.now += 1.5
            return real(f, root, st, *rest)

        with patch.object(needs.time, "monotonic", clock), patch.object(needs, "_folder_outside_item", slow):
            q = needs.build_queue()
        self.assertTrue(1 <= len(q["items"]) < 6, len(q["items"]))
        self.assertTrue(any("reading stopped after" in e for e in q["errors"]), q["errors"])


class HiddenAccounting(Base):
    def test_hidden_files_beyond_the_window_are_counted_and_the_omission_is_reported(self):
        """04-008 (class E): 300 hidden files are 300 hidden, and a listing that shows 200 says so."""
        self.watch()
        with patch.object(needs, "_OUTSIDE_LIMIT", 20):
            for n in range(30):
                self.make(f"n{n:02d}.txt", age=n)
            for _ in range(2):  # first page, then the ten older files it exposed
                for it in needs.build_queue()["items"]:
                    if it["id"] not in needs.load_hidden(self.home):
                        needs.hide(it["id"], it["version"], self.home)
            q = needs.build_queue()
            self.assertEqual(q["hidden"], 30)
            self.assertEqual(q["count"], 0)
            everything = needs.build_queue(include_hidden=True)
            self.assertEqual(len(everything["items"]), 20)
            self.assertTrue(any("10 hidden files are not listed" in e for e in everything["errors"]), everything["errors"])
            self.assertFalse(any("hidden files are not listed" in e for e in q["errors"]), q["errors"])


class SettingsSnapshot(Base):
    def test_roots_and_recursion_come_from_one_settings_snapshot(self):
        """01-076 (class A): the folder list and the recursion flag are read together."""
        self.make("sub/deep.txt")
        self.make("top.txt")
        snapshots = [
            {"outside_dirs": str(self.folder), "outside_recursive": False},
            {"outside_dirs": "", "outside_recursive": True},
        ]
        calls = []

        def settings():
            calls.append(1)
            return snapshots[min(len(calls) - 1, 1)]

        with patch.object(needs, "_settings", settings):
            items = needs.outside_items(self.home)
        self.assertEqual([i["path"] for i in items], ["top.txt"])  # one snapshot: not recursive


if __name__ == "__main__":
    unittest.main()
