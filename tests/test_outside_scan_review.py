"""Review findings on bounded folder intake: symlinked ancestors, scan budgets, depth notices,
overlapping roots and hidden-file accounting."""

import os
import time
import unittest
from unittest.mock import patch

from test_outside import OutsideFixture, needs


class ScanReview(OutsideFixture):
    def setUp(self):
        super().setUp()
        self.watch()
        self.settings["outside_recursive"] = True

    def make(self, rel, text="note"):
        f = self.folder / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        return f

    def queue(self, **kw):
        return needs.build_queue(**kw)

    def test_directory_swapped_for_a_symlink_after_it_was_queued_is_not_followed(self):
        self.make("sub/ok.txt", "visible")
        self.make(".private/secret.txt", "PRIVATE")
        (self.folder / "sub" / "secret.txt").write_text("decoy")
        real = os.scandir
        state = {"swapped": False}

        def swapping(path, *a, **k):
            it = list(real(path, *a, **k))
            if not state["swapped"] and str(path) == str(self.folder.resolve()):
                state["swapped"] = True
                (self.folder / "sub").rename(self.folder / "sub-moved")
                (self.folder / "sub").symlink_to(self.folder / ".private")
            return _Scan(it)

        class _Scan:
            def __init__(self, entries):
                self.entries = entries

            def __enter__(self):
                return iter(self.entries)

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return iter(self.entries)

        with patch.object(needs.os, "scandir", swapping):
            q = self.queue()
        blob = repr(q["items"])
        self.assertNotIn("PRIVATE", blob)
        self.assertNotIn(".private", blob)

    def test_read_rejects_a_symlinked_ancestor_even_for_a_matching_name(self):
        self.make(".private/secret.txt", "PRIVATE")
        (self.folder / "sub").symlink_to(self.folder / ".private")
        f = self.folder.resolve() / "sub" / "secret.txt"
        st = (self.folder / ".private" / "secret.txt").stat()
        self.assertIsNone(needs._folder_outside_item(f, self.folder.resolve(), st))

    def test_scan_budget_applies_before_the_directory_is_materialised(self):
        for n in range(30):
            self.make(f"n{n}.txt")
        with patch.object(needs, "OUTSIDE_SCAN_ENTRIES", 5):
            q = self.queue()
        self.assertLessEqual(len(q["items"]), 5)
        self.assertTrue(any("scan examined 5 entries" in e for e in q["errors"]), q["errors"])

    def test_scan_uses_an_incremental_iterator_not_a_list(self):
        self.make("a.txt")
        real = os.scandir
        seen = []

        def spy(path, *a, **k):
            cm = real(path, *a, **k)
            seen.append(type(cm).__name__)
            return cm

        with patch.object(needs.os, "scandir", spy):
            self.queue()
        self.assertTrue(seen)
        source = __import__("inspect").getsource(needs._scan_outside_folder)
        self.assertNotIn("list(os.scandir", source)
        self.assertNotIn("sorted(entries", source)

    def test_time_budget_stops_before_reading_the_rest_of_a_large_directory(self):
        for n in range(20):
            self.make(f"n{n}.txt")
        ticks = iter([0.0] + [100.0] * 1000)
        with patch.object(needs.time, "monotonic", lambda: next(ticks)):
            q = self.queue()
        self.assertTrue(any("stopped after" in e for e in q["errors"]), q["errors"])

    def test_time_budget_also_applies_to_queued_empty_directories(self):
        for n in range(30):
            (self.folder / f"empty{n}").mkdir()
        self.make("late.txt")
        clock = {"t": 0.0}
        real = os.scandir

        def slow_scandir(path, *a, **k):
            clock["t"] += 1.0  # opening each directory costs a second
            return real(path, *a, **k)

        with patch.object(needs.os, "scandir", slow_scandir), patch.object(
            needs.time, "monotonic", lambda: clock["t"]
        ):
            q = self.queue()
        self.assertTrue(any("stopped after" in e for e in q["errors"]), q["errors"])
        self.assertLess(clock["t"], 10.0)  # it did not open all 31 directories

    def test_files_below_the_depth_limit_produce_a_notice_not_silence(self):
        self.make("a/b/c/d/note.txt")
        q = self.queue()
        self.assertEqual(q["items"], [])
        self.assertTrue(any("deeper than 3" in e for e in q["errors"]), q["errors"])

    def test_no_depth_notice_when_nothing_is_cut_off(self):
        self.make("a/note.txt")
        q = self.queue()
        self.assertFalse(any("deeper than" in e for e in q["errors"]))

    def test_overlapping_roots_collect_a_file_once(self):
        self.make("sub/n.txt")
        self.settings["outside_dirs"] = f"{self.folder},{self.folder / 'sub'}"
        q = self.queue()
        ids = [i["id"] for i in q["items"]]
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertEqual(len(ids), 1)

    def test_hidden_folder_files_stay_countable_and_visible_when_asked(self):
        self.make("n.txt")
        (it,) = self.queue()["items"]
        needs.done_outside(it["id"], it["version"])
        q = self.queue()
        self.assertEqual(q["items"], [])
        self.assertEqual(q["hidden"], 1)
        shown = self.queue(include_hidden=True)
        self.assertEqual([i["id"] for i in shown["items"]], [it["id"]])

    def test_hidden_files_do_not_use_up_the_waiting_window(self):
        for n in range(4):
            self.make(f"n{n}.txt")
            os.utime(self.folder / f"n{n}.txt", (time.time() - n * 10,) * 2)
        with patch.object(needs, "_OUTSIDE_LIMIT", 2):
            first = self.queue()["items"]
            for it in first:
                needs.done_outside(it["id"], it["version"])
            q = self.queue()
        self.assertEqual(len(q["items"]), 2)
        self.assertEqual(q["hidden"], 2)


if __name__ == "__main__":
    unittest.main()
