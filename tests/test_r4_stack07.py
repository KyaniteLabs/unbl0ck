"""Round-4 regression tests for the Ask restart journal (issue #35, PR #58).

Every test here was written from a review reproducer and seen to fail before its fix."""

import contextvars
import enum
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import test_ask_job_recovery as rec

jobs = rec.jobs
J1, J2, J3, CARD, ROOT = rec.J1, rec.J2, rec.J3, rec.CARD, rec.ROOT


def hexid(i: int) -> str:
    return f"{i:032x}"


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class Base(rec.RecoveryBase):
    def other(self, **kw):
        """A second worker over the same directory (its own memory, its own identity)."""
        journal = jobs.AskJournal(lambda: self.dir, lambda: self.keep, self.clock)
        for k, v in kw.items():
            setattr(journal, k, v)
        return jobs.AskJobs(self.clock, journal)


class Containment(Base):
    """07-005 (class C), 07-006 (permissions)."""

    def test_a_symlinked_journal_directory_is_never_written_or_pruned_through(self):
        target = Path(self.tmp.name) / "elsewhere"
        target.mkdir()
        old = target / f"{J3}.json"
        old.write_text("{}")
        os.utime(old, (1, 1))
        self.dir.symlink_to(target, target_is_directory=True)
        self.jobs.create(J1, card="c1")
        self.jobs.housekeeping()
        self.assertEqual(sorted(p.name for p in target.iterdir()), [f"{J3}.json"], "nothing added, nothing deleted")
        self.assertGreater(self.journal.failures, 0)

    def test_a_symlinked_ancestor_is_refused(self):
        real = Path(self.tmp.name) / "real"
        real.mkdir()
        link = Path(self.tmp.name) / "link"
        link.symlink_to(real, target_is_directory=True)
        journal = jobs.AskJournal(lambda: link / "ask", lambda: False, self.clock)
        table = jobs.AskJobs(self.clock, journal)
        table.create(J1, card="c1")
        self.assertEqual(list(real.rglob("*")), [])

    def test_one_profile_cannot_recover_a_job_through_a_symlinked_profile_directory(self):
        work = Path(self.tmp.name) / "ask" / "work"
        personal = Path(self.tmp.name) / "ask" / "personal"
        a = jobs.AskJobs(self.clock, jobs.AskJournal(lambda: work, lambda: True, self.clock))
        a.create(J1, card="c1")
        a.finish(J1, "WORK ONLY", "p", "m")
        personal.symlink_to(work, target_is_directory=True)
        b = jobs.AskJobs(self.clock, jobs.AskJournal(lambda: personal, lambda: True, self.clock))
        st = b.get(J1)
        self.assertNotIn("reply", st or {})

    def test_an_open_directory_is_tightened_to_owner_only(self):
        self.dir.mkdir()
        os.chmod(self.dir, 0o777)
        self.jobs.create(J1, card="c1")
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)

    def test_an_existing_loose_record_is_replaced_by_an_owner_only_one(self):
        self.jobs.create(J1, card="c1")
        os.chmod(self.dir / f"{J1}.json", 0o644)
        self.jobs.mark_sent(J1)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / f"{J1}.json").st_mode), 0o600)

    def test_reading_a_world_readable_record_tightens_it_without_rewriting(self):
        """R5-jobs-027: the replacement test above also passed on the base; this one needs the read to chmod."""
        self.keep = True
        self.jobs.create(J1, card="c1")
        self.jobs.finish(J1, "PRIVATE", "p", "m")
        os.chmod(self.dir / f"{J1}.json", 0o644)
        self.boot()
        self.assertEqual(self.jobs.get(J1)["reply"], "PRIVATE")
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / f"{J1}.json").st_mode), 0o600)

    def test_a_group_writable_record_is_not_trusted(self):
        self.jobs.create(J1, card="c1")
        os.chmod(self.dir / f"{J1}.json", 0o666)
        self.boot()
        self.assertEqual(jobs.ASK_WIRE[self.jobs.get(J1)["status"]], "unknown")


