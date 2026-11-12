"""Every lifecycle UNBL0CK keeps, named and checked: the derived state tables, each legal
transition, each illegal one refused, legacy values still read, and a seeded random walk per
machine that checks the invariants after every step (stdlib only: CI has no Hypothesis)."""

import importlib.util
import json
import os
import random
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


needs = _load("ub_needs_sm", ROOT / "needs.py")
jobs = _load("ub_jobs_sm", ROOT / "jobs.py")

try:
    import fastapi  # noqa: F401
except Exception:  # pragma: no cover - plain CI without the Hermes venv
    fastapi = None


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# --- hidden items ------------------------------------------------------------------------------

class VisibilityMachine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_derived_table_is_exhaustive(self):
        cases = {
            needs.Visibility.SHOWN: ({}, "a", "1"),
            needs.Visibility.HIDDEN: ({"a": "1"}, "a", "1"),
            needs.Visibility.RESURFACED: ({"a": "0"}, "a", "1"),
        }
        self.assertEqual(set(cases), set(needs.Visibility))   # a new state must get a row here
        for want, (hidden, i, v) in cases.items():
            self.assertIs(needs.visibility(hidden, i, v), want)

    def test_legal_transitions(self):
        h = self.home
        state = lambda: needs.visibility(needs.load_hidden(h), "a", "1")
        self.assertIs(state(), needs.Visibility.SHOWN)
        needs.hide("a", "1", h)                                  # SHOWN -> HIDDEN
        self.assertIs(state(), needs.Visibility.HIDDEN)
        self.assertIs(needs.visibility(needs.load_hidden(h), "a", "2"), needs.Visibility.RESURFACED)
        needs.hide("a", "2", h)                                  # RESURFACED -> HIDDEN (new version)
        self.assertIs(needs.visibility(needs.load_hidden(h), "a", "2"), needs.Visibility.HIDDEN)
        self.assertTrue(needs.unhide("a", h))                    # HIDDEN -> SHOWN
        self.assertIs(state(), needs.Visibility.SHOWN)

    def test_unhide_on_shown_is_refused(self):
        self.assertFalse(needs.unhide("never", self.home))
        self.assertFalse((self.home / "plugin-data/unbl0ck/hidden.json").exists())   # nothing written

    def test_hide_refuses_bad_ids(self):
        for bad in ("", "x" * 301):
            with self.assertRaises(ValueError):
                needs.hide(bad, "1", self.home)

    def test_legacy_null_entry_can_be_unhidden(self):
        # Bug: unhide() tested the popped value, so an entry stored as null (an old or
        # hand-edited hidden.json) reported "not hidden" and was never removed from disk.
        p = needs.data_dir(self.home) / "hidden.json"
        p.write_text(json.dumps({"a": None, "b": "1"}))
        self.assertTrue(needs.unhide("a", self.home))
        self.assertEqual(json.loads(p.read_text()), {"b": "1"})

    def test_format_is_unchanged(self):
        needs.hide("kanban:b:t", "7", self.home)
        p = needs.data_dir(self.home) / "hidden.json"
        self.assertEqual(json.loads(p.read_text()), {"kanban:b:t": "7"})

    def test_random_walk_matches_model(self):
        rng = random.Random(7)
        ids, model, versions = ["a", "b", "c"], {}, {"a": "1", "b": "1", "c": "1"}
        for _ in range(300):
            op, i = rng.choice(["hide", "unhide", "bump"]), rng.choice(ids)
            if op == "hide":
                needs.hide(i, versions[i], self.home)
                model[i] = versions[i]
            elif op == "unhide":
                self.assertEqual(needs.unhide(i, self.home), i in model)
                model.pop(i, None)
            else:                                                # the source changed
                versions[i] = str(int(versions[i]) + 1)
            stored = needs.load_hidden(self.home)
            self.assertEqual(stored, model)
            for j in ids:
                want = (needs.Visibility.SHOWN if j not in model else
                        needs.Visibility.HIDDEN if model[j] == versions[j] else needs.Visibility.RESURFACED)
                self.assertIs(needs.visibility(stored, j, versions[j]), want)


# --- Kanban answer rules -----------------------------------------------------------------------

