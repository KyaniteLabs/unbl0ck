"""Done on an outside item is local and recoverable; the agent's resolve is the permanent path."""

import json
import time
import unittest

from test_outside import OutsideFixture, needs


class DoneUndo(OutsideFixture):
    def done(self, it):
        return needs.done_outside(it["id"], it["version"])

    def queue(self):
        return needs.build_queue()["items"]

    def test_filed_done_keeps_the_record_hidden_and_marks_it_local_only(self):
        result = self.file(dedupe_key="a")
        (it,) = self.queue()
        reply = self.done(it)
        self.assertEqual(reply["done"], True)
        self.assertTrue(reply["local_only"])
        self.assertEqual(reply["undo_seconds"], needs.OUTSIDE_UNDO_SECONDS)
        self.assertTrue(self.record(result).exists())  # recoverable, not deleted
        self.assertEqual(self.queue(), [])
        self.assertEqual(needs.build_queue()["counts"]["asks"], 0)

    def test_undo_restores_the_same_record_with_profile_and_version(self):
        result = self.file(dedupe_key="a")
        (it,) = self.queue()
        self.done(it)
        self.assertTrue(needs.unhide(it["id"]))
        (back,) = self.queue()
        for key in ("id", "version", "profile", "session_id", "since", "title", "ask", "act_where"):
            self.assertEqual(back[key], it[key], key)
        self.assertNotIn("done_at", json.loads(self.record(result).read_text()))

    def test_undo_of_something_not_done_or_gone_is_false_and_changes_nothing(self):
        result = self.file(dedupe_key="a")
        (it,) = self.queue()
        self.assertFalse(needs.unhide(it["id"]))  # never done
        self.assertEqual(len(self.queue()), 1)
        self.assertFalse(needs.unhide("outside:filed:" + "0" * 32))
        self.assertTrue(self.record(result).exists())

    def test_undo_clears_done_and_not_for_me_together(self):
        result = self.file(dedupe_key="a")
        (it,) = self.queue()
        self.done(it)
        needs.hide(it["id"], it["version"])
        self.assertEqual(self.queue(), [])
        self.assertTrue(needs.unhide(it["id"]))
        self.assertEqual([i["id"] for i in self.queue()], [it["id"]])
        self.assertNotIn(it["id"], needs.load_hidden())
        self.assertNotIn("done_at", json.loads(self.record(result).read_text()))
        self.assertFalse(needs.unhide(it["id"]))

    def test_not_for_me_on_a_filed_item_is_undoable_through_unhide(self):
        result = self.file(dedupe_key="a")
        (it,) = self.queue()
        needs.hide(it["id"], it["version"])  # "Not for me" writes hidden.json, not done_at
        self.assertEqual(self.queue(), [])
        self.assertTrue(needs.unhide(it["id"]))
        self.assertEqual([i["id"] for i in self.queue()], [it["id"]])
        self.assertNotIn(it["id"], needs.load_hidden())
        self.assertFalse(needs.unhide(it["id"]))  # nothing left to restore
        self.assertTrue(self.record(result).exists())

    def test_consecutive_actions_undo_independently_in_any_order(self):
        self.file(dedupe_key="a", title="A")
        self.file(dedupe_key="b", title="B")
        self.watch()
        (self.folder / "n.txt").write_text("folder note")
        items = {i["title"]: i for i in self.queue()}
        self.assertEqual(set(items), {"A", "B", "folder note"})
        for it in items.values():
            self.done(it)
        self.assertEqual(self.queue(), [])
        self.assertTrue(needs.unhide(items["B"]["id"]))
        self.assertTrue(needs.unhide(items["folder note"]["id"]))
        self.assertEqual({i["title"] for i in self.queue()}, {"B", "folder note"})
        self.assertTrue(needs.unhide(items["A"]["id"]))
        self.assertEqual(len(self.queue()), 3)

    def test_done_twice_is_idempotent_and_keeps_the_first_timestamp(self):
        result = self.file(dedupe_key="a")
        (it,) = self.queue()
        self.done(it)
        first = json.loads(self.record(result).read_text())["done_at"]
        time.sleep(0.01)
        self.done(it)
        self.assertEqual(json.loads(self.record(result).read_text())["done_at"], first)

    def test_expired_done_cannot_be_undone_and_is_deleted(self):
        result = self.file(dedupe_key="a")
        (it,) = self.queue()
        self.done(it)
        path = self.record(result)
        rec = json.loads(path.read_text())
        rec["done_at"] -= needs.OUTSIDE_UNDO_SECONDS + 1
        path.write_text(json.dumps(rec))
        self.assertFalse(needs.unhide(it["id"]))
        self.assertFalse(path.exists())  # retention cleanup removed the text

    def test_polling_cleans_up_expired_done_records(self):
        result = self.file(dedupe_key="a")
        (it,) = self.queue()
        self.done(it)
        path = self.record(result)
        rec = json.loads(path.read_text())
        rec["done_at"] -= needs.OUTSIDE_UNDO_SECONDS + 1
        path.write_text(json.dumps(rec))
        self.assertEqual(self.queue(), [])
        self.assertFalse(path.exists())

    def test_done_records_are_bounded_oldest_first(self):
        paths = []
        for n in range(needs._OUTSIDE_DONE_KEEP + 3):
            result = self.file(dedupe_key=f"k{n}", title=f"T{n}")
            paths.append(self.record(result))
            (it,) = self.queue()
            self.done(it)
            # keep ordering unambiguous
            rec = json.loads(paths[-1].read_text())
            rec["done_at"] = time.time() - (needs._OUTSIDE_DONE_KEEP + 3 - n)
            paths[-1].write_text(json.dumps(rec))
        self.file(dedupe_key="trigger")
        (trigger,) = self.queue()
        self.done(trigger)
        surviving = [p for p in paths if p.exists()]
        self.assertLessEqual(len(surviving), needs._OUTSIDE_DONE_KEEP)
        self.assertFalse(paths[0].exists())

    def test_done_records_do_not_count_against_the_filed_cap(self):
        needs_cap = needs._OUTSIDE_LIMIT
        first = self.file(dedupe_key="first")
        (it,) = self.queue()
        self.done(it)
        for n in range(needs_cap - 1):
            self.file(dedupe_key=f"k{n}")
        self.file(dedupe_key="one-more")  # 200 active + 1 done: still room because done is excluded
        with self.assertRaises(ValueError):
            needs.file_outside(
                {"kind": "draft", "title": "t", "body": "b", "act_where": "w", "dedupe_key": "over"}
            )
        self.assertTrue(self.record(first).exists())

    def test_refiling_identical_content_keeps_it_done_but_a_change_resurfaces(self):
        self.file(dedupe_key="a")
        (it,) = self.queue()
        self.done(it)
        self.file(dedupe_key="a")  # agent repeats the same claim
        self.assertEqual(self.queue(), [])
        changed = self.file(dedupe_key="a", body="New draft text")
        (back,) = self.queue()
        self.assertEqual(back["id"], it["id"])
        self.assertNotEqual(back["version"], it["version"])
        self.assertNotIn("done_at", json.loads(self.record(changed).read_text()))

    def test_done_refuses_a_version_the_agent_changed_and_undo_does_not_resurrect_it(self):
        self.file(dedupe_key="a")
        (old,) = self.queue()
        self.file(dedupe_key="a", body="changed")
        with self.assertRaises(RuntimeError):
            self.done(old)
        (cur,) = self.queue()
        self.done(cur)
        self.file(dedupe_key="a", body="changed again")  # resurfaces as a newer version
        self.assertFalse(needs.unhide(cur["id"]))  # nothing left to undo
        self.assertEqual(len(self.queue()), 1)

    def test_agent_resolve_stays_the_permanent_external_completion(self):
        result = self.file(dedupe_key="a")
        (it,) = self.queue()
        self.done(it)
        resolved = json.loads(self.tools["unbl0ck_file"][2]({"action": "resolve", "dedupe_key": "a"}))
        self.assertTrue(resolved["resolved"])
        self.assertFalse(self.record(result).exists())
        self.assertFalse(needs.unhide(it["id"]))

    def test_folder_done_undo_never_touches_the_source_file(self):
        self.watch()
        f = self.folder / "call.txt"
        f.write_text("Read call notes")
        before = f.read_bytes(), f.stat().st_mtime_ns
        (it,) = self.queue()
        self.assertTrue(self.done(it)["local_only"])
        self.assertEqual(self.queue(), [])
        self.assertTrue(needs.unhide(it["id"]))
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual((f.read_bytes(), f.stat().st_mtime_ns), before)

    def test_changed_folder_file_resurfaces_after_done_and_old_undo_is_harmless(self):
        self.watch()
        f = self.folder / "call.txt"
        f.write_text("Read call notes")
        (it,) = self.queue()
        self.done(it)
        f.write_text("Updated call notes")
        (new,) = self.queue()
        self.assertNotEqual(new["version"], it["version"])
        self.assertFalse(needs.unhide("outside:folder:" + "f" * 64))

    def test_corrupt_done_record_is_never_deleted_by_cleanup(self):
        result = self.file(dedupe_key="a")
        path = self.record(result)
        path.write_text("{not json")
        self.file(dedupe_key="other")
        self.assertTrue(path.exists())  # unreadable records are left for repair, not purged


