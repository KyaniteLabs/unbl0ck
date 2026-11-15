"""Working through a watched-folder backlog: hidden items free the window, nesting is opt-in and bounded."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_outside import OutsideFixture, needs


class FolderPaging(OutsideFixture):
    def make(self, name, text="Review this", age=0, folder=None):
        f = (folder or self.folder) / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        t = time.time() - age
        os.utime(f, (t, t))
        return f

    def items(self):
        return needs.build_queue()

    def test_hiding_the_first_page_exposes_older_files(self):
        self.watch()
        with patch.object(needs, "_OUTSIDE_LIMIT", 3):
            for n in range(7):
                self.make(f"n{n}.txt", age=n * 10)  # n0 is the newest
            q = self.items()
            self.assertEqual([i["path"] for i in q["items"]], ["n0.txt", "n1.txt", "n2.txt"])
            self.assertTrue(any("4 older files are waiting" in e for e in q["errors"]), q["errors"])
            for it in q["items"]:
                needs.done_outside(it["id"], it["version"])
            q = self.items()
            self.assertEqual([i["path"] for i in q["items"]], ["n3.txt", "n4.txt", "n5.txt"])
            self.assertTrue(any("1 older file" in e or "1 older files" in e for e in q["errors"]), q["errors"])
            for it in q["items"]:
                needs.done_outside(it["id"], it["version"])
            q = self.items()
            self.assertEqual([i["path"] for i in q["items"]], ["n6.txt"])
            self.assertFalse(any("older files" in e for e in q["errors"]))

    def test_more_than_200_files_all_reachable_without_moving_any(self):
        self.watch()
        before = {}
        for n in range(230):
            f = self.make(f"f{n:03d}.txt", age=n)
            before[f.name] = (f.read_bytes(), f.stat().st_mtime_ns)
        seen = set()
        for _ in range(3):
            q = self.items()
            if not q["items"]:
                break
            self.assertLessEqual(len(q["items"]), 200)
            for it in q["items"]:
                seen.add(it["path"])
                needs.done_outside(it["id"], it["version"])
        self.assertEqual(len(seen), 230)
        after = {f.name: (f.read_bytes(), f.stat().st_mtime_ns) for f in self.folder.iterdir()}
        self.assertEqual(after, before)  # source files never moved, renamed or changed

    def test_a_hidden_file_that_changes_resurfaces_in_its_place(self):
        self.watch()
        f = self.make("a.txt")
        (it,) = self.items()["items"]
        needs.done_outside(it["id"], it["version"])
        self.assertEqual(self.items()["items"], [])
        f.write_text("Changed contents")
        (back,) = self.items()["items"]
        self.assertNotEqual(back["version"], it["version"])

    def test_tied_timestamps_order_is_stable_across_polls(self):
        self.watch()
        t = time.time() - 100
        for name in ("b.txt", "a.txt", "c.txt"):
            f = self.make(name)
            os.utime(f, (t, t))
        with patch.object(needs, "_OUTSIDE_LIMIT", 2):
            first = [i["path"] for i in self.items()["items"]]
            second = [i["path"] for i in self.items()["items"]]
        self.assertEqual(first, second)
        self.assertEqual(first, ["a.txt", "b.txt"])

    def test_file_deleted_between_listing_and_reading_is_reported_not_fatal(self):
        self.watch()
        self.make("keep.txt", age=0)
        gone = self.make("gone.txt", age=10)
        real = needs._folder_outside_item

        def racing(f, root, st, *rest):
            if f.name == "keep.txt":
                gone.unlink()
            return real(f, root, st, *rest)

        with patch.object(needs, "_folder_outside_item", racing):
            q = self.items()
        self.assertEqual([i["path"] for i in q["items"]], ["keep.txt"])
        self.assertTrue(any(e.startswith("outside:") for e in q["errors"]), q["errors"])

    def test_added_file_appears_on_the_next_poll(self):
        self.watch()
        self.make("a.txt", age=50)
        self.assertEqual(len(self.items()["items"]), 1)
        self.make("b.txt", age=0)
        self.assertEqual([i["path"] for i in self.items()["items"]], ["b.txt", "a.txt"])

    def test_unreadable_file_does_not_hide_the_others(self):
        self.watch()
        self.make("ok.txt", age=0)
        bad = self.make("bad.txt", age=10)
        bad.chmod(0)
        try:
            q = self.items()
        finally:
            bad.chmod(0o600)
        if os.geteuid() != 0:
            self.assertEqual([i["path"] for i in q["items"]], ["ok.txt"])
            self.assertTrue(any("bad.txt" in e for e in q["errors"]))


class NestedFolders(FolderPaging):
    def test_nested_files_are_ignored_by_default_and_do_not_leak_in(self):
        self.watch()
        self.make("top.txt")
        self.make("sub/deep.txt")
        self.assertEqual([i["path"] for i in self.items()["items"]], ["top.txt"])

    def test_overlapping_roots_do_not_lose_files_the_parent_window_left_out(self):
        child = self.folder / "child"
        child.mkdir()
        self.settings["outside_dirs"] = f"{self.folder},{child}"
        self.settings["outside_recursive"] = True
        with patch.object(needs, "_OUTSIDE_LIMIT", 3):
            for n in range(4):
                self.make(f"top{n}.txt", age=n)  # newer than everything in the child
            for n in range(2):
                self.make(f"old{n}.txt", age=100 + n, folder=child)
            paths = {i["path"] for i in self.items()["items"]}
        # The parent's window holds its newest three; the child's own window still shows both
        # of its files, so nothing is silently dropped.
        self.assertTrue({"old0.txt", "old1.txt"} <= {p.split("/")[-1] for p in paths} or
                        {"child/old0.txt", "child/old1.txt"} <= paths, paths)

    def test_paging_survives_a_backlog_larger_than_the_dismissal_store(self):
        self.watch()
        total = needs._HIDDEN_KEEP + needs._OUTSIDE_LIMIT + 100  # 1,300 with production limits
        for n in range(total):
            self.make(f"f{n:04d}.txt", age=n)  # f0000 is the newest
        done = 0
        order = []
        for _ in range(total // needs._OUTSIDE_LIMIT + 2):
            page = self.items()["items"]
            if not page:
                break
            for it in page:
                order.append(it["path"])
                needs.done_outside(it["id"], it["version"])
            done += len(page)
        self.assertEqual(done, total)
        self.assertEqual(order, sorted(order), "paging never revisits or skips ahead")
        self.assertEqual(len(set(order)), total)
        self.assertEqual(self.items()["items"], [])

    def test_other_dismissals_do_not_evict_folder_progress(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            for n in range(needs._HIDDEN_FOLDER_KEEP):
                needs.hide(f"outside:folder:{n}", "v", home) if n % 500 == 0 else None
            needs.hide("outside:folder:keep", "v", home)
            for n in range(needs._HIDDEN_KEEP + 5):
                needs.hide(f"chat:p:{n}", "v", home)
            hidden = needs.load_hidden(home)
            self.assertIn("outside:folder:keep", hidden)
            self.assertEqual(sum(1 for k in hidden if k.startswith("chat:")), needs._HIDDEN_KEEP)

    def test_recursive_intake_reads_nested_files_read_only(self):
        self.watch()
        self.settings["outside_recursive"] = True
        top = self.make("top.txt", age=0)
        deep = self.make("a/b/deep.md", text="# Deep note", age=10)
        before = deep.read_bytes(), deep.stat().st_mtime_ns
        items = {i["path"]: i for i in self.items()["items"]}
        self.assertEqual(set(items), {"top.txt", "a/b/deep.md"})
        needs.done_outside(items["a/b/deep.md"]["id"], items["a/b/deep.md"]["version"])
        self.assertEqual((deep.read_bytes(), deep.stat().st_mtime_ns), before)
        self.assertTrue(top.exists())

    def test_same_filename_in_two_folders_stays_two_items(self):
        self.watch()
        self.settings["outside_recursive"] = True
        self.make("x/note.txt", text="One")
        self.make("y/note.txt", text="Two")
        q = self.items()
        self.assertEqual(len({i["id"] for i in q["items"]}), 2)

    def test_depth_is_bounded(self):
        self.watch()
        self.settings["outside_recursive"] = True
        self.make("a/b/c/ok.txt")
        self.make("a/b/c/d/too-deep.txt")
        self.assertEqual([i["path"] for i in self.items()["items"]], ["a/b/c/ok.txt"])

    def test_entry_bound_is_reported(self):
        self.watch()
        self.settings["outside_recursive"] = True
        for n in range(6):
            self.make(f"d{n}/f.txt")
        with patch.object(needs, "OUTSIDE_SCAN_ENTRIES", 4):
            q = self.items()
        self.assertTrue(any("scan examined 4 entries" in e for e in q["errors"]), q["errors"])

    def test_time_bound_is_reported(self):
        self.watch()
        self.settings["outside_recursive"] = True
        self.make("a/f.txt")
        with patch.object(needs, "OUTSIDE_SCAN_SECONDS", -1.0):
            q = self.items()
        self.assertTrue(any("scan stopped after" in e for e in q["errors"]), q["errors"])

    def test_symlinked_directory_and_file_are_not_followed(self):
        self.watch()
        self.settings["outside_recursive"] = True
        outside = self.home / "elsewhere"
        outside.mkdir()
        (outside / "secret.txt").write_text("private")
        (self.folder / "link").symlink_to(outside, target_is_directory=True)
        (self.folder / "file-link.txt").symlink_to(outside / "secret.txt")
        self.make("ok.txt")
        q = self.items()
        self.assertEqual([i["path"] for i in q["items"]], ["ok.txt"])
        self.assertNotIn("private", json.dumps(q))

    def test_directory_swapped_for_a_symlink_after_the_scan_is_rejected(self):
        self.watch()
        self.settings["outside_recursive"] = True
        outside = self.home / "elsewhere"
        outside.mkdir()
        (outside / "secret.txt").write_text("private")
        nested = self.make("sub/real.txt", text="legit")
        real = needs._folder_outside_item

        def swap(f, root, st):
            if f.name == "real.txt":
                nested.unlink()
                nested.parent.rmdir()
                nested.parent.symlink_to(outside, target_is_directory=True)
                (outside / "real.txt").write_text("private")
            return real(f, root, st)

        with patch.object(needs, "_folder_outside_item", swap):
            q = self.items()
        self.assertEqual(q["items"], [])
        self.assertNotIn("private", json.dumps(q))

    def test_dot_directories_are_skipped(self):
        self.watch()
        self.settings["outside_recursive"] = True
        self.make(".hidden/secret.txt")
        self.make("ok.txt")
        self.assertEqual([i["path"] for i in self.items()["items"]], ["ok.txt"])

    def test_recursive_setting_accepts_text_values(self):
        for raw, expected in (("true", True), ("off", False), (False, False), ("1", True)):
            self.assertIs(needs.outside_recursive({"outside_recursive": raw}), expected)


class FiledCap(OutsideFixture):
    def test_full_queue_refuses_new_items_with_counts_and_loses_nothing(self):
        with patch.object(needs, "_OUTSIDE_LIMIT", 3):
            for n in range(3):
                self.file(dedupe_key=f"k{n}", title=f"T{n}")
            reply = self.file(dedupe_key="extra", title="Extra")
            self.assertFalse(reply.get("ok", False), reply)
            self.assertIn("Outside queue is full (3 filed items)", json.dumps(reply))
            q = needs.build_queue()
            self.assertEqual({i["title"] for i in q["items"]}, {"T0", "T1", "T2"})
            self.assertTrue(any("3 agent-filed items are waiting" in e for e in q["errors"]), q["errors"])
            # updating an existing item still works at the cap
            self.assertTrue(self.file(dedupe_key="k1", title="T1", body="updated").get("ok"))
            # finishing one frees room, and the refused item can then be filed
            first = next(i for i in q["items"] if i["title"] == "T0")
            needs.done_outside(first["id"], first["version"])
            self.assertTrue(self.file(dedupe_key="extra", title="Extra").get("ok"))

    def test_no_error_below_the_cap(self):
        self.file(dedupe_key="k")
        self.assertFalse(any("agent-filed items are waiting" in e for e in needs.build_queue()["errors"]))


class RetentionEvictsOldestNotAlphabetical(unittest.TestCase):
    """The keep limits drop the OLDEST entries, also after the file was written and read back."""

    def test_hidden_cap_evicts_the_oldest_dismissal_even_when_its_name_sorts_last(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            for n in range(needs._HIDDEN_KEEP):
                needs.hide(f"chat:z{n:04d}", "v", home)  # the oldest entries sort LAST by name
            needs.hide("chat:a-newer", "v", home)
            needs.hide("chat:a-newest", "v", home)
            hidden = needs.load_hidden(home)
            self.assertEqual(len(hidden), needs._HIDDEN_KEEP)
            self.assertIn("chat:a-newer", hidden)
            self.assertIn("chat:a-newest", hidden)
            self.assertNotIn("chat:z0000", hidden)
            self.assertNotIn("chat:z0001", hidden)
            self.assertIn("chat:z0002", hidden)

    def test_pending_release_store_refuses_a_new_marker_when_full_and_keeps_the_old_ones(self):
        """The pending-release store is not trimmed (a marker is recovery state): it refuses and reports."""
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            for n in range(needs._HIDDEN_KEEP):
                needs._set_pending_release(f"kanban:b:z{n:04d}", "1", home)
            with self.assertRaises(needs.PendingReleaseFull):
                needs._set_pending_release("kanban:b:a-newer", "1", home)
            pending = needs.load_pending_releases(home)
            self.assertEqual(len(pending), needs._HIDDEN_KEEP)
            self.assertIn("kanban:b:z0000", pending)
            self.assertNotIn("kanban:b:a-newer", pending)


if __name__ == "__main__":
    unittest.main()
