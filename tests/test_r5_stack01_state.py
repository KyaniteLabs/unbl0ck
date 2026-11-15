"""Round-5 findings for the state stores (stack 01): stale dismissals, link-safe locking, deeply
nested files, summary-cache shape and the shape of filed outside records."""

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_outside import needs

STEM = "0123456789abcdef0123456789abcdef"


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.data = needs.data_dir(self.home)


class AStaleDismissalIsJudgedUnderTheLock(Home):
    def test_a_newer_dismissal_made_before_the_lock_is_not_overwritten(self):
        """R5-needs-012 (class A): the check and the write must see the same stored value."""
        real_lock = needs._state_lock
        state = {"raced": False}

        @contextlib.contextmanager
        def racing(home):
            if not state["raced"]:
                state["raced"] = True
                needs.hide("kanban:b:t", "2", home)  # another tab hides version 2 first
            with real_lock(home):
                yield

        def queue(*a, **kw):  # the card is at version 1 until the other tab has hidden version 2
            return {"items": [{"id": "kanban:b:t", "version": "2" if state["raced"] else "1"}]}

        with patch.object(needs, "_state_lock", racing), patch.object(needs, "build_queue", queue):
            with self.assertRaises(needs.StaleHide):
                needs.hide("kanban:b:t", "1", self.home)
        self.assertEqual(needs.load_hidden(self.home), {"kanban:b:t": "2"})

    def test_a_dismissal_with_nothing_stored_still_works(self):
        needs.hide("kanban:b:t", "1", self.home)
        self.assertEqual(needs.load_hidden(self.home), {"kanban:b:t": "1"})


class TheLockFileIsNeverFollowedWithoutNoFollow(Home):
    def test_a_dangling_lock_link_is_refused_where_o_nofollow_is_missing(self):
        """R5-needs-017 (class C): the portable path checks for a link instead of trusting the flag."""
        outside = Path(self.tmp.name) / "outside-lock"
        (self.data / ".state.lock").symlink_to(outside)
        with patch.object(needs, "_NOFOLLOW", 0):
            with self.assertRaises(OSError):
                with needs._state_lock(self.home):
                    pass
            with self.assertRaises(OSError):
                needs.hide("chat:p:s", "1", self.home)
        self.assertFalse(outside.exists())


class Empty:
    def list_pending(self):
        return []


class ADeeplyNestedPendingFileIsDamageNotACrash(Home):
    def test_a_deep_pending_file_does_not_stop_the_pairing_source(self):
        """R5-needs-022 (class M): RecursionError from the parser is a damaged file, reported."""
        folder = self.home / "platforms" / "pairing"
        folder.mkdir(parents=True)
        (folder / "telegram-pending.json").write_text("[" * 200_000 + "]" * 200_000)
        errors: list[str] = []
        items = needs.pairing_items(self.home, errors=errors, store=lambda p: Empty())
        self.assertEqual(items, [])
        self.assertTrue(any(e.startswith("pairing[default]") for e in errors), errors)


class ASummaryCacheEntryOfAnotherShapeIsNotRewritten(Home):
    def test_prune_refuses_an_entry_that_is_not_a_summary_record(self):
        """R5-needs-023 (class G): a strict read checks entry shape; prune must not drop 42."""
        path = self.data / "bluf.json"
        path.write_text(json.dumps({"x": 42}))
        before = path.read_text()
        with self.assertRaises(RuntimeError):
            needs.prune_blufs([], self.home)
        self.assertEqual(path.read_text(), before)

    def test_saving_a_summary_refuses_the_same_damage(self):
        path = self.data / "bluf.json"
        path.write_text(json.dumps({"x": ["future"]}))
        before = path.read_text()
        with self.assertRaises(RuntimeError):
            needs.save_bluf("a", "1", "b", "w", self.home)
        self.assertEqual(path.read_text(), before)

    def test_ordinary_summary_records_are_still_pruned_and_saved(self):
        needs.save_bluf("a", "1", "b", "w", self.home)
        needs.save_bluf("b", "1", "b", "w", self.home)
        needs.prune_blufs([{"id": "a", "version": "1"}], self.home)
        self.assertEqual(sorted(json.loads((self.data / "bluf.json").read_text())), ["a"])


class AForeignJsonFileIsNeverTreatedAsAFiledItem(Home):
    def foreign(self, rec):
        d = needs._outside_dir(self.home)
        f = d / f"{STEM}.json"
        f.write_text(json.dumps(rec))
        return f

    def test_done_does_not_delete_a_record_this_plugin_did_not_file(self):
        """R5-needs-024 (class F): matching on a version alone is not ownership."""
        f = self.foreign({"version": "v", "text": "foreign"})
        with self.assertRaises(needs.SourceUnavailable):
            needs.done_outside(f"outside:filed:{STEM}", "v", self.home)
        self.assertTrue(f.exists())

    def test_lookup_refuses_the_same_record(self):
        self.foreign({"version": "v", "text": "foreign"})
        with self.assertRaises(needs.SourceUnavailable):
            needs.outside_lookup(f"outside:filed:{STEM}", self.home)

    def test_resolve_does_not_delete_it_either(self):
        f = self.foreign({"version": "v", "text": "foreign"})
        with self.assertRaises(needs.SourceUnavailable):
            needs.file_outside(
                {"action": "resolve", "id": f"outside:filed:{STEM}", "version": "v"}, home=self.home
            )
        self.assertTrue(f.exists())

    def test_a_record_that_was_filed_is_still_found_and_done(self):
        filed = needs.file_outside(
            {"kind": "notes", "title": "T", "body": "B", "act_where": "here", "dedupe_key": "k"},
            home=self.home,
        )
        self.assertEqual([i["id"] for i in needs.outside_items(self.home)], [filed["id"]])
        self.assertEqual(needs.outside_lookup(filed["id"], self.home)["id"], filed["id"])
        self.assertEqual(needs.done_outside(filed["id"], filed["version"], self.home)["done"], True)


if __name__ == "__main__":
    unittest.main()
