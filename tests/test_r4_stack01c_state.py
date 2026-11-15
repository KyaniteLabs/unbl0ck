"""Round-4 findings 01-051, 01-057, 01-059, 01-060, 01-071: where state is read from. Each test is the
finding's reproducer on the real code path (real files, real sqlite)."""

import json
import os
import stat
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from r4c_common import ASK, RID, HomeCase, chat_db, needs


def listing_denied(path: Path, case: unittest.TestCase):
    path.chmod(0)
    case.addCleanup(path.chmod, 0o700)
    if os.access(path, os.R_OK):
        case.skipTest("directory permissions are not enforced here")


class ChatDatabaseContainment(HomeCase):
    def test_a_symlinked_profile_directory_is_not_searched(self):
        """01-051 (class C): profiles/<name> pointing at another Hermes home is refused and reported."""
        other = self.base / "other-home"
        chat_db(other / "state.db")
        (self.home / "profiles").mkdir()
        (self.home / "profiles" / "linked").symlink_to(other, target_is_directory=True)
        errors = []
        self.assertEqual(needs.chat_items(self.home, errors=errors), [])
        self.assertTrue(any("linked" in e for e in errors), errors)

    def test_a_symlinked_profile_database_is_not_opened(self):
        """01-051: profiles/work/state.db pointing outside the home is refused and reported."""
        other = self.base / "elsewhere"
        chat_db(other / "state.db")
        (self.home / "profiles" / "work").mkdir(parents=True)
        (self.home / "profiles" / "work" / "state.db").symlink_to(other / "state.db")
        errors = []
        self.assertEqual(needs.chat_items(self.home, errors=errors), [])
        self.assertTrue(any("work" in e for e in errors), errors)
        self.assertEqual(needs._state_dbs(self.home), [])

    def test_a_symlinked_root_database_is_not_opened(self):
        other = self.base / "elsewhere"
        chat_db(other / "state.db")
        (self.home / "state.db").symlink_to(other / "state.db")
        errors = []
        self.assertEqual(needs.chat_items(self.home, errors=errors), [])
        self.assertTrue(errors)

    def test_a_real_profile_is_still_searched(self):
        chat_db(self.home / "profiles" / "work" / "state.db")
        errors = []
        (item,) = needs.chat_items(self.home, errors=errors)
        self.assertEqual((item["profile"], errors), ("work", []))


class ListingFailures(HomeCase):
    def test_an_unlistable_profiles_directory_is_reported(self):
        """01-059 (class G): Path.glob hides a denied listing; the profiles directory is a failed source."""
        chat_db(self.home / "state.db")
        profiles = self.home / "profiles"
        profiles.mkdir()
        listing_denied(profiles, self)
        errors = []
        needs.chat_items(self.home, errors=errors)
        self.assertTrue(any(e.startswith("chat") for e in errors), errors)

    def test_an_unlistable_waits_directory_is_reported_not_empty(self):
        waits = needs._waits_dir(self.home)
        listing_denied(waits, self)
        errors = []
        self.assertEqual(needs.wait_items(self.home, errors=errors), [])
        self.assertTrue(any(e.startswith("prompt") for e in errors), errors)

    def test_an_unlistable_waits_directory_makes_a_lookup_unavailable_not_missing(self):
        needs.record_wait(self.home, request_id=RID, kind="clarify", prompt="Pick one?", platform="cli")
        listing_denied(needs._waits_dir(self.home), self)
        with self.assertRaises(needs.SourceUnavailable):
            needs.source_text(f"prompt:{RID}", RID)

    def test_an_unlistable_outside_directory_is_reported_not_empty(self):
        listing_denied(needs._outside_dir(self.home), self)
        errors = []
        self.assertEqual(needs.outside_items(self.home, errors=errors), [])
        self.assertTrue(any(e.startswith("outside") for e in errors), errors)

    def test_resolving_in_an_unlistable_outside_directory_is_a_failure_not_no_match(self):
        listing_denied(needs._outside_dir(self.home), self)
        with self.assertRaises(OSError):
            needs.file_outside({"action": "resolve", "dedupe_key": "k"}, self.home)

    def test_an_unlistable_pairing_profiles_directory_is_reported(self):
        (self.home / "profiles").mkdir()
        listing_denied(self.home / "profiles", self)
        errors = []
        needs.pairing_items(self.home, errors=errors, store=lambda p: type("S", (), {"list_pending": lambda s: []})())
        self.assertTrue(any(e.startswith("pairing") for e in errors), errors)


