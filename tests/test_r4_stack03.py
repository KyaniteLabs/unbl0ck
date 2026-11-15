"""Round-4 review findings owned by the recoverable-Done branch, group by group."""

import importlib.util
import json
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_outside import OutsideFixture, needs
from test_operator_recovery import HARNESS

ROOT = Path(__file__).resolve().parents[1]


class FiledFixture(OutsideFixture):
    def queue(self):
        return needs.build_queue()["items"]

    def outside(self):
        return needs.data_dir(self.home) / "outside"

    def rewrite(self, result, **changes):
        path = self.record(result)
        rec = json.loads(path.read_text())
        rec.update(changes)
        path.write_text(json.dumps(rec))

    def done_one(self, key="a", **extra):
        result = self.file(dedupe_key=key, **extra)
        item = next(i for i in self.queue() if i["id"] == result["id"])
        return result, item, needs.done_outside(item["id"], item["version"])


class DismissalIdentity(FiledFixture):
    """03-005, 03-006, 03-008: an Undo token names one dismissal, never a content version."""

    def test_a_consumed_hide_token_cannot_clear_a_later_done(self):
        """03-006: hide v, undo it, Done the unchanged item, replay the hide token."""
        result = self.file(dedupe_key="a")
        (item,) = self.queue()
        hide_token = needs.hide(item["id"], item["version"])
        self.assertTrue(needs.unhide(item["id"], token=hide_token))
        needs.done_outside(item["id"], item["version"])
        self.assertFalse(needs.unhide(item["id"], token=hide_token))
        self.assertFalse(needs.unhide(item["id"], token=item["version"]))
        self.assertEqual(self.queue(), [])
        self.assertIn("done_at", json.loads(self.record(result).read_text()))

    def test_two_hides_of_one_version_have_different_tokens(self):
        """03-006: hide v, undo, hide v again; the first token must not clear the second hide."""
        self.file(dedupe_key="a")
        (item,) = self.queue()
        first = needs.hide(item["id"], item["version"])
        self.assertTrue(needs.unhide(item["id"], token=first))
        second = needs.hide(item["id"], item["version"])
        self.assertNotEqual(first, second)
        self.assertFalse(needs.unhide(item["id"], token=first))
        self.assertEqual(self.queue(), [])
        self.assertTrue(needs.unhide(item["id"], token=second))
        self.assertEqual(len(self.queue()), 1)

    def test_hidden_file_still_maps_ids_to_versions(self):
        """The marker carries its token without changing what load_hidden reports."""
        needs.hide("a", "1", self.home)
        self.assertEqual(needs.load_hidden(self.home), {"a": "1"})
        self.assertEqual(needs.visibility(needs.load_hidden(self.home), "a", "1"), needs.Visibility.HIDDEN)

    def test_done_tokens_are_fresh_even_when_the_clock_repeats(self):
        """03-008: Done, Undo, Done at an identical timestamp still names two dismissals."""
        result = self.file(dedupe_key="a")
        (item,) = self.queue()
        with patch.object(needs.time, "time", lambda: 1_700_000_000.0):
            first = needs.done_outside(item["id"], item["version"])["token"]
            self.assertTrue(needs.unhide(item["id"], token=first))
            second = needs.done_outside(item["id"], item["version"])["token"]
            self.assertNotEqual(first, second)
            self.assertFalse(needs.unhide(item["id"], token=first))
            self.assertIn("done_at", json.loads(self.record(result).read_text()))
            self.assertTrue(needs.unhide(item["id"], token=second))

    def test_the_record_version_is_not_a_done_token(self):
        """03-006: the old version fallback let any hide token for this version clear a Done."""
        self.file(dedupe_key="a")
        (item,) = self.queue()
        needs.done_outside(item["id"], item["version"])
        self.assertFalse(needs.unhide(item["id"], token=item["version"]))

    def test_refiling_identical_content_keeps_the_done_token_valid(self):
        result, item, reply = self.done_one()
        self.file(dedupe_key="a")  # identical content: stays Done
        self.assertTrue(needs.unhide(item["id"], token=reply["token"]))

    def test_undo_of_a_hide_after_the_filed_item_changed_restores_nothing(self):
        """03-005: the marker names v1 but the record is v2: nothing was hidden, nothing restored."""
        self.file(dedupe_key="a")
        (old,) = self.queue()
        token = needs.hide(old["id"], old["version"])
        self.file(dedupe_key="a", body="Changed content, needs you again.")
        (new,) = self.queue()
        self.assertNotEqual(new["version"], old["version"])
        self.assertFalse(needs.unhide(old["id"], token=token))

    def test_undo_of_a_hide_after_the_filed_item_was_resolved_restores_nothing(self):
        """03-005."""
        result = self.file(dedupe_key="a")
        (item,) = self.queue()
        token = needs.hide(item["id"], item["version"])
        needs.file_outside({"action": "resolve", "id": item["id"], "version": item["version"]})
        self.assertFalse(self.record(result).exists())
        self.assertFalse(needs.unhide(item["id"], token=token))

    def test_undo_of_a_folder_hide_after_the_file_changed_restores_nothing(self):
        """03-005: folder items carry the same contract as filed ones."""
        self.watch()
        note = self.folder / "n.txt"
        note.write_text("first")
        (item,) = self.queue()
        reply = needs.done_outside(item["id"], item["version"])
        self.assertTrue(reply["token"])
        note.write_text("second, longer")
        (changed,) = self.queue()  # resurfaced
        self.assertNotEqual(changed["version"], item["version"])
        self.assertFalse(needs.unhide(item["id"], token=reply["token"]))

    def test_undo_of_a_folder_hide_that_still_applies_restores_it(self):
        self.watch()
        (self.folder / "n.txt").write_text("first")
        (item,) = self.queue()
        reply = needs.done_outside(item["id"], item["version"])
        self.assertEqual(self.queue(), [])
        self.assertTrue(needs.unhide(item["id"], token=reply["token"]))
        self.assertEqual(len(self.queue()), 1)


