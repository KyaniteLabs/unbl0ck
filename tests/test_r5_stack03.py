"""Round-5 review findings owned by the recoverable-Done branch."""

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from test_outside import OutsideFixture, needs
from test_operator_recovery import HARNESS

ROOT = Path(__file__).resolve().parents[1]

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - the suite requires fastapi
    FastAPI = None


class SummaryOrdering(OutsideFixture):
    """R5-needs-014, R5-api-008, R5-api-018 (class A): the newest-started summary wins."""

    def test_an_older_request_for_a_different_fingerprint_of_the_same_version_is_dropped(self):
        with patch.object(needs.time, "time", lambda: 21.0):
            needs.save_bluf("k", "1", "new text summary", "w", self.home, started_at=20.0, fp="fp-new")
        with patch.object(needs.time, "time", lambda: 22.0):
            needs.save_bluf("k", "1", "old text summary", "w", self.home, started_at=10.0, fp="fp-old")
        entry = needs.load_blufs(self.home)["k"]
        self.assertEqual(entry["fp"], "fp-new")
        self.assertEqual(entry["bluf"], "new text summary")

    def test_a_newer_request_for_a_different_fingerprint_of_the_same_version_replaces_it(self):
        needs.save_bluf("k", "1", "old", "w", self.home, started_at=10.0, fp="fp-old")
        needs.save_bluf("k", "1", "new", "w", self.home, started_at=20.0, fp="fp-new")
        self.assertEqual(needs.load_blufs(self.home)["k"]["fp"], "fp-new")

    def test_an_old_request_is_dropped_by_start_order_even_when_the_clock_stepped_back(self):
        """R5-api-018: the cached summary was stamped 99 (clock stepped back) though its request
        began at 101; the request that began at 100 finishes later and must still lose. A
        completion-time comparison keeps it, so this case tells the ordering rule apart."""
        with patch.object(needs.time, "time", lambda: 99.0):
            needs.save_bluf("k", "v2", "new summary", "w", self.home, started_at=101.0)
        with patch.object(needs.time, "time", lambda: 103.0):
            needs.save_bluf("k", "v1", "old summary", "w", self.home, started_at=100.0)
        self.assertEqual(needs.load_blufs(self.home)["k"]["version"], "v2")


class MalformedDoneTimes(OutsideFixture):
    """R5-needs-021 (class M): a huge JSON integer is a malformed time, never an exception."""

    def test_a_huge_done_at_is_not_a_time(self):
        self.assertIsNone(needs._done_at({"done_at": 10**400}))
        self.assertIsNone(needs._done_at({"done_at": -(10**400)}))
        self.assertTrue(needs._bad_done({"done_at": 10**400}))

    def test_num_treats_a_huge_integer_as_zero(self):
        self.assertEqual(needs._num(10**400), 0.0)
        self.assertEqual(needs._num(-(10**400)), 0.0)

    def test_one_filed_record_with_a_huge_done_at_does_not_stop_the_queue_or_the_cleanup(self):
        good = self.file(dedupe_key="good")
        bad = self.file(dedupe_key="bad")
        path = self.record(bad)
        # json.dumps writes the integer exactly; the loader reads it back as a Python int.
        rec = json.loads(path.read_text())
        rec["done_at"] = 10**400
        path.write_text(json.dumps(rec))
        queue = needs.build_queue()
        self.assertIn(good["id"], [i["id"] for i in queue["items"]])
        self.assertTrue(any("invalid done_at" in e for e in queue.get("errors", [])))
        self.assertTrue(path.exists(), "an unusable record is kept for repair")
        self.assertEqual(needs._purge_done(path.parent), 0)