class CardStatusMachine(unittest.TestCase):
    def test_status_sets_are_consistent(self):
        values = {s.value for s in needs.CardStatus}
        self.assertEqual(needs.HELD_STATUSES, values)
        self.assertLessEqual(needs.RELEASABLE_STATUSES, values)
        self.assertLessEqual({s.value for s in needs.QUEUE_STATUSES}, values)

    def test_answer_table_is_exhaustive(self):
        allowed = {  # (status, release) -> None or the exception raised
            ("blocked", False): None, ("blocked", True): None,
            ("scheduled", False): None, ("scheduled", True): None,
            ("triage", False): None, ("triage", True): ValueError,
        }
        self.assertEqual({s for s, _ in allowed}, {s.value for s in needs.CardStatus})
        for (status, release), err in allowed.items():
            if err is None:
                needs.check_answerable("t", status, release=release)
                needs.check_answerable("t", needs.CardStatus(status), release=release)
            else:
                with self.assertRaises(err):
                    needs.check_answerable("t", status, release=release)

    def test_other_statuses_are_refused(self):
        for status in ("todo", "running", "done", "archived", "", None):
            for release in (False, True):
                with self.assertRaises(RuntimeError):
                    needs.check_answerable("t", status, release=release)

    def test_answered_since_folds_case_like_python_on_both_paths(self):
        # Bug: the queue's SQL path used LOWER(), which folds ASCII only, while answer_card
        # used str.lower(): "ZOË" counted as answered for one and not the other.
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE task_comments(id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INT)")
        conn.execute("INSERT INTO task_comments(task_id, author, body, created_at) VALUES('t','ZOË','ok',10)")
        fallback = types.SimpleNamespace(list_comments=lambda c, t: [types.SimpleNamespace(author="ZOË", created_at=10)])
        self.assertTrue(needs._answered_since(conn, fallback, "t", "zoë", 5))
        self.assertTrue(needs._answered_since(object(), fallback, "t", "zoë", 5))   # fallback path agrees
        self.assertFalse(needs._answered_since(conn, fallback, "t", "zoë", 11))


class AnswerCardStatuses(unittest.TestCase):
    """answer_card goes through check_answerable for every status, before any write."""

    def _run(self, status, release, unblock=True, after=None):
        calls = []
        cur = types.SimpleNamespace(id="t", status=status, created_at=1)
        kdb = types.ModuleType("hermes_cli.kanban_db")
        reads = iter([cur, cur, after or cur])
        kdb.get_task = lambda c, t: next(reads)
        kdb.list_events = lambda c, t: []
        kdb.list_comments = lambda c, t: []
        kdb.add_comment = lambda c, t, author, body: calls.append("comment") or 3
        kdb.unblock_task = lambda c, t: calls.append("unblock") or unblock

        class Txn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        kdb.write_txn = lambda conn, allow_nested=False: Txn()
        kbc = types.ModuleType("hermes_cli.kanban_db_connect")
        kbc.connect = lambda board=None: sqlite3.connect(":memory:")
        pkg = types.ModuleType("hermes_cli")
        pkg.kanban_db, pkg.kanban_db_connect = kdb, kbc
        def fake_release(conn, db, task_id, version):
            calls.append("unblock")
            return unblock
        with mock.patch.dict(sys.modules, {"hermes_cli": pkg, "hermes_cli.kanban_db": kdb,
                                           "hermes_cli.kanban_db_connect": kbc}):
            with mock.patch.object(needs, "_unblock_task_at_version", fake_release):
                with tempfile.TemporaryDirectory() as home:   # the pending-release marker lands here
                    try:
                        return needs.answer_card("b", "t", "go", release=release,
                                                 version="1" if release else None, home=Path(home)), calls
                    except Exception as exc:  # noqa: BLE001
                        return exc, calls

    def test_every_status(self):
        for status in [s.value for s in needs.CardStatus] + ["todo", "running", "done"]:
            for release in (False, True):
                out, calls = self._run(status, release)
                try:
                    needs.check_answerable("t", status, release=release)
                    legal = True
                except (RuntimeError, ValueError):
                    legal = False
                if legal:
                    self.assertEqual(out["comment_id"], 3, (status, release))
                    self.assertEqual(out["released"], release)
                else:
                    self.assertIsInstance(out, (RuntimeError, ValueError), (status, release))
                    self.assertEqual(calls, [], (status, release))

    def test_failed_unblock_still_held_raises_for_every_held_status(self):
        for status in needs.HELD_STATUSES:
            after = types.SimpleNamespace(id="t", status=status, created_at=1)
            out, _ = self._run("blocked", True, unblock=False, after=after)
            self.assertIsInstance(out, needs.UnblockFailed, status)

    def test_failed_unblock_released_by_someone_else_is_not_an_error(self):
        after = types.SimpleNamespace(id="t", status="todo", created_at=1)
        out, _ = self._run("blocked", True, unblock=False, after=after)
        self.assertEqual(out["released"], True)


