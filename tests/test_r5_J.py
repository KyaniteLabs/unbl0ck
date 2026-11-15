"""Round-5 regression tests for the Ask job table and restart journal, the operator recovery tool
and the Ask endpoint (lane J). One test per finding; each drives the behaviour through the entry
point the code had before its fix (a worker's generation token is passed only where the method
takes one), so it fails on that code for the behavioural reason."""

import contextlib
import fcntl
import importlib.util
import inspect
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


jobs = _load("ub_jobs_r5j", "jobs.py")
rec = _load("ub_recovery_r5j", "recovery.py")
J1, J2, J3 = "a" * 32, "b" * 32, "c" * 32


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def gen_of(table, job):
    """The generation token of ``job``, or None where the table has none."""
    return next((st.get("gen") for k, st in table._jobs.items() if isinstance(k, tuple) and k[1] == job), None)


def call(method, job, *args, gen=None):
    """Call a worker method, passing the generation token only where the method takes one."""
    kw = {"gen": gen if gen is not None else gen_of(method.__self__, job)} if "gen" in inspect.signature(method).parameters else {}
    return method(job, *args, **kw)


@contextlib.contextmanager
def refuse(fn_name, names, exc=PermissionError):
    """``os.<fn_name>`` raises for any path whose base name is in ``names``."""
    real = getattr(os, fn_name)

    def wrapper(path, *a, **k):
        if isinstance(path, (str, bytes, os.PathLike)) and os.path.basename(os.fsdecode(path)) in names:
            raise exc(f"refused: {os.path.basename(os.fsdecode(path))}")
        return real(path, *a, **k)

    with patch.object(os, fn_name, wrapper):
        yield


class JobsBase(unittest.TestCase):
    keep = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dir = self.root / "ask"
        self.clock = Clock()
        self.table = self.make()

    def make(self, directory=None, keep=None):
        journal = jobs.AskJournal(directory or (lambda: self.dir), keep or (lambda: self.keep), self.clock)
        return jobs.AskJobs(self.clock, journal)

    def record(self, job, **fields):
        self.dir.mkdir(mode=0o700, exist_ok=True)
        data = {"v": 1, "job": job, "state": "done", "started": self.clock(), "card": "c1", **fields}
        p = self.dir / f"{job}.json"
        p.write_text(json.dumps(data))
        os.chmod(p, 0o600)
        return p


class ScopeLeak(JobsBase):
    def test_an_unresolvable_scope_shows_no_saved_answer(self):
        """R5-jobs-001: a caller whose profile cannot be resolved (or resolves to nothing) sees no
        other profile's answer."""
        where = {"d": self.root / "work"}

        def resolver():
            v = where["d"]
            if isinstance(v, Exception):
                raise v
            return v

        table = self.make(directory=resolver)
        table.create(J1, card="c1")
        call(table.finish, J1, "WORK PRIVATE", "p", "m")
        for broken in (PermissionError("no profile"), None):
            with self.subTest(resolver=repr(broken)):
                where["d"] = broken
                self.assertNotIn("WORK PRIVATE", json.dumps(table.get(J1), default=str))

    def test_reply_persistence_follows_the_profile_that_created_the_job(self):
        """R5-jobs-002: a work-profile job with persistence off is not saved because the worker
        thread's default profile keeps replies."""
        ctx = threading.local()
        table = self.make(directory=lambda: self.root / getattr(ctx, "p", "default"),
                          keep=lambda: getattr(ctx, "p", "default") == "default")
        ctx.p = "work"
        table.create(J1, card="c1")
        gen = gen_of(table, J1)
        t = threading.Thread(target=lambda: call(table.finish, J1, "WORK SECRET", "p", "m", gen=gen))
        t.start()
        t.join()
        self.assertNotIn("WORK SECRET", (self.root / "work" / f"{J1}.json").read_text())

    def test_recovered_directories_stay_under_background_cleanup(self):
        """R5-jobs-008: an answer recovered in a work-profile request is still pruned by the timer,
        which runs in the default profile and has no request."""
        ctx = threading.local()
        table = self.make(directory=lambda: self.root / getattr(ctx, "p", "default"))
        ctx.p = "work"
        table.create(J1, card="c1")
        call(table.finish, J1, "WORK ANSWER", "p", "m")
        restarted = self.make(directory=lambda: self.root / getattr(ctx, "p", "default"))
        restarted._bg_every = 3600.0
        self.assertIn("WORK ANSWER", json.dumps(restarted.get(J1)))
        self.assertIsNotNone(restarted._timer, "a recovered answer must keep the cleanup timer armed")
        restarted._timer.cancel()
        self.clock.t += jobs.AskJobs.TTL_S + 1
        out = []
        t = threading.Thread(target=lambda: out.append(restarted._tick()))  # a fresh thread: the default profile
        t.start()
        t.join()
        restarted._timer and restarted._timer.cancel()
        self.assertFalse((self.root / "work" / f"{J1}.json").exists(), "the work answer outlived its retention")

    def test_the_same_id_in_another_profile_is_a_new_job(self):
        """R5-jobs-012: ids are scoped like visibility: profile B can create an id profile A uses."""
        ctx = types.SimpleNamespace(p="a")
        table = self.make(directory=lambda: self.root / ctx.p)
        table.create(J1, card="card-a")
        ctx.p = "b"
        self.assertIsNone(table.get(J1))
        table.create(J1, card="card-b")
        self.assertEqual(table.get(J1)["card"], "card-b")