class TempFiles(Base):
    def test_a_hard_linked_predictable_temp_name_does_not_truncate_another_file(self):
        self.dir.mkdir()
        valuable = Path(self.tmp.name) / "valuable.txt"
        valuable.write_text("keep me")
        os.link(valuable, self.dir / f".{J1}.tmp")
        self.jobs.create(J1, card="c1")
        self.assertEqual(valuable.read_text(), "keep me")
        self.assertEqual(self.journal.read(J1)["state"], "queued")


class ReadSafety(Base):
    def swap_before_open(self, install):
        """Patches that run ``install()`` once, between the last check of the record and the moment it is opened.

        The base journal checks the path with lstat and then reads it by path, so the swap follows
        that lstat; the descriptor-based reader opens the entry relative to its directory, so the
        swap precedes that open. Either way nothing else has looked at the record yet."""
        done, name = [], f"{J1}.json"
        real_lstat, real_open = os.lstat, os.open

        def fire():
            if not done:
                done.append(1)
                install()

        def lstat(p, *a, **k):
            out = real_lstat(p, *a, **k)
            if str(p).endswith(name) and k.get("dir_fd") is None:
                fire()
            return out

        def opening(p, flags, *a, **k):
            if str(p) == name and k.get("dir_fd") is not None:
                fire()
            return real_open(p, flags, *a, **k)

        return patch.object(os, "lstat", lstat), patch.object(os, "open", opening), done

    def test_a_record_swapped_for_a_symlink_just_before_it_is_opened_is_not_followed(self):
        """R5-jobs-028: the swap happens at the read's own check, not at a housekeeping lstat."""
        self.jobs.create(J1, card="c1")
        stolen = Path(self.tmp.name) / "stolen.json"
        stolen.write_text(json.dumps({"v": 1, "job": J1, "state": "done", "started": self.clock(), "card": "c1",
                                      "reply": "STOLEN"}))
        os.chmod(stolen, 0o600)
        path = self.dir / f"{J1}.json"

        def install():
            path.unlink()
            path.symlink_to(stolen)

        a, b, done = self.swap_before_open(install)
        with a, b:
            got = self.journal.read(J1)
        self.assertTrue(done, "the swap must have happened")
        self.assertNotIn("STOLEN", json.dumps(got, default=str))

    def test_a_record_swapped_for_a_fifo_just_before_it_is_opened_does_not_block(self):
        """R5-jobs-029: a FIFO that is already there was refused by the base; one swapped in after the check was not."""
        self.jobs.create(J1, card="c1")
        path = self.dir / f"{J1}.json"

        def install():
            path.unlink()
            os.mkfifo(path)

        a, b, done = self.swap_before_open(install)
        out = []

        def run():
            with a, b:
                out.append(self.journal.read(J1))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "the read must not block on a FIFO")
        self.assertTrue(out and out[0].get("corrupt"))

    def test_a_path_swapped_for_a_symlink_after_the_size_check_is_not_followed(self):
        self.jobs.create(J1, card="c1")
        big = Path(self.tmp.name) / "big.json"
        big.write_text(json.dumps({"v": 1, "job": J1, "state": "done", "started": self.clock(), "card": "c1",
                                   "reply": "STOLEN", "pad": "x" * (jobs.AskJournal.MAX_RECORD_BYTES + 10)}))
        path = self.dir / f"{J1}.json"
        real = os.lstat
        swapped = []

        def lstat(p, *a, **k):
            out = real(p, *a, **k)
            if str(p).endswith(f"{J1}.json") and not swapped:
                swapped.append(1)
                path.unlink()
                path.symlink_to(big)
            return out

        with patch.object(os, "lstat", lstat):
            self.boot()
            st = self.jobs.get(J1)
        self.assertNotIn("STOLEN", json.dumps(st, default=str))

    def test_a_fifo_in_place_of_a_record_does_not_block_a_poll(self):
        self.dir.mkdir()
        os.mkfifo(self.dir / f"{J1}.json")
        out = []
        t = threading.Thread(target=lambda: out.append(self.jobs.get(J1)), daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "the read must not block on a FIFO")
        self.assertEqual(jobs.ASK_WIRE[out[0]["status"]], "unknown")

    def test_a_deeply_nested_record_is_corrupt_not_an_outage(self):
        self.dir.mkdir()
        (self.dir / f"{J1}.json").write_text("[" * 20000 + "]" * 20000)
        self.assertEqual(jobs.ASK_WIRE[self.jobs.get(J1)["status"]], "unknown")
        self.jobs.housekeeping()  # the parser failure must not escape housekeeping either
        os.utime(self.dir / f"{J1}.json", (1, 1))
        self.jobs.housekeeping()
        self.assertEqual(self.files(), [])