# --- live waits --------------------------------------------------------------------------------

RID = "c" * 32


class WaitMachine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.f = Path(self.tmp.name) / f"{RID}.json"

    def tearDown(self):
        self.tmp.cleanup()

    def rec(self, **kw):
        base = {"request_id": RID, "pid": os.getpid(), "proc_start": needs._proc_start(os.getpid()),
                "since": 1000, "kind": "clarify"}
        base.update(kw)
        return base

    def test_state_table(self):
        now = 1000 + 60
        self.assertIs(needs.wait_state(self.f, self.rec(), now), needs.WaitState.OPEN)
        stale = [
            {},                                                   # unreadable
            self.rec(pid=0), self.rec(pid="x"),                   # malformed pid
            self.rec(request_id="NOT-HEX"),                       # unsafe id
            self.rec(request_id="d" * 32),                        # file name does not match
            self.rec(since=now - needs.WAIT_MAX_AGE - 1),         # missed resolve hook
            self.rec(proc_start="not-the-same-process"),          # recycled pid
        ]
        for r in stale:
            self.assertIs(needs.wait_state(self.f, r, now), needs.WaitState.STALE, r)
        self.assertEqual(set(needs.WaitState), {needs.WaitState.OPEN, needs.WaitState.STALE})

    def test_legacy_record_without_proc_start_is_open(self):
        r = self.rec()
        del r["proc_start"]
        self.assertIs(needs.wait_state(self.f, r, 1060), needs.WaitState.OPEN)

    def test_proc_start_without_procfs_runs_only_ps_without_a_shell(self):
        # The README discloses this one subprocess; keep it to a fixed ps argv, no shell.
        with mock.patch("pathlib.Path.read_text", side_effect=OSError), \
                mock.patch("pathlib.Path.exists", return_value=False), \
                mock.patch.object(needs.os, "name", "posix"), \
                mock.patch("subprocess.run") as run:
            run.return_value.stdout = "Wed Nov 11 10:00:00 2026\n"
            self.assertEqual(needs._proc_start(12345), "Wed Nov 11 10:00:00 2026")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["ps", "-o", "lstart=", "-p", "12345"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["env"], {"PATH": "/bin:/usr/bin", "LC_ALL": "C"})

    def test_transitions_through_the_hooks(self):
        needs.record_wait(self.home, request_id=RID, kind="clarify", prompt="Which?")   # -> OPEN
        self.assertEqual([i["id"] for i in needs.wait_items(self.home)], [f"prompt:{RID}"])
        needs.clear_wait(self.home, request_id=RID)                                    # OPEN -> gone
        self.assertEqual(needs.wait_items(self.home), [])
        needs.clear_wait(self.home, request_id=RID)                                    # gone: no-op

    def test_stale_is_deleted_on_the_next_poll(self):
        needs.record_wait(self.home, request_id=RID, kind="clarify")
        f = needs._waits_dir(self.home) / f"{RID}.json"
        data = json.loads(f.read_text())
        data["since"] = 0
        f.write_text(json.dumps(data))
        self.assertEqual(needs.wait_items(self.home), [])
        self.assertFalse(f.exists())


# --- item sources ------------------------------------------------------------------------------

class Sources(unittest.TestCase):
    def test_order_covers_every_source(self):
        self.assertEqual(set(needs.SOURCE_ORDER), set(needs.SOURCES))
        self.assertLessEqual(set(needs.NO_BLUF_SOURCES), set(needs.SOURCES))

    def test_pocket_pack_handles_every_source(self):
        items = [{"id": f"{s}:1", "source": s, "title": s, "ask": "a", "why": "w", "task_id": "t",
                  "profile": "p", "platform": "telegram"} for s in needs.SOURCES]
        cards = needs.pocket_pack({"items": items}, limit=len(items))["cards"]
        self.assertEqual([c["id"] for c in cards], [i["id"] for i in items])
        labels = {c["id"].split(":")[0]: c["choices"][0] for c in cards}
        self.assertEqual(labels["kanban"], "Answer + unblock")
        self.assertEqual(labels["pairing"], "Review request")


# --- summary (BLUF) jobs -----------------------------------------------------------------------