class DoneRecordValidation(FiledFixture):
    """03-009, 03-010: cleanup deletes only records it can prove are expired Done records."""

    def test_boolean_done_at_is_preserved_and_reported(self):
        result = self.file(dedupe_key="a")
        self.rewrite(result, done_at=False)
        self.file(dedupe_key="b")  # triggers cleanup
        self.assertTrue(self.record(result).exists())
        errors = needs.build_queue()["errors"]
        self.assertTrue(any("done" in e.lower() and self.record(result).name in e for e in errors), errors)

    def test_nonfinite_done_at_is_preserved_and_reported(self):
        result = self.file(dedupe_key="a")
        path = self.record(result)
        rec = json.loads(path.read_text())
        path.write_text(json.dumps({**rec, "done_at": float("nan")}))
        self.file(dedupe_key="b")
        self.assertTrue(path.exists())
        self.assertTrue(any(self.record(result).name in e for e in needs.build_queue()["errors"]))

    def test_a_foreign_json_file_with_done_at_is_never_deleted(self):
        outside = self.outside()
        self.file(dedupe_key="a")
        backup = outside / "backup.json"
        backup.write_text(json.dumps({"done_at": True, "text": "recoverable data"}))
        old = outside / ("f" * 32 + ".json")
        old.write_text(json.dumps({"done_at": 1, "text": "not ours: wrong identity"}))
        self.file(dedupe_key="b")
        self.assertTrue(backup.exists())
        self.assertTrue(old.exists())  # id does not match the file name: not a record we wrote

    def test_a_clock_moved_back_does_not_destroy_the_undo_window(self):
        """03-010: Done at T, clock back more than a minute, poll: still undoable."""
        result, item, reply = self.done_one()
        done_at = json.loads(self.record(result).read_text())["done_at"]
        with patch.object(needs.time, "time", lambda: done_at - 600):
            self.assertEqual(self.queue(), [])
            self.assertTrue(self.record(result).exists())
            self.assertTrue(needs.unhide(item["id"], token=reply["token"]))
        self.assertEqual(len(self.queue()), 1)

    def test_a_record_stamped_in_the_future_expires_after_a_full_window_not_never(self):
        result, item, reply = self.done_one()
        done_at = json.loads(self.record(result).read_text())["done_at"]
        with patch.object(needs.time, "time", lambda: done_at - 600):
            self.queue()  # clock behind: the window restarts from this clock
        restamped = json.loads(self.record(result).read_text())["done_at"]
        self.assertLessEqual(restamped, done_at - 600 + 1)
        with patch.object(needs.time, "time", lambda: restamped + needs.OUTSIDE_UNDO_SECONDS + 5):
            self.queue()
        self.assertFalse(self.record(result).exists())


