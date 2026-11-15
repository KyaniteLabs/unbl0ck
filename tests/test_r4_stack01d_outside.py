"""Round-4 findings 01-078 and friends: things filed from outside Hermes."""

import os
import unittest
from unittest.mock import patch

from test_outside import OutsideFixture, needs


class FiledVersionFollowsContent(OutsideFixture):
    def file_in(self, session, **changes):
        args = {
            "kind": "draft",
            "title": "Reply to customer",
            "body": "Ready for you to send.",
            "act_where": "Outlook",
            "dedupe_key": "k",
            **changes,
        }
        return needs.file_outside(args, self.home, profile="work", session_id=session)

    def test_identical_content_from_another_session_keeps_the_version(self):
        """01-078 (class J): the session that filed it is provenance, not content."""
        a = self.file_in("session-a")
        b = self.file_in("session-b")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(a["version"], b["version"])

    def test_a_dismissal_survives_a_refile_from_another_session(self):
        a = self.file_in("session-a")
        needs.hide(a["id"], a["version"], self.home)
        self.file_in("session-b")
        self.assertEqual(needs.build_queue(self.home)["items"], [])

    def test_changed_content_still_changes_the_version(self):
        a = self.file_in("session-a")
        b = self.file_in("session-a", body="Different text")
        self.assertNotEqual(a["version"], b["version"])

    def test_the_latest_session_is_still_recorded(self):
        self.file_in("session-a")
        r = self.file_in("session-b")
        rec = needs._read_json(self.record(r), strict=True)
        self.assertEqual(rec["session_id"], "session-b")


class ChangedWhileReadingFile:
    """A file object whose content changes during the read, as an editor saving would."""

    def __init__(self, fh, path):
        self._fh = fh
        self._path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()

    def fileno(self):
        return self._fh.fileno()

    def read(self, *args):
        data = self._fh.read(*args)
        with open(self._path, "ab") as out:
            out.write(b" and more")
        return data


class WatchedFolderCoverage(OutsideFixture):
    def nested_eml(self, depth):
        head = "From: a@example.test\nSubject: Deep\nMIME-Version: 1.0\n"
        body = ""
        for i in range(depth):
            body += f'Content-Type: multipart/mixed; boundary="b{i}"\n\n--b{i}\n'
        body += "Content-Type: text/plain\n\nhello\n"
        return head + body

    def test_a_file_that_changes_while_it_is_read_is_reported_not_silently_dropped(self):
        """01-083 (class E): the poll names the file it could not read coherently."""
        self.watch()
        target = self.folder / "note.txt"
        target.write_text("Question one?")
        real_fdopen = os.fdopen

        def fdopen(fd, *args, **kwargs):
            return ChangedWhileReadingFile(real_fdopen(fd, *args, **kwargs), target)

        errors = []
        with patch.object(needs.os, "fdopen", fdopen):
            items = needs.outside_items(self.home, errors=errors)
        self.assertEqual(items, [])
        self.assertTrue(any("note.txt" in e and "changed" in e for e in errors), errors)

    def test_a_deeply_nested_message_does_not_abort_the_rest_of_the_folder(self):
        """01-084 (class G): one hostile file is an error line; the other files still load."""
        self.watch()
        (self.folder / "a-deep.eml").write_text(self.nested_eml(1500))
        (self.folder / "b-good.txt").write_text("Fine question?")
        errors = []
        items = needs.outside_items(self.home, errors=errors)
        self.assertIn("Fine question?", [i["text"] for i in items])
        self.assertTrue(any("a-deep.eml" in e for e in errors), errors)

    def test_a_lookup_of_a_deeply_nested_message_is_unavailable_not_a_crash(self):
        self.watch()
        (self.folder / "a-deep.eml").write_text(self.nested_eml(1500))
        item_id = "outside:folder:" + needs.hashlib.sha256(
            str((self.folder / "a-deep.eml").resolve()).encode()
        ).hexdigest()
        with self.assertRaises(needs.SourceUnavailable):
            needs.outside_lookup(item_id, self.home)


if __name__ == "__main__":
    unittest.main()