class StaleToken(JobsBase):
    def test_a_deleted_jobs_late_worker_cannot_finish_its_replacement(self):
        """R5-jobs-003: create, delete, create again under the same id: the first worker's answer is
        never given to the new job. (With a journal the deleted id may instead stay fenced, so the
        second create is refused; the late answer must still not appear.)"""
        for name, table in (("memory", jobs.AskJobs(self.clock)), ("journal", self.table)):
            with self.subTest(table=name):
                table.create(J1, card="old-card")
                old = gen_of(table, J1)
                table.discard(J1)
                with contextlib.suppress(jobs.IllegalTransition):
                    table.create(J1, card="new-card")
                call(table.finish, J1, "OLD PRIVATE ANSWER", "p", "m", gen=old)
                self.assertNotIn("OLD PRIVATE ANSWER", json.dumps(table.get(J1), default=str))

    def test_a_record_swapped_in_during_the_rename_is_not_kept_as_the_write(self):
        """R5-jobs-007: a stale "queued" record substituted for the temporary file is not published
        as the "sent" marker, so a restart never offers a safe resubmit."""
        self.table.create(J1, card="c1")
        queued = (self.dir / f"{J1}.json").read_bytes()
        real, done = os.rename, []

        def rename(src, dst, *a, **k):
            if os.path.basename(os.fsdecode(src)).startswith(f".{J1}") and not done:
                done.append(1)
                at = k.get("src_dir_fd")
                os.unlink(src, dir_fd=at)
                fd = os.open(src, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=at)
                os.write(fd, queued)
                os.close(fd)
            return real(src, dst, *a, **k)

        with patch.object(os, "rename", rename):
            call(self.table.mark_sent, J1)
        self.assertTrue(done)
        st = self.make().get(J1)
        self.assertNotEqual(jobs.ASK_WIRE[st["status"]] if st else None, "not_submitted")