class MalformedNumbersInOtherRecords(OutsideFixture):
    """R5-needs-021 sweep (class M): the same unbounded-number hole in neighbouring readers."""

    def test_a_wait_record_with_an_infinite_pid_is_stale_not_an_exception(self):
        state = needs.wait_state(Path("w.json"), {"pid": float("inf"), "request_id": "r", "since": 1})
        self.assertIsNotNone(state)

    def test_trimming_the_summary_cache_survives_an_infinite_saved_time(self):
        data = {f"k{n}": {"version": "1", "bluf": "b", "why": "w", "at": float("inf") if n == 0 else n} for n in range(needs._BLUF_KEEP + 3)}
        self.assertEqual(len(needs._trim_blufs(data)), needs._BLUF_KEEP)

    def test_a_legacy_lost_entry_with_a_text_count_does_not_block_a_new_fault(self):
        path = needs._fault_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"+lost": {"count": "many", "pid": 1}}))
        needs._journal_fault(self.home, "0123456789abcdef", "record")
        self.assertEqual(json.loads(path.read_text())["0123456789abcdef"]["what"], "record")
        needs.wait_items(self.home, errors=[])


class CountingScandir:
    """os.scandir that counts how many directory entries the caller actually pulled."""

    def __init__(self, real, counter):
        self._it = real
        self._counter = counter

    def __enter__(self):
        self._it.__enter__()
        return self

    def __exit__(self, *exc):
        return self._it.__exit__(*exc)

    def __iter__(self):
        return self

    def __next__(self):
        entry = next(self._it)
        self._counter[0] += 1
        return entry


class FiledScanBounds(OutsideFixture):
    """R5-needs-026 (class D): the advertised bound counts every directory entry."""

    LIMIT = 5

    def crowd(self, extra=40):
        self.file(dedupe_key="real")
        d = needs.data_dir(self.home) / "outside"
        for n in range(extra):
            (d / f"note-{n:03d}.txt").write_text("not a record")
        return d

    def counted(self, call):
        counter = [0]
        real = os.scandir
        with patch.object(needs, "_PURGE_SCAN_LIMIT", self.LIMIT), patch.object(
            needs.os, "scandir", lambda p: CountingScandir(real(p), counter)
        ):
            result = call()
        return counter[0], result

    def test_filed_files_reads_at_most_the_limit_of_entries_when_none_are_json(self):
        d = self.crowd()
        read, (files, over) = self.counted(lambda: needs._filed_files(d))
        self.assertLessEqual(read, self.LIMIT + 1)
        self.assertTrue(over, "more entries than the limit must be reported")

    def test_purge_stops_after_the_limit_even_when_the_entries_are_not_json(self):
        d = self.crowd()
        errors = []
        read, _ = self.counted(lambda: needs._purge_done(d, errors=errors))
        self.assertLessEqual(read, self.LIMIT + 1)
        self.assertTrue(any("stopped early" in e for e in errors))

    def test_a_folder_inside_the_limit_still_lists_its_records(self):
        self.file(dedupe_key="a")
        d = needs.data_dir(self.home) / "outside"
        files, over = needs._filed_files(d)
        self.assertEqual((len(files), over), (1, False))