class RetentionIsReportedAndBounded(FiledFixture):
    """03-012, 03-014, 03-015."""

    def test_a_failed_cleanup_is_reported_in_the_queue_errors(self):
        """03-014."""
        result, item, reply = self.done_one()
        self.rewrite(result, done_at=time.time() - needs.OUTSIDE_UNDO_SECONDS - 10)
        real = Path.unlink

        def fail(path, *a, **kw):
            if path.name == self.record(result).name:
                raise PermissionError("denied")
            return real(path, *a, **kw)

        with patch.object(Path, "unlink", fail):
            q = needs.build_queue()
        self.assertTrue(any("could not remove" in e and "1" in e for e in q["errors"]), q["errors"])
        self.assertTrue(self.record(result).exists())

    def test_a_failed_eviction_after_done_still_returns_the_token(self):
        """03-015: the dismissal committed; the client must get its token and Undo."""
        keep = needs._OUTSIDE_DONE_KEEP
        with patch.object(needs, "_OUTSIDE_DONE_KEEP", 1):
            first, first_item, _ = self.done_one("first")
            self.rewrite(first, done_at=time.time() - 30)
            second = self.file(dedupe_key="second")
            (item,) = self.queue()
            real = Path.unlink

            def fail(path, *a, **kw):
                if path.name == self.record(first).name:
                    raise PermissionError("denied")
                return real(path, *a, **kw)

            with patch.object(Path, "unlink", fail):
                reply = needs.done_outside(item["id"], item["version"])
            self.assertTrue(reply["token"])
            self.assertEqual(reply["cleanup_failed"], 1)
            self.assertTrue(needs.unhide(item["id"], token=reply["token"]))
        self.assertGreaterEqual(keep, 1)

    def test_an_undeletable_expired_record_does_not_block_unrelated_mutations(self):
        """03-015."""
        result, item, reply = self.done_one("old")
        self.rewrite(result, done_at=time.time() - needs.OUTSIDE_UNDO_SECONDS - 10)
        real = Path.unlink

        def fail(path, *a, **kw):
            if path.name == self.record(result).name:
                raise PermissionError("denied")
            return real(path, *a, **kw)

        with patch.object(Path, "unlink", fail):
            other = self.file(dedupe_key="other")
        self.assertTrue(other["ok"])

    def test_id_only_resolve_does_not_read_every_entry_of_a_huge_folder(self):
        """03-012: cleanup and the existence check are bounded, not proportional to the folder."""
        result = self.file(dedupe_key="a")
        (item,) = self.queue()
        outside = self.outside()
        for n in range(3000):
            (outside / f"junk{n}.json").write_text("{not json")
        reads = []
        real = needs._read_json

        def counting(path, *a, **kw):
            reads.append(path)
            return real(path, *a, **kw)

        with patch.object(needs, "_read_json", counting):
            needs.file_outside({"action": "resolve", "id": item["id"], "version": item["version"]})
        self.assertLess(len(reads), needs._PURGE_SCAN_LIMIT + 50)
        self.assertFalse(self.record(result).exists())

    def test_filing_into_a_folder_over_the_scan_bound_is_refused_not_unbounded(self):
        self.file(dedupe_key="a")
        outside = self.outside()
        for n in range(needs._PURGE_SCAN_LIMIT + 20):
            (outside / f"junk{n}.json").write_text("{}")
        reads = []
        real = needs._read_json

        def counting(path, *a, **kw):
            reads.append(path)
            return real(path, *a, **kw)

        with patch.object(needs, "_read_json", counting):
            with self.assertRaises(RuntimeError):
                needs.file_outside({"kind": "draft", "title": "t", "body": "b", "act_where": "w", "dedupe_key": "x"})
        self.assertLess(len(reads), needs._PURGE_SCAN_LIMIT + 50)