class MultistepWrite(JobsBase):
    def test_two_tables_cannot_both_admit_the_same_id(self):
        """R5-jobs-004: two workers that both saw no job cannot both create (and so both send) it."""
        a, b = self.make(), self.make()
        self.assertIsNone(a.get(J1))
        self.assertIsNone(b.get(J1))
        a.create(J1, card="c1")
        with self.assertRaises(jobs.IllegalTransition):
            b.create(J1, card="c1")

    def test_a_job_deleted_by_another_worker_is_not_served_from_memory(self):
        """R5-jobs-010: worker A finished the job; worker B deleted it; A no longer serves it."""
        a, b = self.make(), self.make()
        a.create(J1, card="c1")
        call(a.finish, J1, "ANSWER", "p", "m")
        self.assertTrue(b.discard(J1))
        self.assertIsNone(a.get(J1))

    def test_a_saved_answer_whose_eviction_failed_is_not_recovered_after_a_restart(self):
        """R5-jobs-015: over the settled bound after a restart, an oldest answer that could not be
        removed is not served again."""
        with patch.object(jobs.AskJobs, "MAX_SETTLED", 2):
            for job in (J1, J2):
                self.table.create(job, card="c1")
                self.clock.t += 1
                call(self.table.finish, job, f"ANSWER {job[0]}", "p", "m")
            table = self.make()
            table.create(J3, card="c1")
            self.clock.t += 1
            with refuse("unlink", {f"{J1}.json"}):
                call(table.finish, J3, "ANSWER c", "p", "m")
            self.assertTrue((self.dir / f"{J1}.json").exists())
            self.assertNotIn("ANSWER a", json.dumps(table.get(J1), default=str))

    def test_answers_whose_removal_failed_stay_evicted_however_many_there_are(self):
        """R5-jobs-016: evicted answers whose files could not be removed are never served again,
        even past any bound on how many such failures are remembered."""
        with patch.object(jobs.AskJobs, "MAX_SETTLED", 1), patch.object(jobs.AskJobs, "MAX_UNDELETED", 1, create=True):
            with refuse("unlink", {f"{J1}.json", f"{J2}.json"}):
                for job in (J1, J2, J3):
                    self.table.create(job, card="c1")
                    self.clock.t += 1
                    call(self.table.finish, job, f"ANSWER {job[0]}", "p", "m")
            self.assertTrue((self.dir / f"{J1}.json").exists())
            self.assertNotIn("ANSWER a", json.dumps(self.table.get(J1), default=str))

    def test_a_dropped_tombstone_is_not_a_successful_delete(self):
        """R5-jobs-009: when the tombstone cannot be written, the delete is refused and the record
        stays, so a live owner cannot recreate an answer the caller was told is gone."""
        owner = self.make()
        owner._journal.pid = os.getppid()  # another live process owns the job
        owner.create(J1, card="c1")
        with refuse("open", {f"{J1}.gone"}):
            with self.assertRaises(jobs.DiscardFailed):
                self.make().discard(J1)
        self.assertTrue((self.dir / f"{J1}.json").exists())


class MalformedRecord(JobsBase):
    def test_huge_integers_in_a_record_read_as_unknown_not_a_crash(self):
        """R5-jobs-013: started=10**1000 or an owner pid of 10**1000 is an implausible record, not
        an OverflowError out of a poll or housekeeping."""
        for job, fields in ((J1, {"started": 10 ** 1000}), (J2, {"state": "queued", "owner": {"pid": 10 ** 1000, "inst": "x"}})):
            with self.subTest(job=job[0]):
                self.record(job, **fields)
                st = self.table.get(job)
                self.assertIn(jobs.ASK_WIRE[st["status"]], ("unknown", "not_submitted"))
        self.table.housekeeping()


class Retention(JobsBase):
    def test_a_kept_oldest_record_does_not_stop_the_settled_bound(self):
        """R5-jobs-014: with the oldest settled record protected, the next oldest is removed."""
        for job, at in ((J1, 1.0), (J2, 2.0)):
            self.table._journal.write(job, {"state": "done", "started": self.clock(), "settled_at": self.clock() + at, "card": "c1"})
        self.table._journal.enforce_settled(1, keep=lambda j: j == J1)
        self.assertTrue((self.dir / f"{J1}.json").exists())
        self.assertFalse((self.dir / f"{J2}.json").exists())