class Validation(Base):
    def put(self, job=J1, **over):
        data = {"v": 1, "job": job, "state": "done", "started": self.clock(), "card": "c1", "reply": "SECRET"}
        data.update(over)
        self.dir.mkdir(exist_ok=True, mode=0o700)
        p = self.dir / f"{job}.json"
        text = json.dumps({k: v for k, v in data.items() if v is not ...})
        text = text.replace('"__NAN__"', "NaN").replace('"__INF__"', "Infinity")
        p.write_text(text)
        os.chmod(p, 0o600)
        return p

    def test_non_finite_and_far_future_timestamps_do_not_keep_a_reply_forever(self):
        for bad in ("__NAN__", "__INF__", 1e300, self.clock() + 10 ** 7):
            with self.subTest(started=bad):
                p = self.put(started=bad)
                st = self.jobs.get(J1)
                self.assertNotIn("reply", st or {}, "a record with an invalid time is not recoverable")
                os.utime(p, (1, 1))
                self.jobs.housekeeping()
                self.assertEqual(self.files(), [], "and it still expires by file age")

    def test_unsupported_version_missing_version_state_or_card_are_unknown(self):
        for over in ({"v": 999}, {"v": ...}, {"state": ...}, {"card": ...}, {"state": "weird"}):
            with self.subTest(over=over):
                self.put(**{"state": "queued", **over})
                st = self.jobs.get(J1)
                self.assertEqual(jobs.ASK_WIRE[st["status"]], "unknown")
                (self.dir / f"{J1}.json").unlink()


class Size(Base):
    keep = True

    def test_an_oversized_reply_is_stored_as_unkept_not_as_an_unreadable_record(self):
        self.jobs.create(J1, card="c1")
        self.jobs.mark_sent(J1)
        self.jobs.finish(J1, "x" * 300000, "p", "m")
        self.assertLessEqual(os.path.getsize(self.dir / f"{J1}.json"), jobs.AskJournal.MAX_RECORD_BYTES)
        self.boot()
        st = self.jobs.get(J1)
        self.assertEqual(jobs.ASK_WIRE[st["status"]], "unknown")
        self.assertIn("too large", st["detail"])

    def test_a_reply_that_is_not_json_native_does_not_break_settling(self):
        class Kind(enum.Enum):
            A = 1

        jobs.AskJobs.MAX_SETTLED, old = 2, jobs.AskJobs.MAX_SETTLED
        self.addCleanup(setattr, jobs.AskJobs, "MAX_SETTLED", old)
        for i in range(5):
            self.jobs.create(hexid(i), card="c")
            self.assertTrue(self.jobs.finish(hexid(i), "ok", Kind.A, object()))
        self.assertEqual(len(self.jobs), 2, "retention still enforced")
        self.assertEqual([p for p in self.dir.iterdir() if p.name.endswith(".tmp")], [])


