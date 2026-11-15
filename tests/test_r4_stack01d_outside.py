"""Round-4 findings 01-078 and friends: things filed from outside Hermes."""

import unittest

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


if __name__ == "__main__":
    unittest.main()