class Budget(JobsBase):
    def test_a_held_journal_lock_does_not_multiply_the_cleanup_wait(self):
        """R5-jobs-018: expiring many jobs while another process holds the journal lock waits one
        lock period for the whole pass, not one per record."""
        with patch.object(jobs.AskJournal, "LOCK_WAIT_S", 0.2):
            for i in range(15):
                self.table.create(f"{i:032x}", card="c1")
            lock = os.open(self.dir / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            self.addCleanup(os.close, lock)
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.clock.t += jobs.AskJobs.TTL_S + 1
            began = time.monotonic()
            self.table.housekeeping()
            self.table._timer and self.table._timer.cancel()
        self.assertLess(time.monotonic() - began, 1.5)


class Containment(JobsBase):
    def test_a_parent_swapped_for_a_link_before_the_open_is_not_followed(self):
        """R5-jobs-005: the journal's parent replaced by a link to another directory holding the
        same child name, right before the directory is opened: nothing is written there."""
        prof = self.root / "prof"
        (prof / "ask").mkdir(parents=True, mode=0o700)
        foreign = self.root / "foreign"
        (foreign / "ask").mkdir(parents=True, mode=0o700)
        real, done = os.open, []

        def opener(path, *a, **k):
            if k.get("dir_fd") is None and str(path).rstrip("/").endswith(("/prof", "/prof/ask")) and not done:
                done.append(1)
                prof.rename(self.root / "prof.real")
                prof.symlink_to(foreign, target_is_directory=True)
            return real(path, *a, **k)

        table = self.make(directory=lambda: prof / "ask")
        with patch.object(os, "open", opener), self.assertRaises(jobs.ScopeUnavailable):
            table.create(J1, card="c1")
        self.assertTrue(done)
        self.assertEqual(list((foreign / "ask").iterdir()), [])


class RecoveryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.d = self.home / "plugin-data" / "unbl0ck"
        self.d.mkdir(parents=True)

    def put(self, name, content=b'{"a": "v"}', where=None):
        p = (where or self.d) / name
        p.parent.mkdir(mode=0o700, exist_ok=True)
        p.write_bytes(content)
        os.chmod(p, 0o600)
        return p


class RecoveryContainment(RecoveryBase):
    def test_the_data_directory_swapped_for_a_link_after_the_check_is_not_used(self):
        """R5-jobs-006: plugin-data/unbl0ck replaced by a link after it was checked: preserve reads
        and writes only the installation it checked."""
        self.put("hidden.json", b'{"a": "mine"}')
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        self.put("hidden.json", b'{"a": "theirs"}', where=elsewhere)
        swapped = []

        def swap():
            if not swapped:
                swapped.append(1)
                self.d.rename(self.home / "plugin-data" / "unbl0ck.real")
                self.d.symlink_to(elsewhere, target_is_directory=True)

        real_open, real_lstat = os.open, os.lstat

        def opener(path, *a, **k):
            if os.path.basename(os.fsdecode(path)) == "hidden.json":
                swap()
            return real_open(path, *a, **k)

        def lstat(path, *a, **k):
            if os.path.basename(os.fsdecode(path)) == "hidden.json":
                swap()
            return real_lstat(path, *a, **k)

        with patch.object(os, "open", opener), patch.object(os, "lstat", lstat):
            out = rec.preserve("hidden", self.home)
        self.assertTrue(swapped)
        self.assertFalse((elsewhere / "recovery").exists(), "a backup was written into the linked installation")
        self.assertNotEqual(out.get("sha256"), rec._sha(b'{"a": "theirs"}'))


class RecoveryBudget(RecoveryBase):
    def test_the_backup_listing_stops_when_its_time_budget_runs_out(self):
        """R5-jobs-019: the time budget starts before the directory is listed, so a huge archive is
        not enumerated in full."""
        for i in range(300):
            self.put(f"hidden-{i:04d}.bak", where=self.d / "recovery")
        seen = [0]
        real_scandir = os.scandir

        @contextlib.contextmanager
        def counting(path):
            with real_scandir(path) as it:
                def gen():
                    for e in it:
                        seen[0] += 1
                        yield e
                yield gen()

        tick = [0.0]

        def monotonic():
            tick[0] += 1.0
            return tick[0]

        with patch.object(os, "scandir", counting), patch.object(time, "monotonic", monotonic):
            rec.diagnose(self.home)
        self.assertLess(seen[0], 300)

    def test_every_byte_read_from_backups_is_charged_to_the_budget(self):
        """R5-jobs-020: the read budget is enforced on what is read, not on a size checked before:
        two 8-byte backups whose pre-read size claims 1 byte are not both read on a 10-byte budget."""
        for i in range(2):
            self.put(f"hidden-{i}.bak", b'{"a":1}', where=self.d / "recovery")  # 8 bytes each
        real_lstat, real_stat = os.lstat, os.stat

        def small(st):
            v = list(st)
            v[6] = 1
            return os.stat_result(v)

        def lstat(p, *a, **k):
            st = real_lstat(p, *a, **k)
            return small(st) if os.fsdecode(p).endswith(".bak") else st

        def stat_(p, *a, follow_symlinks=True, **k):
            st = real_stat(p, *a, follow_symlinks=follow_symlinks, **k)
            return small(st) if not follow_symlinks and os.fsdecode(p).endswith(".bak") else st

        with patch.object(rec, "BACKUP_READ_BUDGET", 10), patch.object(os, "lstat", lstat), \
                patch.object(os, "stat", stat_):
            out = rec.diagnose(self.home)
        checked = [r for r in out["backups"] if r["status"] != "not_checked"]
        self.assertLessEqual(len(checked), 1, "16 bytes were read on a 10-byte budget")


class RecoveryTruncation(RecoveryBase):
    def test_a_backup_that_cannot_be_examined_is_still_shown(self):
        """R5-jobs-021: a listed backup whose stat fails is a row (unsafe) or counted as omitted,
        never silently dropped."""
        self.put("hidden-x.bak", where=self.d / "recovery")
        real_lstat, real_stat = os.lstat, os.stat

        def lstat(p, *a, **k):
            if os.fsdecode(p).endswith("hidden-x.bak"):
                raise PermissionError("no")
            return real_lstat(p, *a, **k)

        def stat_(p, *a, follow_symlinks=True, **k):
            if not follow_symlinks and os.fsdecode(p).endswith("hidden-x.bak"):
                raise PermissionError("no")
            return real_stat(p, *a, follow_symlinks=follow_symlinks, **k)

        with patch.object(os, "lstat", lstat), patch.object(os, "stat", stat_):
            out = rec.diagnose(self.home)
        self.assertEqual(len(out["backups"]) + out["backups_omitted"], out["backups_total"])
        self.assertEqual(out["backups"][0]["status"], "unsafe")


class RecoveryFailures(RecoveryBase):
    def test_an_unreadable_store_after_a_failed_restore_is_not_called_unchanged(self):
        """R5-jobs-022: when the store cannot be read after a failed install, store_unchanged is
        null, not true."""
        backup = self.put("hidden-ok.bak", where=self.d / "recovery").name
        real_read, calls = rec._read_store, []

        def read_store(*a, **k):
            calls.append(1)
            return real_read(*a, **k) if len(calls) == 1 else (None, "unsafe")

        def replace(*a, **k):
            raise OSError("disk full")

        with patch.object(rec, "_read_store", read_store), patch.object(os, "replace", replace):
            out = rec.restore("hidden", backup, self.home, apply=True)
        self.assertEqual(out["status"], "failed")
        self.assertIsNone(out["store_unchanged"])

    def test_a_backup_made_before_a_failed_directory_sync_is_reported(self):
        """R5-jobs-023: a backup file created before the directory sync failed is named, by preserve
        and by restore."""
        self.put("hidden.json", b'{"a": "current"}')
        (self.d / "recovery").mkdir(mode=0o700)
        backup = self.put("hidden-ok.bak", where=self.d / "recovery").name

        def fail(*a, **k):
            raise OSError("sync failed")

        for run in (lambda: rec.preserve("hidden", self.home), lambda: rec.restore("hidden", backup, self.home, apply=True)):
            before = set(os.listdir(self.d / "recovery"))
            with patch.object(rec, "_fsync_dir", fail):
                out = run()
            made = set(os.listdir(self.d / "recovery")) - before
            with self.subTest(out=out.get("status")):
                self.assertEqual(len(made), 1)
                named = out.get("preserved_current") or out.get("backup") or ""
                self.assertEqual(Path(named).name, made.pop())


class RecoveryDocs(RecoveryBase):
    def test_the_module_says_preserve_tightens_the_recovery_directory(self):
        """R5-jobs-025: preserve sets an existing recovery directory to 0700, and the module says so."""
        self.put("hidden.json")
        (self.d / "recovery").mkdir(mode=0o755)
        os.chmod(self.d / "recovery", 0o755)
        rec.preserve("hidden", self.home)
        self.assertEqual(os.stat(self.d / "recovery").st_mode & 0o777, 0o700)
        self.assertNotIn("only adds a backup file", " ".join(rec.__doc__.split()))
        self.assertIn("sets that directory to 0700", " ".join(rec.__doc__.split()))

    def test_restore_documents_an_unknown_store_state_after_a_failure(self):
        """R5-jobs-026: a failure after the swap does not imply store_unchanged is false: it can be
        true, false or null, and the docstring says so."""
        doc = " ".join(rec.restore.__doc__.split())
        self.assertIn("null when it could not be read", doc)
        self.assertNotIn("false after the swap", doc)


try:
    import fastapi
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    fastapi = None


@unittest.skipIf(fastapi is None, "fastapi not installed")
class Api(unittest.TestCase):
    def load(self):
        api = _load(f"ub_api_r5j_{id(self)}", "dashboard/plugin_api.py")
        timer = api._ASK_JOBS._timer
        if timer is not None:
            self.addCleanup(timer.cancel)
        return api

    def test_importing_the_api_starts_no_cleanup_timer(self):
        """R5-api-003: a module import arms no housekeeping timer (one would resolve the profile of
        whatever process imported it, long after a test fixture is gone)."""
        self.assertIsNone(self.load()._ASK_JOBS._timer)

    def test_a_question_is_required(self):
        """R5-api-006: an Ask without messages is refused by the request model, default included."""
        api = self.load()
        with self.assertRaises(Exception):
            api.CardAsk(board="default", task_id="t")

    def test_a_concurrent_twin_for_another_card_is_refused(self):
        """R5-api-007: when the id lookup raced (None) and creation then finds the id taken, the
        twin is checked against its card like a retry, not handed another card's job."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        api = self.load()
        needs = types.SimpleNamespace(
            ask_enabled=lambda: True, card_context=lambda b, t: {"id": t}, build_ask_messages=lambda c, h: list(h),
            ask_journal_dir=lambda: Path(tmp.name) / "ask", ask_persist_replies=lambda: False)
        api._needs = lambda: needs
        api._checked_card_ref = lambda i, b, t: ("factory", "other")
        api._loaded_ctx = lambda: types.SimpleNamespace(llm=types.SimpleNamespace(
            complete=lambda m, **kw: types.SimpleNamespace(text="x", provider="p", model="m")))
        api._ASK_JOBS = api.jobs.AskJobs(journal=api.jobs.AskJournal(lambda: api._ask_journal_dir(), lambda: False))
        api._ASK_JOBS.create(J1, card="kanban:factory:t_1")
        real_get, calls = api._ASK_JOBS.get, []

        def racing_get(job):
            calls.append(job)
            return None if len(calls) == 1 else real_get(job)

        api._ASK_JOBS.get = racing_get
        app = fastapi.FastAPI()
        app.include_router(api.router)
        r = TestClient(app).post("/card/ask", json={"id": "y", "request_id": J1, "messages": [{"role": "user", "content": "q"}]})
        self.assertEqual(r.status_code, 409)


if __name__ == "__main__":
    unittest.main()
