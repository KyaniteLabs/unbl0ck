"""Issue #70: sweep defects found during the 0.6.0 review rounds."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from r4c_common import HomeCase, needs


@unittest.skipUnless(needs._DIR_FD, "descriptor-relative opens are not supported here")
class NestedReadIsPinned(unittest.TestCase):
    """A nested watched file is opened from the folder's descriptor, one directory at a time,
    so a subfolder swapped for a link after the containment check is not followed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "watched"
        self.away = self.tmp / "away"
        (self.root / "sub").mkdir(parents=True)
        self.away.mkdir()
        (self.away / "b.md").write_text("outside the watched folder")
        # The same inode inside and outside, so only the walk itself can tell them apart.
        os.link(self.away / "b.md", self.root / "sub" / "b.md")

    def test_a_subfolder_swapped_for_a_link_after_the_check_is_not_followed(self):
        f = self.root / "sub" / "b.md"
        st = os.stat(f)
        real_resolve = Path.resolve
        swapped = []

        def resolve_then_swap(path, *a, **kw):
            out = real_resolve(path, *a, **kw)
            if path == f and not swapped:
                swapped.append(True)
                shutil.rmtree(self.root / "sub")
                (self.root / "sub").symlink_to(self.away, target_is_directory=True)
            return out

        with needs._watched_dir(self.root) as dfd, patch.object(type(f), "resolve", resolve_then_swap):
            try:
                item = needs._folder_outside_item(f, self.root, st, dfd)
            except OSError:
                item = None
        self.assertTrue(swapped)
        self.assertIsNone(item)


class StateListingsAreBounded(HomeCase):
    """Every state folder read is one bounded listing: a folder full of records cannot make a
    poll read every one of them, and a cut listing says so instead of looking complete."""

    def counted(self, fn):
        reads = []
        real = needs._read_plain_bytes_st

        def read(path, *a, **kw):
            reads.append(Path(path).name)
            return real(path, *a, **kw)

        with patch.object(needs, "_PURGE_SCAN_LIMIT", 3), patch.object(needs, "_read_plain_bytes_st", read):
            return reads, fn()

    def test_live_prompt_records_are_read_at_most_the_listing_limit(self):
        for n in range(6):
            needs.record_wait(self.home, request_id=f"{n:016x}", kind="clarify", prompt="Pick?", platform="cli")
        errors = []
        reads, _ = self.counted(lambda: needs.wait_items(self.home, errors, readonly=True))
        self.assertLessEqual(len([r for r in reads if r.endswith(".json")]), 3, reads)
        self.assertTrue(any("only the first 3" in str(e) for e in errors), errors)

    def test_pairing_requests_are_read_at_most_the_listing_limit(self):
        folder = self.home / "platforms" / "pairing"
        folder.mkdir(parents=True)
        for n in range(6):
            (folder / f"p{n}-pending.json").write_text("{}")
        errors = []
        reads, _ = self.counted(lambda: needs.pairing_items(self.home, errors, store=lambda p: type("S", (), {"list_pending": lambda self: []})()))
        self.assertLessEqual(len(reads), 3, reads)
        self.assertTrue(any("only the first 3" in str(e) for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