class Durability(Base):
    def test_a_marker_and_its_directory_are_synced_before_the_call_is_trusted(self):
        calls = []
        real = os.fsync
        with patch.object(os, "fsync", lambda fd: (calls.append(os.fstat(fd).st_mode), real(fd))[1]):
            self.jobs.create(J1, card="c1")
            n = len(calls)
            self.journal.delete(J1)
        self.assertGreaterEqual(n, 2)
        self.assertTrue(any(stat.S_ISREG(m) for m in calls[:n]), "the record itself")
        self.assertTrue(any(stat.S_ISDIR(m) for m in calls[:n]), "and the directory entry")
        self.assertTrue(stat.S_ISDIR(calls[-1]), "a delete is synced too")


class CapRace(Base):
    def fill(self, n, **rec_over):
        for i in range(n):
            self.assertTrue(self.journal.write(hexid(i + 1000), {"state": "done", "started": self.clock(), "card": "c", **rec_over}))
            os.utime(self.dir / f"{hexid(i + 1000)}.json", (100 + i, 100 + i))

    def test_two_workers_cannot_both_take_the_last_slot(self):
        self.fill(jobs.AskJournal.MAX_FILES - 1)
        a, b = self.other(), self.other()
        gate = threading.Barrier(2, timeout=0.5)
        for w in (a, b):
            real = w._journal._make_room

            def room(*args, _real=real, **kw):
                out = _real(*args, **kw)
                try:
                    gate.wait()
                except threading.BrokenBarrierError:
                    pass
                return out

            w._journal._make_room = room
        ts = [threading.Thread(target=lambda w=w, j=j: w.create(j, card="c")) for w, j in ((a, J1), (b, J2))]
        [t.start() for t in ts]
        [t.join(10) for t in ts]
        self.assertLessEqual(len(self.files()), jobs.AskJournal.MAX_FILES)

    def test_a_live_job_of_another_worker_is_never_evicted(self):
        peer = self.other()
        peer._journal.pid = os.getppid()  # a live process that is not this one
        for i in range(jobs.AskJournal.MAX_FILES):
            peer.create(hexid(i + 1), card="c")
        ok = self.journal.write(J1, {"state": "queued", "started": self.clock(), "card": "c"})
        self.assertFalse(ok, "no room and nothing evictable: memory only")
        self.assertEqual(len(self.files()), jobs.AskJournal.MAX_FILES)
        self.assertNotIn(f"{J1}.json", self.files())


class UnverifiedAbsence(Base):
    def lose_directory(self, journal=None):
        def broken():
            raise PermissionError("state unavailable")

        (journal or self.journal)._directory = broken

    def test_a_restarted_table_does_not_call_an_unreadable_directory_empty(self):
        self.keep = True
        self.jobs.create(J1, card="c1")
        self.jobs.finish(J1, "ANSWER", "p", "m")
        self.boot()
        self.lose_directory()
        self.assertFalse(self.journal.verified_absent(J1))
        with self.assertRaises(jobs.DiscardFailed):
            self.jobs.discard(J1)
        self.assertEqual(self.files(), [f"{J1}.json"])

    def test_a_resolver_failing_with_a_non_os_error_is_unknown_not_absent_and_not_a_crash(self):
        """R5-jobs-030: the base caught only OSError, so these escaped from discard() and verified_absent()."""
        self.keep = True
        self.jobs.create(J1, card="c1")
        self.jobs.finish(J1, "ANSWER", "p", "m")
        self.boot()
        for exc in (RuntimeError("settings gone"), ValueError("bad profile")):
            with self.subTest(exc=type(exc).__name__):
                def broken(exc=exc):
                    raise exc

                self.journal._directory = broken
                self.assertFalse(self.journal.verified_absent(J1))
                with self.assertRaises(jobs.DiscardFailed):
                    self.jobs.discard(J1)
        self.assertEqual(self.files(), [f"{J1}.json"])

    def test_unavailable_storage_reads_as_unknown_not_as_an_expired_job(self):
        self.jobs.create(J1, card="c1")
        self.jobs.mark_sent(J1)
        self.boot()
        self.lose_directory()
        st = self.jobs.get(J1)
        self.assertIsNotNone(st, "a 404 would claim the job does not exist")
        self.assertEqual(jobs.ASK_WIRE[st["status"]], "unknown")
        self.assertFalse(st.get("resubmit_safe", False))


