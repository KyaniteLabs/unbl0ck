"""Round-5 review findings owned by the outside-paging branch."""

import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_outside import OutsideFixture, needs

ROOT = Path(__file__).resolve().parents[1]


class UndoIsRetryable(OutsideFixture):
    """R5-needs-011 (class B): a failed second write of an undo leaves the first one undone."""

    def test_a_failed_done_write_keeps_the_hidden_marker_so_the_same_undo_can_retry(self):
        result = self.file(dedupe_key="a")
        (it,) = needs.build_queue()["items"]
        done = needs.done_outside(it["id"], it["version"])
        hidden_token = needs.hide(it["id"], it["version"])
        self.assertTrue(done["token"])
        real_write = needs._write_json
        record = self.record(result)

        def failing(path, *a, **kw):
            if Path(path) == record:
                raise OSError("disk full")
            return real_write(path, *a, **kw)

        with patch.object(needs, "_write_json", failing):
            with self.assertRaises(OSError):
                needs.unhide(it["id"], token=hidden_token)
        # Nothing changed, so the operator's retry with the same token still works.
        self.assertIn(it["id"], needs.load_hidden())
        self.assertTrue(needs.unhide(it["id"], token=hidden_token))
        self.assertNotIn(it["id"], needs.load_hidden())
        self.assertEqual([i["id"] for i in needs.build_queue()["items"]], [it["id"]])


class HideRefusesAnOlderVersion(OutsideFixture):
    """R5-needs-052 / R5-desk-013 (class I): the refusal holds with no earlier dismissal too."""

    def test_a_hide_for_an_older_version_is_refused_when_nothing_was_hidden(self):
        self.watch()
        f = self.folder / "a.txt"
        f.write_text("one")
        (it,) = needs.build_queue()["items"]
        old = it["version"]
        f.write_text("two, longer")
        with self.assertRaises(needs.StaleHide):
            needs.hide(it["id"], old)
        self.assertNotIn(it["id"], needs.load_hidden())
        (now,) = needs.build_queue()["items"]
        self.assertNotEqual(now["version"], old)
        self.assertIsInstance(needs.hide(it["id"], now["version"]), str)

    def test_a_hide_for_an_older_version_is_refused_when_the_same_version_was_hidden(self):
        self.watch()
        f = self.folder / "a.txt"
        f.write_text("one")
        (it,) = needs.build_queue()["items"]
        needs.hide(it["id"], it["version"])
        f.write_text("two, longer")
        with self.assertRaises(needs.StaleHide):
            needs.hide(it["id"], it["version"])


class DismissalIsBoundToContent(OutsideFixture):
    """R5-needs-019 (class A): a stat-identical file with other content is not dismissed."""

    def swap_same_size(self, f, text):
        st = f.stat()
        f.write_text(text)
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))

    def test_a_same_size_edit_with_restored_mtime_is_listed_without_reading_hidden_files(self):
        self.watch()
        f = self.folder / "a.txt"
        f.write_text("approve it")
        (it,) = needs.outside_items(with_hidden=True)
        needs.hide(it["id"], it["version"])
        self.assertEqual(needs.outside_items(with_hidden=False), [])
        self.swap_same_size(f, "delete all")
        (shown,) = needs.outside_items(with_hidden=False)
        self.assertEqual(shown["id"], it["id"])
        self.assertNotEqual(shown["version"], it["version"])
        self.assertEqual(shown["text"], "delete all")

    def test_the_edit_surfaces_in_the_queue_beyond_the_hidden_read_window(self):
        self.watch()
        files = []
        with patch.object(needs, "_OUTSIDE_LIMIT", 2):
            for n in range(4):
                f = self.folder / f"h{n}.txt"
                f.write_text(f"body {n}")
                t = time.time() - n * 10  # h0 newest
                os.utime(f, (t, t))
                files.append(f)
            for _ in range(2):  # two pages of two
                for it in needs.build_queue()["items"]:
                    needs.hide(it["id"], it["version"])
            q = needs.build_queue()
            self.assertEqual(q["items"], [])
            # The oldest file lies past the two-file hidden read window.
            self.swap_same_size(files[3], "BODY 3")
            q = needs.build_queue()
            self.assertEqual([i["path"] for i in q["items"]], ["h3.txt"])

    def test_an_unchanged_dismissed_file_stays_hidden(self):
        self.watch()
        f = self.folder / "a.txt"
        f.write_text("approve it")
        (it,) = needs.outside_items(with_hidden=True)
        needs.hide(it["id"], it["version"])
        self.assertEqual(needs.outside_items(with_hidden=False), [])
        self.assertEqual(needs.build_queue()["items"], [])


class DocsPinTheStaleHideRule(unittest.TestCase):
    def test_architecture_states_the_refusal_without_a_precondition(self):
        text = (ROOT / "docs" / "architecture.md").read_text()
        self.assertRegex(text, r"a hide for a version older than the card's current one is refused")


if __name__ == "__main__":
    unittest.main()