K = ("kanban:b:t", "1")


class BlufJobsMachine(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.j = jobs.BlufJobs(clock=self.clock)

    def test_states_are_named(self):
        self.assertEqual({s.value for s in jobs.BlufState}, {"pending", "failed"})

    def test_legal_transitions(self):
        self.assertIsNone(self.j.state(K))
        self.assertTrue(self.j.start(K))                     # absent -> PENDING
        self.assertIs(self.j.state(K), jobs.BlufState.PENDING)
        self.j.fail(K, "boom")                               # PENDING -> FAILED
        self.assertIs(self.j.state(K), jobs.BlufState.FAILED)
        self.assertFalse(self.j.start(K))                    # FAILED, cooling down: refused
        self.clock.t += jobs.BlufJobs.RETRY_S
        self.assertTrue(self.j.start(K))                     # FAILED -> PENDING
        self.j.succeed(K)                                    # PENDING -> absent
        self.assertIsNone(self.j.state(K))

    def test_pending_is_not_restarted_while_the_pool_works(self):
        # Bug: the old table retried any entry older than 600 s, pending or failed, so a
        # backlog on the two-worker pool submitted the same summary again and again.
        self.assertTrue(self.j.start(K))
        self.clock.t += jobs.BlufJobs.RETRY_S + 1
        self.assertFalse(self.j.start(K))
        self.clock.t += jobs.BlufJobs.STUCK_S
        self.assertTrue(self.j.start(K))                     # really stuck: one retry

    def test_illegal_transitions_are_refused(self):
        self.j.start(K)
        self.j.fail(K, "x")
        with self.assertRaises(jobs.IllegalTransition):
            self.j.succeed(K)                                # FAILED -> absent
        with self.assertRaises(jobs.IllegalTransition):
            self.j.fail(K, "y")                              # FAILED -> FAILED

    def test_prune_never_drops_a_pending_job(self):
        self.j.start(K)
        for i in range(jobs.BlufJobs.PRUNE_OVER + 5):
            k = (f"x{i}", "1")
            self.j.start(k)
            self.j.fail(k, "e")
        self.clock.t += jobs.BlufJobs.RETRY_S + 1
        self.assertTrue(self.j.start(("new", "1")))
        self.assertIs(self.j.state(K), jobs.BlufState.PENDING)
        self.assertLess(len(self.j), 10)

    def test_random_walk_invariants(self):
        rng = random.Random(11)
        keys = [(f"k{i}", "1") for i in range(4)]
        model = {}   # key -> (state, at, attempts)
        for _ in range(500):
            k, op = rng.choice(keys), rng.choice(["start", "succeed", "fail", "tick"])
            st = model.get(k)
            if op == "tick":
                self.clock.t += rng.choice([1, 300, 700, 4000])
                continue
            if op == "start":
                age = None if st is None else self.clock.t - st[1]
                n = 0 if st is None else st[2]
                want = (st is None
                        or (st[0] == "failed" and n < jobs.BlufJobs.MAX_ATTEMPTS
                            and age >= jobs.BlufJobs.RETRY_S * 2 ** (n - 1))
                        or (st[0] == "pending" and age >= jobs.BlufJobs.STUCK_S))
                self.assertEqual(self.j.start(k), want)
                if want:
                    model[k] = ("pending", self.clock.t, n if st is not None and st[0] == "failed" else 0)
            elif st is None or st[0] == "pending":
                getattr(self.j, op)(*((k,) if op == "succeed" else (k, "e")))
                if op == "succeed":
                    model.pop(k, None)
                else:
                    model[k] = ("failed", self.clock.t, (st[2] if st is not None else 0) + 1)
            else:
                with self.assertRaises(jobs.IllegalTransition):
                    getattr(self.j, op)(*((k,) if op == "succeed" else (k, "e")))
            for kk in keys:
                want = model.get(kk)
                self.assertEqual(self.j.state(kk), None if want is None else jobs.BlufState(want[0]))


# --- Ask jobs ----------------------------------------------------------------------------------

class AskJobsMachine(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.j = jobs.AskJobs(clock=self.clock)

    def test_states_are_named(self):
        self.assertEqual({s.value for s in jobs.AskState}, {"pending", "done", "error"})
        self.assertEqual(jobs.ASK_SETTLED, set(jobs.AskState) - {jobs.AskState.PENDING})

    def test_done_path(self):
        self.j.create("a")
        self.assertIs(self.j.get("a")["status"], jobs.AskState.PENDING)
        self.assertTrue(self.j.finish("a", "Pick A", "p", "m"))
        st = self.j.consume("a")
        self.assertEqual((st["status"], st["reply"], st["provider"], st["model"]), (jobs.AskState.DONE, "Pick A", "p", "m"))
        self.assertIsNone(self.j.get("a"))

    def test_error_path(self):
        self.j.create("a")
        self.assertTrue(self.j.fail("a", "model call failed"))
        st = self.j.consume("a")
        self.assertEqual((st["status"], st["ok"], st["detail"]), (jobs.AskState.ERROR, False, "model call failed"))

    def test_illegal_transitions_are_refused(self):
        self.j.create("a")
        with self.assertRaises(jobs.IllegalTransition):
            self.j.consume("a")                       # still pending
        with self.assertRaises(jobs.IllegalTransition):
            self.j.create("a")                        # duplicate
        self.j.finish("a", "x")
        for move in (lambda: self.j.finish("a", "y"), lambda: self.j.fail("a", "z")):
            with self.assertRaises(jobs.IllegalTransition):
                move()                                # already settled
        with self.assertRaises(KeyError):
            self.j.consume("missing")

    def test_expired_job_and_late_worker(self):
        self.j.create("old")
        self.clock.t += jobs.AskJobs.TTL_S + 1
        self.j.create("new")                          # expires "old"
        self.assertIsNone(self.j.get("old"))
        self.assertFalse(self.j.finish("old", "late"))   # never resurrected
        self.assertIsNone(self.j.get("old"))

    def test_settled_record_is_complete_when_seen(self):
        self.j.create("a")
        self.j.finish("a", "r")
        st = self.j.get("a")
        self.assertTrue({"status", "ok", "reply", "provider", "model", "started"} <= set(st))

    def test_random_walk_invariants(self):
        rng = random.Random(5)
        model = {}
        for n in range(500):
            op = rng.choice(["create", "finish", "fail", "consume", "tick"])
            job = rng.choice(["a", "b", "c"])
            if op == "tick":
                self.clock.t += rng.choice([10, 500, 1000])
                continue
            if op == "create":
                for k in [k for k, (_, t) in model.items() if t < self.clock.t - jobs.AskJobs.TTL_S]:
                    model.pop(k)
                if job in model:
                    with self.assertRaises(jobs.IllegalTransition):
                        self.j.create(job)
                else:
                    self.j.create(job)
                    model[job] = ("pending", self.clock.t)
            elif op in ("finish", "fail"):
                fn = (lambda: self.j.finish(job, "r")) if op == "finish" else (lambda: self.j.fail(job, "d"))
                if job not in model:
                    self.assertFalse(fn())
                elif model[job][0] != "pending":
                    with self.assertRaises(jobs.IllegalTransition):
                        fn()
                else:
                    self.assertTrue(fn())
                    model[job] = ("done" if op == "finish" else "error", model[job][1])
            else:
                if job not in model:
                    with self.assertRaises(KeyError):
                        self.j.consume(job)
                elif model[job][0] == "pending":
                    with self.assertRaises(jobs.IllegalTransition):
                        self.j.consume(job)
                else:
                    self.assertEqual(self.j.consume(job)["status"].value, model.pop(job)[0])
            for k in ("a", "b", "c"):
                st = self.j.get(k)
                self.assertEqual(None if st is None else st["status"].value, model.get(k, (None,))[0])


@unittest.skipIf(fastapi is None, "fastapi not installed")
class BlufRouteRegression(unittest.TestCase):
    """The dashboard route submits one summary per item version while it is pending."""

    def test_backlogged_summary_is_not_resubmitted(self):
        api = _load("ub_api_sm", ROOT / "dashboard/plugin_api.py")
        submitted = []
        api._BLUF_POOL = types.SimpleNamespace(submit=lambda fn, *a: submitted.append(a[-1]["id"]))
        item = {"id": "kanban:b:t", "version": "1", "source": "kanban"}
        with mock.patch("time.time", return_value=1_000_000.0):
            api._queue_bluf(None, None, dict(item))
        with mock.patch("time.time", return_value=1_000_000.0 + 700):   # still queued behind others
            again = dict(item)
            api._queue_bluf(None, None, again)
        self.assertEqual(submitted, ["kanban:b:t"])
        self.assertTrue(again.get("bluf_pending"))


if __name__ == "__main__":
    unittest.main()