@unittest.skipIf(FastAPI is None, "fastapi not installed")
class DismissalOrder(OutsideFixture):
    """R5-desk-006, R5-desk-025 (class A, H): the server says which dismissal is newest."""

    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location(
            "unbl0ck_r5_s03_order_api", ROOT / "dashboard/plugin_api.py"
        )
        self.api = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.api
        spec.loader.exec_module(self.api)
        self.api._needs = lambda: needs
        self.api._loaded_ctx = lambda: None
        app = FastAPI()
        app.include_router(self.api.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_each_dismissal_reply_carries_a_strictly_increasing_order(self):
        orders = []
        for n in range(4):
            reply = self.client.post("/hide", json={"id": f"chat:p:s{n}", "version": "1"}).json()
            orders.append(reply["order"])
        filed = self.file(dedupe_key="a")
        (item,) = needs.build_queue()["items"]
        done = self.client.post("/outside/done", json={"id": item["id"], "version": item["version"]}).json()
        orders.append(done["order"])
        self.assertEqual(orders, sorted(set(orders)))
        self.assertTrue(all(isinstance(o, int) for o in orders))

    def test_a_repeated_done_keeps_the_order_of_the_dismissal_it_returns(self):
        self.file(dedupe_key="a")
        (item,) = needs.build_queue()["items"]
        first = needs.done_outside(item["id"], item["version"])
        again = needs.done_outside(item["id"], item["version"])
        self.assertEqual((first["token"], first["order"]), (again["token"], again["order"]))


class DesktopDismissalOrder(unittest.TestCase):
    """R5-desk-006, R5-desk-021, R5-desk-025."""

    def run_views(self, script):
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                result = subprocess.run(["node", "-e", HARNESS + script, str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_server_commit_order_not_the_click_order_picks_the_dismissal_kept(self):
        """desk-006: hide A is clicked first but commits last on the server; its token is current."""
        self.run_views(r"""
undo=[];
const it={source:'kanban',id:'kanban:default:task',board:'default',task_id:'task',version:'1',title:'Launch',ask:'Which day?',tier:'ask'};
let releaseA;
const ctxA={rest:()=>new Promise(res=>{releaseA=()=>res({ok:true,token:'tok-A',order:6})})};
const ctxB={rest:async()=>({ok:true,token:'tok-B',order:5})};
(async()=>{
 cursor=0;state=[];
 const pA=find(Item({ctx:ctxA,item:it,n:1,refresh,askEnabled:false}),'Not for me').onClick();
 cursor=0;state=[];
 await find(Item({ctx:ctxB,item:it,n:1,refresh,askEnabled:false}),'Not for me').onClick();
 assert.equal(undo.length,1);
 assert.equal(undo[0].token,'tok-B');
 releaseA();
 await pA;
 assert.equal(undo.length,1);
 assert.equal(undo[0].token,'tok-A','the server committed A last, so A holds the usable token');
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_a_response_the_server_committed_earlier_does_not_replace_a_later_one(self):
        self.run_views(r"""
undo=[];
const it={source:'kanban',id:'kanban:default:task',board:'default',task_id:'task',version:'1',title:'Launch',ask:'Which day?',tier:'ask'};
let releaseA;
const ctxA={rest:()=>new Promise(res=>{releaseA=()=>res({ok:true,token:'tok-A',order:5})})};
const ctxB={rest:async()=>({ok:true,token:'tok-B',order:6})};
(async()=>{
 cursor=0;state=[];
 const pA=find(Item({ctx:ctxA,item:it,n:1,refresh,askEnabled:false}),'Not for me').onClick();
 cursor=0;state=[];
 await find(Item({ctx:ctxB,item:it,n:1,refresh,askEnabled:false}),'Not for me').onClick();
 releaseA();
 await pA;
 assert.equal(undo.length,1);
 assert.equal(undo[0].token,'tok-B');
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_the_hide_undo_token_is_the_one_the_server_returned(self):
        """desk-021: hide's entry carries r.token, never the card version."""
        self.run_views(r"""
undo=[];
const it={source:'kanban',id:'kanban:default:task',board:'default',task_id:'task',version:'ver-9',title:'Launch',ask:'Which day?',tier:'ask'};
const ctx={rest:async()=>({ok:true,token:'srv-token',order:3})};
(async()=>{
 cursor=0;state=[];
 await find(Item({ctx,item:it,n:1,refresh,askEnabled:false}),'Not for me').onClick();
 assert.equal(undo.length,1);
 assert.equal(undo[0].kind,'hidden');
 assert.equal(undo[0].token,'srv-token');
 assert.notEqual(undo[0].token,it.version);
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_the_undo_history_keeps_the_five_newest_dismissals_whatever_order_responses_arrive_in(self):
        """desk-025: orders 1-6 for six items; responses 2-6 arrive, then 1: 1 is the one dropped."""
        self.run_views(r"""
undo=[];
const mk=n=>({source:'kanban',id:'kanban:default:t'+n,board:'default',task_id:'t'+n,version:'1',title:'T'+n,ask:'q',tier:'ask'});
const releases={};
const ctxFor=n=>({rest:()=>new Promise(res=>{releases[n]=()=>res({ok:true,token:'tok-'+n,order:n})})});
(async()=>{
 const pending=[];
 for(let n=1;n<=6;n++){cursor=0;state=[];pending.push(find(Item({ctx:ctxFor(n),item:mk(n),n:1,refresh,askEnabled:false}),'Not for me').onClick())}
 for(const n of [2,3,4,5,6,1]){releases[n]();await pending[n-1]}
 assert.deepEqual(undo.map(u=>u.token),['tok-6','tok-5','tok-4','tok-3','tok-2']);
})().catch(e=>{console.error(e);process.exitCode=1});
""")


if __name__ == "__main__":
    unittest.main()