class WorkerOwnership(Base):
    def test_a_marker_owned_by_a_live_worker_is_pending_not_safe_to_resubmit(self):
        a = self.other()
        a._journal.pid = os.getppid()
        a.create(J1, card="c1")
        b = self.other()
        st = b.get(J1)
        self.assertEqual(jobs.ASK_WIRE[st["status"]], "pending")

    def test_a_marker_whose_owner_is_gone_is_not_submitted(self):
        a = self.other()
        a._journal.pid = dead_pid()
        a.create(J1, card="c1")
        self.assertEqual(jobs.ASK_WIRE[self.other().get(J1)["status"]], "not_submitted")

    def test_the_same_marker_flips_between_pending_and_not_submitted_with_its_owner(self):
        """R5-jobs-031: ignoring ownership entirely satisfied the dead-owner test alone, so one marker is read both ways."""
        for state in ("queued", "sent"):
            with self.subTest(state=state):
                job = hexid(7 if state == "queued" else 8)
                a = self.other()
                ticket = a.create(job, card="c1")
                if state == "sent":
                    a.mark_sent(ticket)
                path = self.dir / f"{job}.json"
                body = json.loads(path.read_text())
                outcomes = {}
                for label, pid in (("live", os.getppid()), ("dead", dead_pid())):
                    body["owner"] = {"pid": pid, "inst": f"other-{label}"}
                    path.write_text(json.dumps(body))
                    os.chmod(path, 0o600)
                    outcomes[label] = jobs.ASK_WIRE[self.other().get(job)["status"]]
                self.assertEqual(outcomes["live"], "pending")
                self.assertEqual(outcomes["dead"], "not_submitted" if state == "queued" else "unknown")


class SettledAcrossRestart(Base):
    keep = True

    def test_the_settled_bound_holds_for_records_recovered_after_a_restart(self):
        for i in range(jobs.AskJobs.MAX_SETTLED):
            self.jobs.create(hexid(i), card="c")
            self.jobs.finish(hexid(i), "A", "p", "m")
            self.clock.t += 1
        self.boot()
        for i in range(jobs.AskJobs.MAX_SETTLED, 2 * jobs.AskJobs.MAX_SETTLED):
            self.jobs.create(hexid(i), card="c")
            self.jobs.finish(hexid(i), "B", "p", "m")
            self.clock.t += 1
        self.assertLessEqual(len(self.files()), jobs.AskJobs.MAX_SETTLED)
        self.assertIsNone(self.jobs.get(hexid(0)), "the oldest answers went, in completion order")
        self.assertEqual(self.jobs.get(hexid(2 * jobs.AskJobs.MAX_SETTLED - 1))["reply"], "B")


class DeleteResurrection(Base):
    keep = True

    def test_a_deleted_job_is_not_written_again_by_its_live_owner(self):
        a = self.other()
        a._journal.pid = os.getppid()
        a.create(J1, card="c1")
        b = self.other()
        self.assertTrue(b.discard(J1))
        a.finish(J1, "LATE ANSWER", "p", "m")
        self.assertIsNone(self.other().get(J1), "the deleted answer must not come back")
        self.assertNotIn(f"{J1}.json", self.files())
        self.assertIsNone(a.get(J1), "and the owner stops serving it")


class CleanupRetry(Base):
    def test_a_directory_that_cannot_be_listed_keeps_the_cleanup_timer_armed(self):
        self.jobs.create(J1, card="c1")
        self.jobs.start_background(3600)
        self.addCleanup(lambda: self.jobs._timer and self.jobs._timer.cancel())
        with self.jobs._lock:
            self.jobs._timer.cancel()
            self.jobs._timer = None
        boom = PermissionError("unreadable")
        with patch.object(os, "listdir", side_effect=boom), patch.object(os, "scandir", side_effect=boom):
            self.boot()
            self.jobs.start_background(3600)
            with self.jobs._lock:
                self.jobs._timer.cancel()
                self.jobs._timer = None
            self.jobs._tick()
        self.assertIsNotNone(self.jobs._timer, "files may still exist: keep trying")
        self.jobs._timer.cancel()