class WaitStoreContainment(HomeCase):
    def record(self, **over):
        rec = {
            "request_id": RID,
            "kind": "clarify",
            "prompt": "foreign needle?",
            "prompt_cut": False,
            "session_id": "",
            "platform": "cli",
            "profile": "default",
            "pid": os.getpid(),
            "proc_start": needs._proc_start(os.getpid()),
            "since": int(time.time()),
        }
        rec.update(over)
        return rec

    def test_a_symlinked_wait_record_is_not_read(self):
        """01-060 (class C): waits/<rid>.json pointing outside the store never enters the queue."""
        outside = self.base / "foreign.json"
        outside.write_text(json.dumps(self.record()))
        (needs._waits_dir(self.home) / f"{RID}.json").symlink_to(outside)
        errors = []
        self.assertEqual(needs.wait_items(self.home, errors=errors), [])
        self.assertTrue(errors)
        with self.assertRaises(needs.SourceUnavailable):
            needs.source_text(f"prompt:{RID}", RID)

    def test_a_symlinked_waits_directory_is_refused_and_nothing_outside_is_deleted(self):
        foreign = self.base / "foreign-waits"
        foreign.mkdir()
        (foreign / f"{RID}.json").write_text(json.dumps(self.record(pid=2**22 + 7)))  # stale: a dead pid
        (self.home / "plugin-data" / "unbl0ck").mkdir(parents=True)
        (self.home / "plugin-data" / "unbl0ck" / "waits").symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(OSError):
            needs.wait_items(self.home)
        needs.clear_wait(self.home, request_id=RID)
        self.assertTrue((foreign / f"{RID}.json").exists())

    def test_a_symlinked_plugin_data_directory_is_refused(self):
        """01-060/01-071: an ancestor of the data directory that is a link redirects every write."""
        foreign = self.base / "foreign-plugin-data"
        (foreign / "unbl0ck").mkdir(parents=True)
        (self.home / "plugin-data").symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(OSError):
            needs.data_dir(self.home)
        needs.record_wait(self.home, request_id=RID, kind="clarify", prompt="x?", platform="cli")
        self.assertEqual(list((foreign / "unbl0ck").rglob("*")), [])


class OutsideStoreContainment(HomeCase):
    def setUp(self):
        super().setUp()
        self.foreign = self.base / "foreign-outside"
        self.foreign.mkdir()
        self.victim = self.foreign / ("a" * 32 + ".json")
        self.victim.write_text(json.dumps({"version": "v", "done_at": 1}))
        (self.home / "plugin-data" / "unbl0ck").mkdir(parents=True)
        (self.home / "plugin-data" / "unbl0ck" / "outside").symlink_to(self.foreign, target_is_directory=True)

    def test_done_does_not_delete_through_a_symlinked_outside_directory(self):
        """01-071 (class C): an ancestor link is refused, not only each leaf."""
        with self.assertRaises(OSError):
            needs.done_outside("outside:filed:" + "a" * 32, "v", self.home)
        self.assertTrue(self.victim.exists())

    def test_filing_does_not_write_through_a_symlinked_outside_directory(self):
        with self.assertRaises(OSError):
            needs.file_outside(
                {"kind": "draft", "title": "t", "body": "b", "act_where": "w"}, self.home
            )
        self.assertEqual([p.name for p in self.foreign.iterdir()], [self.victim.name])

    def test_resolve_does_not_delete_through_a_symlinked_outside_directory(self):
        with self.assertRaises(OSError):
            needs.file_outside({"action": "resolve", "id": "outside:filed:" + "a" * 32}, self.home)
        self.assertTrue(self.victim.exists())

    def test_polling_does_not_read_through_a_symlinked_outside_directory(self):
        errors = []
        with self.assertRaises(OSError):
            needs.outside_items(self.home, errors=errors)
        queue = needs.build_queue(self.home)
        self.assertTrue(any(e.startswith("outside") for e in queue["errors"]))
        self.assertTrue(self.victim.exists())


class PairingStoreFailures(HomeCase):
    class Empty:
        def list_pending(self):
            return []

    def pending(self, text):
        d = self.home / "platforms" / "pairing"
        d.mkdir(parents=True, exist_ok=True)
        (d / "telegram-pending.json").write_text(text)
        return d / "telegram-pending.json"

    def test_a_corrupt_pending_file_is_a_failed_source_not_an_empty_one(self):
        """01-057 (class G): the stock store loads a corrupt file as {}; the source must still say so."""
        self.pending("{not json SECRETCODE")
        errors = []
        items = needs.pairing_items(self.home, errors=errors, store=lambda p: self.Empty())
        self.assertEqual(items, [])
        self.assertTrue(any(e.startswith("pairing[default]") for e in errors), errors)
        self.assertFalse(any("SECRETCODE" in e for e in errors))

    def test_a_corrupt_pending_file_marks_the_queue_incomplete(self):
        self.pending("[1, 2")
        with patch.object(needs, "_stock_pairing_store", lambda p: self.Empty()), patch.object(
            needs, "kanban_items", lambda **kw: []
        ):
            queue = needs.build_queue(self.home)
        self.assertTrue(any(e.startswith("pairing") for e in queue["errors"]), queue["errors"])

    def test_a_valid_empty_or_missing_pending_file_is_clean(self):
        for text in ("{}", '{"abc": {"user_id": "1"}}'):
            self.pending(text)
            errors = []
            needs.pairing_items(self.home, errors=errors, store=lambda p: self.Empty())
            self.assertEqual(errors, [], text)
        (self.home / "platforms" / "pairing" / "telegram-pending.json").unlink()
        errors = []
        needs.pairing_items(self.home, errors=errors, store=lambda p: self.Empty())
        self.assertEqual(errors, [])

    def test_a_non_object_pending_file_is_a_failed_source(self):
        self.pending("[]")
        errors = []
        needs.pairing_items(self.home, errors=errors, store=lambda p: self.Empty())
        self.assertTrue(errors)


class StateDocs(unittest.TestCase):
    def test_the_readme_says_a_damaged_pending_file_is_a_failed_source(self):
        """01-057 (class I): the README names the one check made on the pairing files."""
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        self.assertIn("report a damaged file as a failed Pairing source", readme)


if __name__ == "__main__":
    unittest.main()
