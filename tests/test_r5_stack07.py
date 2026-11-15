"""Round-5 regression tests for the Ask restart journal (issue #35).

Each test reproduces a round-5 review finding with real files and real threads, and was seen to
fail against the code before its fix. Siblings are loaded by file path so the tests also run
under a shadowing ``tests`` package."""

import contextvars
import importlib.util
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent


def _sibling(name):
    spec = importlib.util.spec_from_file_location(f"ub_r5s07_{name}", _HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rec = _sibling("test_ask_job_recovery")
jobs = rec.jobs
J1, J2, J3, CARD, ROOT = rec.J1, rec.J2, rec.J3, rec.CARD, rec.ROOT


def hexid(i: int) -> str:
    return f"{i:032x}"


class Profiles(rec.RecoveryBase):
    """Two profile directories chosen by a context variable, like the real request context."""

    def setUp(self):
        super().setUp()
        self.who = contextvars.ContextVar(f"who_{id(self)}", default="home")
        self.persist = contextvars.ContextVar(f"persist_{id(self)}", default=True)
        self.dirs = {"home": Path(self.tmp.name) / "p" / "home", "work": Path(self.tmp.name) / "p" / "work"}
        self.unresolved = False

    def resolve(self):
        if self.unresolved:
            raise PermissionError("profile cannot be resolved")
        return self.dirs[self.who.get()]

    def table(self):
        journal = jobs.AskJournal(self.resolve, lambda: self.persist.get(), self.clock)
        return jobs.AskJobs(self.clock, journal)


class ScopeFailsClosed(Profiles):
    """R5-jobs-001 (class F)."""

    def test_an_unresolvable_profile_is_served_nothing_from_memory(self):
        table = self.table()
        self.who.set("work")
        table.create(J1, card="c1")
        table.finish(J1, "WORK PRIVATE ANSWER", "p", "m")
        for broken in ("raise", "none"):
            with self.subTest(resolver=broken):
                self.unresolved = broken == "raise"
                if broken == "none":
                    table._journal._directory = lambda: None
                st = table.get(J1)
                self.assertNotIn("WORK PRIVATE ANSWER", json.dumps(st, default=str))
                self.assertIsNone(table.card(J1) if broken == "none" else None)

    def test_a_job_cannot_be_created_while_its_profile_is_unresolvable(self):
        table = self.table()
        self.unresolved = True
        with self.assertRaises(jobs.JournalUnavailable):
            table.create(J1, card="c1")
        self.unresolved = False
        self.assertEqual(len(table), 0)


class PersistenceFollowsTheJobsProfile(Profiles):
    """R5-jobs-002 (class F)."""

    def test_a_worker_context_does_not_override_the_profiles_persistence_setting(self):
        table = self.table()
        self.who.set("work")
        self.persist.set(False)  # the work profile does not keep replies
        ticket = table.create(J1, card="c1")

        def worker():  # a pool thread: a fresh context, where the default profile keeps replies
            table.mark_sent(ticket)
            table.finish(ticket, "WORK ANSWER", "p", "m")

        t = threading.Thread(target=worker)
        t.start()
        t.join(10)
        text = (self.dirs["work"] / f"{J1}.json").read_text()
        self.assertNotIn("WORK ANSWER", text)
        self.assertEqual(json.loads(text)["state"], "done")


class StaleWorker(Profiles):
    """R5-jobs-003 (class A)."""

    def test_a_deleted_jobs_worker_cannot_complete_a_replacement_with_the_same_id(self):
        table = self.table()
        old = table.create(J1, card="old-card") or J1  # older tables returned no ticket
        table.discard(J1)
        table.create(J1, card="new-card")
        self.assertFalse(table.finish(old, "OLD PRIVATE ANSWER", "p", "m"))
        st = table.get(J1)
        self.assertEqual(jobs.ASK_WIRE[st["status"]], "pending")
        self.assertNotIn("OLD PRIVATE ANSWER", json.dumps(st, default=str))
        self.assertEqual(st["card"], "new-card")


class RecoveredDirectoriesStayTracked(Profiles):
    """R5-jobs-008 (class F)."""

    def test_cleanup_in_the_default_profile_still_visits_a_recovered_work_profile(self):
        first = self.table()
        self.who.set("work")
        first.create(J1, card="c1")
        first.finish(J1, "WORK ANSWER", "p", "m")
        restarted = self.table()
        self.assertEqual(restarted.get(J1)["reply"], "WORK ANSWER")  # recovered during a work request
        self.who.set("home")  # the timer thread runs in the default profile
        self.clock.t += jobs.AskJobs.TTL_S + 5
        restarted.housekeeping()
        self.assertEqual(list(self.dirs["work"].glob("*.json")), [], "the work reply expires although nobody asks")

    def test_leftover_temp_files_are_still_cleaned_after_the_last_job_leaves_memory(self):
        table = self.table()
        self.who.set("work")
        ticket = table.create(J1, card="c1")
        table.finish(ticket, "A", "p", "m")
        table.consume(J1)
        stray = self.dirs["work"] / ".stray.tmp"
        stray.write_text("x")
        os.utime(stray, (1, 1))
        self.who.set("home")
        self.clock.t += jobs.AskJobs.TTL_S + 5
        table.housekeeping()
        self.assertFalse(stray.exists())
        self.assertTrue(table._more() is False)


class IdsPerProfile(Profiles):
    """R5-jobs-012 (class F)."""

    def test_the_same_job_id_can_be_created_in_two_profiles(self):
        table = self.table()
        self.who.set("home")
        table.create(J1, card="home-card")
        table.finish(J1, "HOME", "p", "m")
        self.who.set("work")
        self.assertIsNone(table.get(J1))
        ticket = table.create(J1, card="work-card")  # must not raise IllegalTransition
        table.finish(ticket, "WORK", "p", "m")
        self.assertEqual(table.get(J1)["reply"], "WORK")
        self.who.set("home")
        self.assertEqual(table.get(J1)["reply"], "HOME")
        self.assertEqual(table.get(J1)["card"], "home-card")


class ClaimIsExclusive(Profiles):
    """R5-jobs-004 (class B)."""

    def test_two_workers_that_both_found_nothing_cannot_both_send_the_request(self):
        a, b = self.table(), self.table()
        gate = threading.Barrier(2, timeout=10)
        sent, created = [], []

        def worker(table):
            self.assertIsNone(table.get(J1))
            gate.wait()  # both looked before either created
            try:
                ticket = table.create(J1, card="c1")
            except jobs.IllegalTransition:
                return
            created.append(1)
            sent.append(table.mark_sent(ticket))

        threads = [threading.Thread(target=worker, args=(t,)) for t in (a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        self.assertEqual(len(created), 1, "only one worker may claim the request")
        self.assertEqual(sent, [True])

    def test_a_record_another_worker_owns_is_not_overwritten_by_a_send_marker(self):
        a, b = self.table(), self.table()
        ta = a.create(J1, card="c1")
        # b holds a ticket for the same id from before a claimed it (its entry was never claimed on disk)
        b._jobs[(self.dirs["home"], J1)] = {"status": jobs.AskState.PENDING, "started": self.clock(),
                                            "card": "c1", "dir": self.dirs["home"], "gen": 99, "keep": True}
        self.assertFalse(b.mark_sent(J1))
        self.assertTrue(a.mark_sent(ta))


class PublishVerifiesWhatItInstalled(Profiles):
    """R5-jobs-007 (class A)."""

    def test_a_substituted_temporary_file_does_not_publish_as_the_sent_marker(self):
        table = self.table()
        ticket = table.create(J1, card="c1")
        real = os.rename

        def swapping(src, dst, *a, **kw):
            if str(dst) == f"{J1}.json" and "src_dir_fd" in kw:
                fake = f".{J1}.fake.tmp"
                forged = {"v": 1, "job": J1, "state": "queued", "started": self.clock(), "card": "c1"}
                fd = os.open(fake, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=kw["src_dir_fd"])
                os.write(fd, json.dumps(forged).encode())
                os.close(fd)
                real(fake, src, src_dir_fd=kw["src_dir_fd"], dst_dir_fd=kw["src_dir_fd"])
            return real(src, dst, *a, **kw)

        with patch("os.rename", swapping):
            table.mark_sent(ticket)
        restarted = self.table()
        st = restarted.get(J1)
        self.assertTrue(st is None or jobs.ASK_WIRE[st["status"]] != "not_submitted",
                        "a forged queued record must not report that resubmitting is safe")


class ParentSwapAndLinkedRecords(rec.RecoveryBase):
    """R5-jobs-005 and R5-jobs-011 (class C)."""

    def test_a_parent_swapped_for_a_link_before_the_open_is_not_followed(self):
        base = Path(self.tmp.name) / "swap"
        parent, child, foreign = base / "par", base / "par" / "ask", base / "foreign"
        (foreign / "ask").mkdir(parents=True, mode=0o700)
        child.mkdir(parents=True, mode=0o700)
        journal = jobs.AskJournal(lambda: child, lambda: True, self.clock)
        real_open, swapped = os.open, []

        def opening(path, flags, *a, **kw):
            if not swapped and kw.get("dir_fd") is None and Path(str(path)) in (parent, child):
                swapped.append(1)
                os.rename(parent, base / "par.moved")
                os.symlink(foreign, parent)
            return real_open(path, flags, *a, **kw)

        with patch("os.open", opening):
            wrote = journal.write(J1, {"state": "queued", "started": self.clock(), "card": "c"})
        self.assertTrue(swapped)
        self.assertFalse(wrote)
        self.assertEqual(list((foreign / "ask").iterdir()), [], "nothing may be written through the swapped parent")

    def test_a_linked_record_is_refused_even_without_O_NOFOLLOW(self):
        self.dir.mkdir(parents=True, mode=0o700)
        target = Path(self.tmp.name) / "elsewhere.json"
        target.write_text(json.dumps({"v": 1, "job": J1, "state": "done", "started": self.clock(),
                                      "card": "c", "reply": "FOREIGN REPLY"}))
        os.chmod(target, 0o600)
        os.symlink(target, self.dir / f"{J1}.json")
        with patch.object(os, "O_NOFOLLOW", 0):
            got = self.journal.read(J1)
        self.assertTrue(got.get("corrupt"), got)
        self.assertNotIn("FOREIGN REPLY", json.dumps(got))


class HugeIntegersAreImplausibleNotFatal(rec.RecoveryBase):
    """R5-jobs-013 (class G)."""

    def put(self, **over):
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        body = {"v": 1, "job": J1, "state": "queued", "started": self.clock(), "card": "c"}
        body.update(over)
        path = self.dir / f"{J1}.json"
        path.write_text(json.dumps(body))
        os.chmod(path, 0o600)

    def test_a_huge_start_time_reads_as_unknown(self):
        self.put(started=10**1000)
        st = self.jobs.get(J1)
        self.assertEqual(jobs.ASK_WIRE[st["status"]], "unknown")

    def test_a_huge_owner_pid_is_not_a_live_owner(self):
        self.put(owner={"pid": 10**1000, "inst": "other"})
        st = self.jobs.get(J1)
        self.assertEqual(jobs.ASK_WIRE[st["status"]], "not_submitted")

    def test_a_huge_settled_time_does_not_abort_the_settled_limit(self):
        self.put(state="done", settled_at=10**1000, reply="r")
        self.assertEqual(self.journal.enforce_settled(0, keep=lambda j: False), 1)


try:
    import fastapi
    from fastapi.testclient import TestClient
except Exception:  # noqa: BLE001
    fastapi = None
    TestClient = None
import tempfile
import time
import types


@unittest.skipIf(fastapi is None, "fastapi not installed")
class ApiBase(unittest.TestCase):
    keep = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "ask"
        spec = importlib.util.spec_from_file_location("ub_api_r5s07", ROOT / "dashboard/plugin_api.py")
        self.api = importlib.util.module_from_spec(spec)
        sys.modules["ub_api_r5s07"] = self.api
        spec.loader.exec_module(self.api)
        self.addCleanup(self.api._ASK_JOBS.stop_background)
        self.dir_fn = lambda: self.dir
        self.card = ("factory", "t_1")
        self.llm = lambda m, **kw: types.SimpleNamespace(text="Pick A", provider="p", model="m")
        needs = types.SimpleNamespace(
            ask_enabled=lambda: True,
            card_context=lambda b, t: {"id": t},
            build_ask_messages=lambda c, h: list(h),
            ask_journal_dir=lambda: self.dir_fn(),
            ask_persist_replies=lambda: self.keep,
        )
        self.api._needs = lambda: needs
        self.api._checked_card_ref = lambda i, b, t: self.card
        self.api._loaded_ctx = lambda: types.SimpleNamespace(llm=types.SimpleNamespace(complete=lambda m, **kw: self.llm(m, **kw)))
        self.api._ASK_JOBS = self.api.jobs.AskJobs(journal=self.api.jobs.AskJournal(lambda: self.api._ask_journal_dir(), lambda: self.keep))
        app = fastapi.FastAPI()
        app.include_router(self.api.router)
        self.c = TestClient(app)

    def post(self, request_id=None):
        body = {"id": "x", "messages": [{"role": "user", "content": "q"}]}
        if request_id:
            body["request_id"] = request_id
        return self.c.post("/card/ask", json=body)

    def settle(self, job):
        for _ in range(100):
            p = self.c.get(f"/card/ask/{job}?poll=true&card={CARD}").json()
            if not p.get("pending"):
                return p
            time.sleep(0.05)
        self.fail("ask did not settle")


class ApiScope(ApiBase):
    def test_a_request_whose_profile_cannot_be_resolved_is_refused_not_served(self):
        def broken():
            raise PermissionError("no state")

        self.dir_fn = broken
        r = self.post()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(len(self.api._ASK_JOBS), 0)

    def test_a_concurrent_twin_for_another_card_is_refused_not_given_the_other_cards_job(self):
        """R5-api-007 (class F)."""
        gate = threading.Barrier(2, timeout=10)
        card_var = contextvars.ContextVar("card")
        seen_var = contextvars.ContextVar("seen")  # the request's own flag, shared with its worker thread
        real_get = self.api._ASK_JOBS.get

        def synced_get(job):
            out = real_get(job)
            seen = seen_var.get(None)
            if seen is not None and not seen:
                seen.append(1)
                gate.wait()  # both lookups happen before either creates
            return out

        self.api._ASK_JOBS.get = synced_get
        self.api._checked_card_ref = lambda i, b, t: card_var.get()
        rid = "e" * 32
        results = {}

        def go(name, card):
            card_var.set(card)
            seen_var.set([])
            body = {"id": "x", "request_id": rid, "messages": [{"role": "user", "content": "q"}]}
            results[name] = self.c.post("/card/ask", json=body).status_code

        threads = [threading.Thread(target=go, args=(n, c)) for n, c in (("a", ("factory", "t_a")), ("b", ("factory", "t_b")))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(sorted(results.values()), [200, 409], results)

    def test_a_worker_for_a_deleted_job_cannot_answer_its_replacement(self):
        release = threading.Event()
        started = threading.Event()
        answers = ["OLD PRIVATE ANSWER", "NEW ANSWER"]

        def slow(m, **kw):
            started.set()
            release.wait(10)
            return types.SimpleNamespace(text=answers.pop(0), provider="p", model="m")

        self.llm = slow
        rid = "d" * 32
        self.assertEqual(self.post(rid).status_code, 200)
        self.assertTrue(started.wait(10))
        self.assertTrue(self.c.delete(f"/card/ask/{rid}?card={CARD}").json()["deleted"])
        self.card = ("factory", "t_2")  # the replacement is a different card's request with the same id
        self.assertEqual(self.post(rid).status_code, 200)
        release.set()
        time.sleep(0.5)
        p = self.c.get(f"/card/ask/{rid}?poll=true&card=kanban:factory:t_2").json()
        self.assertNotIn("OLD PRIVATE ANSWER", json.dumps(p))


if __name__ == "__main__":
    unittest.main()