class CapacityOnReactivation(FiledFixture):
    """03-011: reactivating a Done record counts against the active cap."""

    def fill(self, limit):
        a = self.file(dedupe_key="a")
        (item,) = self.queue()
        needs.done_outside(item["id"], item["version"])
        for n in range(limit):
            self.file(dedupe_key=f"k{n}")
        return a, item

    def test_refiling_changed_content_over_a_done_record_respects_the_cap(self):
        with patch.object(needs, "_OUTSIDE_LIMIT", 2):
            a, item = self.fill(2)
            with self.assertRaises(ValueError):
                needs.file_outside(
                    {"kind": "draft", "title": "Reply to customer", "body": "Changed content, needs you again.",
                     "act_where": "Outlook → Drafts", "dedupe_key": "a"}
                )
            active = [i for i in self.queue()]
            self.assertEqual(len(active), 2)
            self.assertIn("done_at", json.loads(self.record(a).read_text()))  # untouched

    def test_refiling_unchanged_content_over_a_done_record_stays_done_and_allowed(self):
        with patch.object(needs, "_OUTSIDE_LIMIT", 2):
            a, item = self.fill(2)
            self.file(dedupe_key="a")
            self.assertEqual(len(self.queue()), 2)

    def test_undo_of_done_is_refused_when_the_queue_is_full(self):
        with patch.object(needs, "_OUTSIDE_LIMIT", 2):
            a, item = self.fill(2)
            with self.assertRaises(RuntimeError) as ctx:
                needs.unhide(item["id"])
            self.assertIn("full", str(ctx.exception))
            self.assertEqual(len(self.queue()), 2)
            self.assertIn("done_at", json.loads(self.record(a).read_text()))

    def test_undo_still_works_with_room(self):
        with patch.object(needs, "_OUTSIDE_LIMIT", 3):
            a, item = self.fill(2)
            self.assertTrue(needs.unhide(item["id"]))
            self.assertEqual(len(self.queue()), 3)

    def test_the_cap_error_is_a_409_over_http(self):
        client = HttpFixture.client_for(self)
        with patch.object(needs, "_OUTSIDE_LIMIT", 2):
            a, item = self.fill(2)
            r = client.post("/unhide", json={"id": item["id"]})
        self.assertEqual(r.status_code, 409)


class HttpFixture:
    @staticmethod
    def client_for(case):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        spec = importlib.util.spec_from_file_location("unbl0ck_r4_stack03_api", ROOT / "dashboard/plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = api
        spec.loader.exec_module(api)
        api._needs = lambda: needs
        api._loaded_ctx = lambda: None
        app = FastAPI()
        app.include_router(api.router)
        client = TestClient(app)
        case.addCleanup(client.close)
        return client


class TokenApi(FiledFixture):
    """03-016: the HTTP contract carries tokens in both directions."""

    def setUp(self):
        super().setUp()
        self.client = HttpFixture.client_for(self)

    def test_done_returns_a_token_and_unhide_honors_it(self):
        self.file(dedupe_key="a")
        (item,) = self.queue()
        r = self.client.post("/outside/done", json={"id": item["id"], "version": item["version"]})
        token = r.json()["token"]
        self.assertTrue(token)
        wrong = self.client.post("/unhide", json={"id": item["id"], "token": "not-the-token"})
        self.assertEqual((wrong.status_code, wrong.json()["restored"]), (200, False))
        self.assertEqual(self.client.get("/queue").json()["count"], 0)
        ok = self.client.post("/unhide", json={"id": item["id"], "token": token})
        self.assertTrue(ok.json()["restored"])
        replay = self.client.post("/unhide", json={"id": item["id"], "token": token})
        self.assertFalse(replay.json()["restored"])

    def test_hide_returns_a_token_and_a_replayed_one_restores_nothing(self):
        self.file(dedupe_key="a")
        (item,) = self.queue()
        first = self.client.post("/hide", json={"id": item["id"], "version": item["version"]}).json()["token"]
        self.assertTrue(self.client.post("/unhide", json={"id": item["id"], "token": first}).json()["restored"])
        second = self.client.post("/hide", json={"id": item["id"], "version": item["version"]}).json()["token"]
        self.assertNotEqual(first, second)
        stale = self.client.post("/unhide", json={"id": item["id"], "token": first})
        self.assertFalse(stale.json()["restored"])
        self.assertEqual(self.client.get("/queue").json()["count"], 0)

    def test_folder_done_returns_a_token(self):
        self.watch()
        (self.folder / "n.txt").write_text("note")
        (item,) = self.queue()
        r = self.client.post("/outside/done", json={"id": item["id"], "version": item["version"]})
        self.assertTrue(r.json()["token"])

    def test_oversized_and_control_character_tokens_are_rejected(self):
        self.file(dedupe_key="a")
        (item,) = self.queue()
        needs.done_outside(item["id"], item["version"])
        for token in ("x" * 301, "bad\x00token", "bad\ntoken", "bad\x85token", "bad token"):
            with self.subTest(token=repr(token)[:20]):
                r = self.client.post("/unhide", json={"id": item["id"], "token": token})
                self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get("/queue").json()["count"], 0)  # still Done