class ProfileScope(Base):
    """01-009 (context lost in the worker), 01-024 (one table, two profiles), 01-025 (evicted answer)."""

    def test_a_worker_thread_without_the_request_context_writes_to_the_jobs_own_profile(self):
        who = contextvars.ContextVar("who", default="home")
        dirs = {"home": Path(self.tmp.name) / "home", "work": Path(self.tmp.name) / "work"}
        table = jobs.AskJobs(self.clock, jobs.AskJournal(lambda: dirs[who.get()], lambda: True, self.clock))
        who.set("work")
        table.create(J1, card="c1")

        def worker():  # a pool thread: it starts with a fresh context, so who.get() is "home"
            table.mark_sent(J1)
            table.finish(J1, "WORK", "p", "m")

        t = threading.Thread(target=worker)
        t.start()
        t.join(10)
        self.assertFalse((dirs["home"]).exists() and list(dirs["home"].glob("*.json")), "nothing leaks to the default profile")
        self.assertEqual(json.loads((dirs["work"] / f"{J1}.json").read_text())["state"], "done")

    def test_one_table_does_not_answer_a_job_to_another_profile(self):
        who = contextvars.ContextVar("who2", default="a")
        dirs = {"a": Path(self.tmp.name) / "a", "b": Path(self.tmp.name) / "b"}
        table = jobs.AskJobs(self.clock, jobs.AskJournal(lambda: dirs[who.get()], lambda: True, self.clock))
        table.create(J1, card="c1")
        table.finish(J1, "A ANSWER", "p", "m")
        who.set("b")
        self.assertIsNone(table.get(J1))
        self.assertIsNone(table.card(J1))
        self.assertFalse(table.discard(J1))
        who.set("a")
        self.assertEqual(table.get(J1)["reply"], "A ANSWER")

    def test_an_evicted_answer_whose_record_cannot_be_deleted_is_not_recovered(self):
        self.keep = True
        jobs.AskJobs.MAX_SETTLED, old = 1, jobs.AskJobs.MAX_SETTLED
        self.addCleanup(setattr, jobs.AskJobs, "MAX_SETTLED", old)
        self.boot()
        self.jobs.create(J1, card="c")
        self.jobs.finish(J1, "A", "p", "m")
        real = self.journal.delete
        self.journal.delete = lambda job, *a, **k: False if job == J1 else real(job, *a, **k)
        with patch.object(self.journal, "verified_absent", return_value=False):
            self.jobs.create(J2, card="c")
            self.jobs.finish(J2, "B", "p", "m")
            self.assertIsNone(self.jobs.get(J1), "the settled limit is not bypassed by a failed delete")
        self.journal.delete = real
        self.jobs.housekeeping()
        self.assertNotIn(f"{J1}.json", self.files(), "and the delete is retried")


try:
    import fastapi
    from fastapi.testclient import TestClient
except Exception:  # noqa: BLE001
    fastapi = None