class DoneUndoApi(OutsideFixture):
    def setUp(self):
        super().setUp()
        import importlib.util
        import sys

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from test_outside import ROOT

        spec = importlib.util.spec_from_file_location("unbl0ck_done_undo_api", ROOT / "dashboard/plugin_api.py")
        self.api = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.api
        spec.loader.exec_module(self.api)
        self.api._needs = lambda: needs
        self.api._loaded_ctx = lambda: None
        app = FastAPI()
        app.include_router(self.api.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_done_then_unhide_round_trips_over_http(self):
        self.file(dedupe_key="a")
        (it,) = needs.build_queue()["items"]
        r = self.client.post("/outside/done", json={"id": it["id"], "version": it["version"]})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["local_only"])
        self.assertEqual(self.client.get("/queue").json()["count"], 0)
        r = self.client.post("/unhide", json={"id": it["id"]})
        self.assertEqual((r.status_code, r.json()["restored"]), (200, True))
        self.assertEqual(self.client.get("/queue").json()["count"], 1)
        r = self.client.post("/unhide", json={"id": it["id"]})
        self.assertEqual((r.status_code, r.json()["restored"]), (200, False))


class UndoTokens(OutsideFixture):
    """Round-2 review: an Undo names the dismissal it undoes and clears every marker of it."""

    def queue(self):
        return needs.build_queue()["items"]

    def test_old_undo_cannot_remove_a_newer_done(self):
        result = self.file(dedupe_key="a")
        (it,) = self.queue()
        first = needs.done_outside(it["id"], it["version"])["token"]
        self.assertTrue(needs.unhide(it["id"], token=first))
        self.file(dedupe_key="a", body="Changed content, needs you again.")
        (again,) = self.queue()
        self.assertNotEqual(again["version"], it["version"])
        second = needs.done_outside(again["id"], again["version"])["token"]
        self.assertNotEqual(first, second)
        self.assertFalse(needs.unhide(it["id"], token=first))  # the stale Undo is refused
        self.assertEqual(self.queue(), [])
        self.assertIn("done_at", json.loads(self.record(result).read_text()))
        self.assertTrue(needs.unhide(it["id"], token=second))
        self.assertEqual(len(self.queue()), 1)

    def test_old_not_for_me_undo_cannot_remove_a_newer_hide(self):
        self.file(dedupe_key="a")
        (it,) = self.queue()
        old = needs.hide(it["id"], "v-old")
        new = needs.hide(it["id"], it["version"])  # dismissed again at the current version
        self.assertFalse(needs.unhide(it["id"], token=old))
        self.assertEqual(self.queue(), [])
        self.assertTrue(needs.unhide(it["id"], token=new))

    def test_done_and_not_for_me_on_one_version_are_cleared_together(self):
        for name in ("done", "hidden"):
            with self.subTest(undo=name):
                result = self.file(dedupe_key=name)
                item = next(i for i in self.queue() if i["id"].endswith(self.record(result).stem))
                hide_token = needs.hide(item["id"], item["version"])  # tab A: Not for me
                done_token = needs.done_outside(item["id"], item["version"])["token"]  # tab B: Done
                self.assertTrue(all(i["id"] != item["id"] for i in self.queue()))
                token = done_token if name == "done" else hide_token
                self.assertTrue(needs.unhide(item["id"], token=token))
                self.assertIn(item["id"], [i["id"] for i in self.queue()])  # not left hidden by the other marker
                self.assertNotIn("done_at", json.loads(self.record(result).read_text()))
                self.assertNotIn(item["id"], needs.load_hidden())
                self.assertFalse(needs.unhide(item["id"], token=token))
                needs.done_outside(item["id"], item["version"])
                needs.unhide(item["id"])  # cleanup: next subtest starts clean

    def test_a_stale_hide_token_cannot_remove_a_newer_done(self):
        result = self.file(dedupe_key="a")
        (old,) = self.queue()
        needs.hide(old["id"], old["version"])  # tab 1: Not for me at v1
        self.file(dedupe_key="a", body="Changed content, needs you again.")
        (new,) = self.queue()
        self.assertNotEqual(new["version"], old["version"])
        needs.done_outside(new["id"], new["version"])  # tab 2: Done at v2
        self.assertFalse(needs.unhide(old["id"], token=old["version"]))
        self.assertIn("done_at", json.loads(self.record(result).read_text()))
        self.assertEqual(self.queue(), [])

    def test_a_failed_second_write_leaves_the_done_undo_retryable(self):
        from unittest import mock

        result = self.file(dedupe_key="a")
        (it,) = self.queue()
        needs.hide(it["id"], it["version"])
        token = needs.done_outside(it["id"], it["version"])["token"]
        real = needs._write_json
        calls = []

        def flaky(path, data, *a, **k):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(path, data, *a, **k)

        with mock.patch.object(needs, "_write_json", flaky):
            with self.assertRaises(OSError):
                needs.unhide(it["id"], token=token)
        self.assertTrue(needs.unhide(it["id"], token=token))  # the retry still finds its token
        self.assertEqual([i["id"] for i in self.queue()], [it["id"]])
        self.assertNotIn("done_at", json.loads(self.record(result).read_text()))

    def test_desktop_sends_the_token_of_the_dismissal_it_undoes(self):
        from pathlib import Path

        for name in ("desktop/src/plugin.src.js", "desktop/plugin.js"):
            src = (Path(__file__).resolve().parents[1] / name).read_text()
            with self.subTest(name):
                self.assertIn("token: r.token", src)
                self.assertIn("{ id: u.id, token: u.token }", src)

    def test_tokenless_callers_keep_the_old_behavior(self):
        self.file(dedupe_key="a")
        (it,) = self.queue()
        needs.done_outside(it["id"], it["version"])
        self.assertTrue(needs.unhide(it["id"]))


if __name__ == "__main__":
    unittest.main()