class SummaryFreshness(FiledFixture):
    """03-007, 03-013, 01-037."""

    def test_a_finished_old_request_does_not_make_the_newer_one_look_stale(self):
        """03-007: v1 starts 100, v2 starts 101; v1 finishes 102, v2 finishes 103; v2 must be kept."""
        with patch.object(needs.time, "time", lambda: 102.0):
            needs.save_bluf("k", "v1", "old summary", "w", self.home, started_at=100.0)
        with patch.object(needs.time, "time", lambda: 103.0):
            needs.save_bluf("k", "v2", "new summary", "w", self.home, started_at=101.0)
        self.assertEqual(needs.load_blufs(self.home)["k"]["version"], "v2")

    def test_an_old_request_finishing_after_a_newer_one_is_dropped(self):
        with patch.object(needs.time, "time", lambda: 102.0):
            needs.save_bluf("k", "v2", "new summary", "w", self.home, started_at=101.0)
        with patch.object(needs.time, "time", lambda: 103.0):
            needs.save_bluf("k", "v1", "old summary", "w", self.home, started_at=100.0)
        self.assertEqual(needs.load_blufs(self.home)["k"]["version"], "v2")

    def test_sub_second_ordering_is_kept(self):
        """01-037: a summary saved at 100.6 outranks a request that began at 100.1."""
        with patch.object(needs.time, "time", lambda: 100.6):
            needs.save_bluf("k", "v2", "new", "w", self.home, started_at=100.3)
        with patch.object(needs.time, "time", lambda: 100.9):
            needs.save_bluf("k", "v1", "old", "w", self.home, started_at=100.1)
        self.assertEqual(needs.load_blufs(self.home)["k"]["version"], "v2")

    def test_a_snapshot_observed_at_100_2_does_not_prune_a_summary_saved_at_100_8(self):
        """01-037."""
        with patch.object(needs.time, "time", lambda: 100.8):
            needs.save_bluf("k", "v1", "new", "w", self.home)
        needs.prune_blufs([], self.home, observed_at=100.2)
        self.assertIn("k", needs.load_blufs(self.home))

    def test_a_done_item_keeps_its_summary_and_undo_does_not_ask_the_model_again(self):
        """03-013."""
        result = self.file(dedupe_key="a")
        (item,) = self.queue()
        needs.save_bluf(item["id"], item["version"], "Pick a lane.", "Because.", self.home)
        reply = needs.done_outside(item["id"], item["version"])
        self.assertEqual(self.queue(), [])
        self.assertIn(item["id"], needs.load_blufs(self.home))
        self.assertTrue(needs.unhide(item["id"], token=reply["token"]))
        (back,) = self.queue()
        self.assertEqual(back.get("bluf"), "Pick a lane.")

    def test_an_expired_done_records_summary_is_pruned(self):
        result = self.file(dedupe_key="a")
        (item,) = self.queue()
        needs.save_bluf(item["id"], item["version"], "x", "y", self.home)
        needs.done_outside(item["id"], item["version"])
        self.rewrite(result, done_at=time.time() - needs.OUTSIDE_UNDO_SECONDS - 10)
        self.queue()
        self.assertNotIn(item["id"], needs.load_blufs(self.home))


class DocsMatchRetention(unittest.TestCase):
    """03-001, 03-004."""

    def text(self, name):
        return (ROOT / name).read_text(encoding="utf-8")

    def test_readme_describes_recoverable_done(self):
        readme = self.text("README.md")
        self.assertNotIn("**Done** deletes an agent-filed record", readme)
        self.assertNotIn("deleting an agent-filed record does not", readme)
        self.assertRegex(readme, r"filed[^.\n]*Done[^.\n]*(kept|retain)[^.\n]*15 minutes")
        self.assertNotRegex(readme, r"(?i)filed records? (are|is) deleted on done")

    def test_readme_disclosure_counts_retained_done_records(self):
        readme = self.text("README.md")
        self.assertRegex(readme, r"(?i)at most 25")
        self.assertRegex(readme, r"(?i)225|200 active")

    def test_retention_is_described_as_cleanup_on_the_next_use_not_a_timer(self):
        for name in ("README.md", "docs/operator-guide.md"):
            with self.subTest(name=name):
                text = re.sub(r"\s+", " ", self.text(name))
                self.assertRegex(text, r"(?i)next (poll|filing|use|action|time UNBL0CK runs)")
                self.assertRegex(text, r"(?i)no background timer|not a timer|only when UNBL0CK runs|while UNBL0CK is not running")