@unittest.skipIf(fastapi is None, "fastapi not installed")
class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "ask"
        self.keep = True
        import importlib.util

        spec = importlib.util.spec_from_file_location("ub_api_r4s07", ROOT / "dashboard/plugin_api.py")
        self.api = importlib.util.module_from_spec(spec)
        sys.modules["ub_api_r4s07"] = self.api
        spec.loader.exec_module(self.api)
        self.dir_fn = lambda: self.dir
        needs = types.SimpleNamespace(
            ask_enabled=lambda: True,
            card_context=lambda b, t: {"id": t},
            build_ask_messages=lambda c, h: list(h),
            ask_journal_dir=lambda: self.dir_fn(),
            ask_persist_replies=lambda: self.keep,
        )
        self.api._needs = lambda: needs
        self.api._checked_card_ref = lambda i, b, t: ("factory", "t_1")
        self.api._loaded_ctx = lambda: types.SimpleNamespace(llm=types.SimpleNamespace(
            complete=lambda m, **kw: types.SimpleNamespace(text="Pick A", provider="p", model="m")))
        self.api._ASK_JOBS = self.api.jobs.AskJobs(journal=self.api.jobs.AskJournal(lambda: self.api._ask_journal_dir(), lambda: self.keep))
        app = fastapi.FastAPI()
        app.include_router(self.api.router)
        self.c = TestClient(app)

    def restart(self):
        self.api._ASK_JOBS = self.api.jobs.AskJobs(journal=self.api.jobs.AskJournal(lambda: self.api._ask_journal_dir(), lambda: self.keep))

    def ask(self):
        job = self.c.post("/card/ask", json={"id": "x", "messages": [{"role": "user", "content": "q"}]}).json()["job"]
        for _ in range(80):
            p = self.c.get(f"/card/ask/{job}?poll=true&card={CARD}").json()
            if not p.get("pending"):
                return job
            time.sleep(0.05)
        self.fail("ask did not settle")

    def test_the_card_binding_is_not_optional_on_read_or_delete(self):
        job = self.ask()
        for suffix in ("", "?card=", "?poll=true", "?poll=true&card="):
            with self.subTest(read=suffix):
                self.assertIn(self.c.get(f"/card/ask/{job}{suffix}").status_code, (400, 404))
        self.assertIn(self.c.delete(f"/card/ask/{job}").status_code, (400, 404))
        self.assertIn(self.c.delete(f"/card/ask/{job}?card=").status_code, (400, 404))
        self.assertEqual(self.c.get(f"/card/ask/{job}?poll=true&card={CARD}").json()["reply"], "Pick A")

    def test_a_record_without_a_stored_card_answers_nobody(self):
        self.api._ASK_JOBS.create(J1)  # no card bound
        self.assertEqual(self.c.get(f"/card/ask/{J1}?poll=true&card={CARD}").status_code, 404)

    def test_a_request_id_of_another_cards_job_is_refused(self):
        job = self.ask()
        self.api._checked_card_ref = lambda i, b, t: ("factory", "other")
        r = self.c.post("/card/ask", json={"id": "y", "request_id": job, "messages": [{"role": "user", "content": "q"}]})
        self.assertEqual(r.status_code, 409)

    def test_unavailable_state_is_not_reported_as_an_unknown_job(self):
        job = self.ask()
        self.restart()

        def broken():
            raise PermissionError("no state")

        self.dir_fn = broken
        r = self.c.get(f"/card/ask/{job}?poll=true&card={CARD}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "unknown")
        self.assertFalse(r.json()["resubmit_safe"])
        self.assertEqual(self.c.delete(f"/card/ask/{job}?card={CARD}").status_code, 500, "no false success for an absent file")

    def test_a_failed_delete_does_not_promise_expiry(self):
        job = self.ask()
        with patch.object(self.api._ASK_JOBS._journal, "delete", return_value=False), \
                patch.object(self.api._ASK_JOBS._journal, "verified_absent", return_value=False):
            r = self.c.delete(f"/card/ask/{job}?card={CARD}")
        self.assertEqual(r.status_code, 500)
        detail = r.json()["detail"].lower()
        self.assertNotIn("on its own", detail)
        self.assertNotIn("15 minutes", detail)
        self.assertIn("not deleted", detail)


class Docs(unittest.TestCase):
    def test_the_documented_marker_location_matches_the_implementation(self):
        for name in ("README.md", "docs/operator-guide.md", "docs/architecture.md"):
            text = (ROOT / name).read_text()
            with self.subTest(doc=name):
                self.assertNotIn("ask/<job>.json", text, "markers live under a per-profile directory")
        guide = (ROOT / "docs/operator-guide.md").read_text()
        self.assertIn("ask/<profile>/<job>.json", guide)
        self.assertIn("per profile", guide)
        self.assertIn("organization", guide.lower())


if __name__ == "__main__":
    unittest.main()
