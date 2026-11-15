"""Issue #70: sweep defects found during the 0.6.0 review rounds."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import needs


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


if __name__ == "__main__":
    unittest.main()