class DesktopLateDismissals(unittest.TestCase):
    """03-002, 03-003: a late response only touches its own dismissal."""

    def run_views(self, script):
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                result = subprocess.run(["node", "-e", HARNESS + script, str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_late_dismissal_response_does_not_replace_a_newer_dismissal(self):
        self.run_views(r"""
undo=[];
const mk=v=>({source:'outside',origin:'filed',kind:'draft',id:'outside:filed:1',version:v,title:'T'+v,ask:'a',act_where:'w'});
let releaseFirst;
const first=new Promise(res=>{releaseFirst=()=>res({ok:true,done:true,token:'tok-v1',order:1})});
const ctx1={rest:()=>first};
const ctx2={rest:async()=>({ok:true,done:true,token:'tok-v2',order:2})};
(async()=>{
 cursor=0;state=[];
 const p1=find(Item({ctx:ctx1,item:mk('v1'),n:1,refresh,askEnabled:false}),'Done').onClick();
 cursor=0;state=[];
 await find(Item({ctx:ctx2,item:mk('v2'),n:1,refresh,askEnabled:false}),'Done').onClick();
 assert.equal(undo.length,1);
 assert.equal(undo[0].token,'tok-v2');
 releaseFirst();
 await p1;
 assert.equal(undo.length,1,'the late response does not add a second entry');
 assert.equal(undo[0].token,'tok-v2','the newer dismissal keeps its usable token');
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_a_late_undo_response_keys_on_the_dismissal_token_not_the_millisecond(self):
        for outcome in ("ok", "fail"):
            with self.subTest(outcome=outcome):
                self.run_views(r"""
const outcome=%r;
const at=Date.now();
const old={id:'outside:filed:1',version:'v1',title:'Old',kind:'done',at,token:'A',seq:1};
undo=[old];
let release;
const ctx={rest:()=>new Promise((res,rej)=>{release=()=>outcome==='ok'?res({restored:true}):rej(new Error('net'))})};
(async()=>{
 cursor=0;state=[];
 const pending=all(Page({ctx,refresh})).find(n=>n?.type==='button'&&n.props.children==='Undo').props.onClick();
 undo=[{...old,version:'v2',title:'New',token:'B',seq:2}];   // same item, same millisecond
 release();
 await pending;
 assert.equal(undo.length,1,'the newer dismissal survives');
 assert.equal(undo[0].token,'B');
 assert.ok(!undo[0].failed,'not marked failed by the older request');
})().catch(e=>{console.error(e);process.exitCode=1});
""" % outcome)

    def test_the_hide_and_folder_done_tokens_come_from_the_server(self):
        self.run_views(r"""
undo=[];
const calls=[];
const ctx={rest:async(url,o)=>{calls.push([url,o]);return {ok:true,hidden:true,token:'srv-token'}}};
const it={source:'outside',origin:'folder',kind:'notes',id:'outside:folder:'+'a'.repeat(64),version:'v1',title:'T',ask:'a',act_where:'w'};
(async()=>{
 cursor=0;state=[];
 await find(Item({ctx,item:it,n:1,refresh,askEnabled:false}),'Done — hides until the file changes').onClick();
 assert.equal(undo.length,1);
 assert.equal(undo[0].token,'srv-token');
 // Not for me takes its token from the server too, never the item version.
 undo=[];cursor=0;state=[];
 const kb={source:'kanban',id:'kanban:default:t1',board:'default',task_id:'t1',version:'v1',title:'K',ask:'a',tier:'ask'};
 await find(Item({ctx,item:kb,n:1,refresh,askEnabled:false}),'Not for me').onClick();
 assert.equal(undo.length,1);
 assert.equal(undo[0].kind,'hidden');
 assert.equal(undo[0].token,'srv-token');
})().catch(e=>{console.error(e);process.exitCode=1});
""")


if __name__ == "__main__":
    unittest.main()
