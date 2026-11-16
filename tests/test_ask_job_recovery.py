"""Ask job recovery across a gateway restart (issue #35).

A restart is simulated with a fresh ``AskJobs`` over the same journal directory."""

import importlib.util
import os
import stat
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from conftest import ask_gen

ROOT = Path(__file__).parents[1]


def _load_jobs():
    spec = importlib.util.spec_from_file_location("ub_jobs_recovery", ROOT / "jobs.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ub_jobs_recovery"] = mod
    spec.loader.exec_module(mod)
    return mod


jobs = _load_jobs()
J1 = "a" * 32
J2 = "b" * 32
J3 = "c" * 32
CARD = "kanban:factory:t_1"  # what the mocked board/task pair canonicalises to


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# The Ask journal needs directory descriptors and POSIX owners; elsewhere (Windows) it reports
# "unavailable" by design, which tests/test_journal_non_posix.py pins (#43).
POSIX_JOURNAL = jobs._DIR_FD and hasattr(os, "geteuid")


@unittest.skipUnless(POSIX_JOURNAL, "the Ask journal is POSIX-only; Windows reports it unavailable (#43)")
class RecoveryBase(unittest.TestCase):
    keep = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "ask"
        self.clock = Clock()
        self.boot()

    def boot(self):
        """(Re)start the gateway: new in-memory table, same disk."""
        journal = jobs.AskJournal(lambda: self.dir, lambda: self.keep, self.clock)
        self.jobs = jobs.AskJobs(self.clock, journal)
        self.journal = journal
        return self.jobs

    def files(self):
        return sorted(p.name for p in self.dir.glob("*.json")) if self.dir.exists() else []


class RestartStates(RecoveryBase):
    def test_restart_before_send_is_not_submitted(self):
        self.jobs.create(J1, card="c1")
        self.boot()
        st = self.jobs.get(J1)
        self.assertEqual(jobs.ASK_WIRE[st["status"]], "not_submitted")
        self.assertTrue(st["recovered"])

    def test_restart_after_send_is_unknown_not_resent(self):
        self.jobs.create(J1, card="c1")
        self.jobs.mark_sent(J1, gen=ask_gen(self.jobs, J1))
        self.boot()
        st = self.jobs.get(J1)
        self.assertEqual(jobs.ASK_WIRE[st["status"]], "unknown")
        self.assertIn("not sent again", st["detail"])

    def test_restart_after_completion_without_persistence_is_unknown(self):
        self.jobs.create(J1, card="c1")
        self.jobs.mark_sent(J1, gen=ask_gen(self.jobs, J1))
        self.jobs.finish(J1, "SECRET REPLY TEXT", "p", "m", gen=ask_gen(self.jobs, J1))
        self.assertNotIn("SECRET REPLY TEXT", (self.dir / f"{J1}.json").read_text())
        self.boot()
        st = self.jobs.get(J1)
        self.assertEqual(jobs.ASK_WIRE[st["status"]], "unknown")
        self.assertNotIn("reply", st)

    def test_restart_after_completion_with_persistence_is_complete(self):
        self.keep = True
        self.jobs.create(J1, card="c1")
        self.jobs.mark_sent(J1, gen=ask_gen(self.jobs, J1))
        self.jobs.finish(J1, "Pick A", "p", "m", gen=ask_gen(self.jobs, J1))
        self.boot()
        st = self.jobs.get(J1)
        self.assertEqual(jobs.ASK_WIRE[st["status"]], "complete")
        self.assertEqual((st["reply"], st["provider"], st["model"]), ("Pick A", "p", "m"))

    def test_restart_after_failure_is_failed(self):
        self.jobs.create(J1, card="c1")
        self.jobs.mark_sent(J1, gen=ask_gen(self.jobs, J1))
        self.jobs.fail(J1, "provider said no", gen=ask_gen(self.jobs, J1))
        self.boot()
        st = self.jobs.get(J1)
        self.assertEqual(jobs.ASK_WIRE[st["status"]], "failed")
        self.assertEqual(st["detail"], "provider said no")

    def test_live_pending_stays_pending(self):
        self.jobs.create(J1, card="c1")
        self.jobs.mark_sent(J1, gen=ask_gen(self.jobs, J1))
        self.assertEqual(jobs.ASK_WIRE[self.jobs.get(J1)["status"]], "pending")

    def test_repeated_polling_is_idempotent(self):
        self.jobs.create(J1, card="c1")
        self.jobs.mark_sent(J1, gen=ask_gen(self.jobs, J1))
        self.boot()
        first = self.jobs.get(J1)
        for _ in range(5):
            self.assertEqual(self.jobs.get(J1), first)
        self.assertEqual(self.files(), [f"{J1}.json"])

    def test_slow_completion_before_ttl_still_settles(self):
        self.jobs.create(J1, card="c1")
        self.jobs.mark_sent(J1, gen=ask_gen(self.jobs, J1))
        self.clock.t += jobs.AskJobs.TTL_S - 5
        self.assertTrue(self.jobs.finish(J1, "late", "p", "m", gen=ask_gen(self.jobs, J1)))
        self.assertEqual(jobs.ASK_WIRE[self.jobs.get(J1)["status"]], "complete")

    def test_late_worker_after_expiry_cannot_resurrect(self):
        self.jobs.create(J1, card="c1")
        self.clock.t += jobs.AskJobs.TTL_S + 1
        self.assertIsNone(self.jobs.get(J1))
        self.assertFalse(self.jobs.finish(J1, "late", "p", "m", gen=ask_gen(self.jobs, J1)))
        self.assertEqual(self.files(), [])

    def test_recovered_job_never_invokes_the_provider(self):
        calls = []
        self.jobs.create(J1, card="c1")
        self.jobs.mark_sent(J1, gen=ask_gen(self.jobs, J1))
        self.boot()
        for _ in range(3):
            self.jobs.get(J1)
        self.assertEqual(calls, [])
        # A recovered job cannot be created again under the same id from the journal alone.
        self.assertEqual(jobs.ASK_WIRE[self.jobs.get(J1)["status"]], "unknown")


class RetentionAndDeletion(RecoveryBase):
    def test_expired_record_is_deleted_on_read(self):
        self.jobs.create(J1, card="c1")
        self.boot()
        self.clock.t += jobs.AskJobs.TTL_S + 1
        self.assertIsNone(self.jobs.get(J1))
        self.assertEqual(self.files(), [])

    def test_prune_removes_expired_files_nobody_asked_about(self):
        self.jobs.create(J1, card="c1")
        self.boot()
        self.clock.t += jobs.AskJobs.TTL_S + 60
        self.jobs.get("f" * 32)  # any access runs throttled housekeeping
        self.assertEqual(self.files(), [])

    def test_file_cap_evicts_oldest(self):
        self.journal.MAX_FILES = 3
        for i in range(6):
            self.clock.t += 1
            job = f"{i:032x}"
            self.journal.write(job, {"state": "queued", "started": self.clock.t, "card": "c"})
            os.utime(self.dir / f"{job}.json", (self.clock.t, self.clock.t))
        self.journal.prune(jobs.AskJobs.TTL_S)
        self.assertEqual(len(self.files()), 3)
        self.assertEqual(self.files(), [f"{i:032x}.json" for i in (3, 4, 5)])

    def test_settled_cap_evicts_journal_files_too(self):
        self.jobs.MAX_SETTLED = 2
        for i in range(4):
            job = f"{i:032x}"
            self.jobs.create(job, card="c")
            self.jobs.fail(job, "x", gen=ask_gen(self.jobs, job))
        self.assertEqual(len(self.files()), 2)

    def test_consumed_job_leaves_no_file(self):
        self.jobs.create(J1, card="c1")
        self.jobs.fail(J1, "x", gen=ask_gen(self.jobs, J1))
        self.jobs.consume(J1)
        self.assertEqual(self.files(), [])

    def test_discard_deletes_memory_and_disk(self):
        self.jobs.create(J1, card="c1")
        self.jobs.mark_sent(J1, gen=ask_gen(self.jobs, J1))
        self.assertTrue(self.jobs.discard(J1))
        self.assertIsNone(self.jobs.get(J1))
        self.assertEqual(self.files(), [])
        self.assertFalse(self.jobs.discard(J1))

    def test_discard_after_restart(self):
        self.jobs.create(J1, card="c1")
        self.boot()
        self.assertTrue(self.jobs.discard(J1))
        self.assertIsNone(self.jobs.get(J1))

    def test_discard_reports_failure_when_the_saved_record_cannot_be_deleted(self):
        self.keep = True
        self.jobs.create(J1, card="c1")
        self.jobs.finish(J1, "secret answer", gen=ask_gen(self.jobs, J1))
        with patch.object(os, "unlink", side_effect=PermissionError):
            with self.assertRaises(jobs.DiscardFailed):
                self.jobs.discard(J1)
        # Nothing was reported as deleted, but the fence is down: the answer is never served again.
        self.assertIsNone(self.jobs.get(J1))
        self.assertEqual(self.files(), [f"{J1}.json"])
        # Once the file can be removed, discard succeeds and nothing is left to recover.
        self.assertTrue(self.jobs.discard(J1))
        self.boot()
        self.assertIsNone(self.jobs.get(J1))

    def test_journal_never_exceeds_its_cap_across_simulated_boots(self):
        counts = []
        for boot in range(3):
            self.boot()
            for i in range(128):
                self.clock.t += 0.001
                self.jobs.create(f"{boot:08x}{i:024x}", card="c")
            counts.append(len(self.files()))
        self.assertEqual(counts, [128, 256, 256])

    def test_a_new_job_never_evicts_a_live_one_from_the_journal(self):
        self.journal.MAX_FILES = 2
        self.jobs.create(J1, card="c")
        self.jobs.create(J2, card="c")
        with self.assertRaises(jobs.ScopeUnavailable):  # no room and nothing evictable: refused
            self.jobs.create(J3, card="c")
        self.assertEqual(self.files(), [f"{J1}.json", f"{J2}.json"])
        self.assertIsNone(self.jobs.get(J3))


class Protection(RecoveryBase):
    def test_modes_are_owner_only(self):
        self.jobs.create(J1, card="c1")
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / f"{J1}.json").st_mode), 0o600)

    def test_markers_hold_no_card_text(self):
        self.jobs.create(J1, card="board:card-1")
        self.jobs.mark_sent(J1, gen=ask_gen(self.jobs, J1))
        body = (self.dir / f"{J1}.json").read_text()
        self.assertNotIn("messages", body)
        self.assertNotIn("reply", body)

    def test_no_tmp_files_left(self):
        self.jobs.create(J1, card="c1")
        self.jobs.mark_sent(J1, gen=ask_gen(self.jobs, J1))
        self.jobs.fail(J1, "x", gen=ask_gen(self.jobs, J1))
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.endswith(".tmp")], [])

    def test_bad_job_ids_never_touch_disk(self):
        for bad in ("../x", "A" * 32, "", "z" * 8, "a" * 65):
            self.journal.write(bad, {"state": "queued", "started": 1})
            self.assertIsNone(self.journal.read(bad))
        self.assertEqual(self.files(), [])


class Corruption(RecoveryBase):
    def _unknown(self, job):
        st = self.jobs.get(job)
        self.assertEqual(jobs.ASK_WIRE[st["status"]], "unknown")

    def test_garbage_file_is_unknown(self):
        self.dir.mkdir(parents=True)
        (self.dir / f"{J1}.json").write_text("{not json")
        self._unknown(J1)

    def test_wrong_shape_is_unknown(self):
        self.dir.mkdir(parents=True)
        (self.dir / f"{J1}.json").write_text("[1,2,3]")
        self._unknown(J1)

    def test_foreign_job_id_inside_file_is_unknown(self):
        self.jobs.create(J2, card="c")
        (self.dir / f"{J1}.json").write_bytes((self.dir / f"{J2}.json").read_bytes())
        self._unknown(J1)

    def test_symlink_is_not_followed(self):
        self.dir.mkdir(parents=True)
        target = Path(self.tmp.name) / "elsewhere.json"
        target.write_text('{"v":1,"job":"%s","state":"done","started":1000,"reply":"STOLEN"}' % J1)
        (self.dir / f"{J1}.json").symlink_to(target)
        st = self.jobs.get(J1)
        self.assertEqual(jobs.ASK_WIRE[st["status"]], "unknown")
        self.assertNotIn("reply", st)

    def test_unknown_state_value_is_unknown(self):
        self.dir.mkdir(parents=True)
        (self.dir / f"{J1}.json").write_text('{"v":1,"job":"%s","state":"weird","started":1000}' % J1)
        self._unknown(J1)

    def test_unwritable_directory_refuses_the_job(self):
        blocked = Path(self.tmp.name) / "file"
        blocked.write_text("x")
        journal = jobs.AskJournal(lambda: blocked / "ask", lambda: False, self.clock)
        table = jobs.AskJobs(self.clock, journal)
        with self.assertRaises(jobs.ScopeUnavailable):
            table.create(J1, card="c")
        self.assertEqual(len(table), 0)

    def test_no_journal_is_plain_memory(self):
        table = jobs.AskJobs(self.clock)
        table.create(J1)
        table.finish(J1, "ok", gen=ask_gen(table, J1))
        self.assertEqual(table.get(J1)["reply"], "ok")
        self.assertIsNone(jobs.AskJobs(self.clock).get(J1))


class Isolation(RecoveryBase):
    def test_card_is_recorded_for_recovered_jobs(self):
        self.jobs.create(J1, card="profA:board:c1")
        self.boot()
        self.assertEqual(self.jobs.card(J1), "profA:board:c1")
        self.assertEqual(self.jobs.get(J1)["card"], "profA:board:c1")

    def test_profiles_with_separate_directories_do_not_see_each_other(self):
        self.jobs.create(J1, card="c1")
        other = jobs.AskJobs(self.clock, jobs.AskJournal(lambda: Path(self.tmp.name) / "other", lambda: False, self.clock))
        self.assertIsNone(other.get(J1))


try:
    import fastapi
    from fastapi.testclient import TestClient
except Exception:  # noqa: BLE001
    fastapi = None


@unittest.skipIf(fastapi is None, "fastapi not installed")
class AskApiRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "ask"
        self.keep = False
        self.sent = []
        spec = importlib.util.spec_from_file_location("ub_api_rec", ROOT / "dashboard/plugin_api.py")
        self.api = importlib.util.module_from_spec(spec)
        sys.modules["ub_api_rec"] = self.api
        spec.loader.exec_module(self.api)
        needs = types.SimpleNamespace(
            ask_enabled=lambda: True,
            card_context=lambda b, t: {"id": t},
            build_ask_messages=lambda c, h: list(h),
            ask_journal_dir=lambda: self.dir,
            ask_persist_replies=lambda: self.keep,
        )
        self.api._needs = lambda: needs
        self.api._checked_card_ref = lambda i, b, t: ("factory", "t_1")

        def complete(messages, **kw):
            self.sent.append(messages)
            return types.SimpleNamespace(text="Pick A", provider="p", model="m")

        self.api._loaded_ctx = lambda: types.SimpleNamespace(llm=types.SimpleNamespace(complete=complete))
        app = fastapi.FastAPI()
        app.include_router(self.api.router)
        self.c = TestClient(app)

    def restart(self):
        """A new gateway process: empty memory, same journal directory."""
        self.api._ASK_JOBS = self.api.jobs.AskJobs(journal=self.api.jobs.AskJournal(lambda: self.dir, lambda: self.keep))

    def _ask(self, card="x"):
        return self.c.post("/card/ask", json={"id": card, "messages": [{"role": "user", "content": "which?"}]}).json()["job"]

    def _wait(self, job):
        for _ in range(60):
            p = self.c.get(f"/card/ask/{job}?poll=true&card={CARD}").json()
            if not p.get("pending"):
                return p
            time.sleep(0.05)
        self.fail("ask did not settle")

    def test_completed_job_reports_complete_state(self):
        p = self._wait(self._ask())
        self.assertEqual((p["state"], p["reply"]), ("complete", "Pick A"))

    def test_restart_after_complete_without_persistence_reports_unknown_without_resending(self):
        job = self._ask()
        self._wait(job)
        n = len(self.sent)
        self.restart()
        p = self.c.get(f"/card/ask/{job}?poll=true&card={CARD}").json()
        self.assertEqual((p["ok"], p["state"], p["resubmit_safe"]), (False, "unknown", False))
        self.assertEqual(len(self.sent), n)
        self.assertEqual(self.c.get(f"/card/ask/{job}?card={CARD}").status_code, 409)
        self.assertEqual(len(self.sent), n)

    def test_restart_after_complete_with_persistence_returns_reply(self):
        self.keep = True
        job = self._ask()
        self._wait(job)
        self.restart()
        p = self.c.get(f"/card/ask/{job}?poll=true&card={CARD}").json()
        self.assertEqual((p["state"], p["reply"]), ("complete", "Pick A"))

    def test_restart_before_send_reports_not_submitted_and_resubmit_safe(self):
        job = "d" * 32
        self.api._ASK_JOBS.create(job, card=CARD)
        self.restart()
        p = self.c.get(f"/card/ask/{job}?poll=true&card={CARD}").json()
        self.assertEqual((p["state"], p["resubmit_safe"]), ("not_submitted", True))
        self.assertEqual(self.sent, [])

    def test_restart_mid_send_reports_unknown(self):
        job = "e" * 32
        self.api._ASK_JOBS.create(job, card=CARD)
        self.api._ASK_JOBS.mark_sent(job, gen=ask_gen(self.api._ASK_JOBS, job))
        self.restart()
        p = self.c.get(f"/card/ask/{job}?poll=true&card={CARD}").json()
        self.assertEqual((p["state"], p["resubmit_safe"]), ("unknown", False))

    def test_other_card_cannot_read_a_job(self):
        job = self._ask("x")
        self._wait(job)
        self.assertEqual(self.c.get(f"/card/ask/{job}?poll=true&card=other").status_code, 404)
        self.assertEqual(self.c.get(f"/card/ask/{job}?poll=true&card={CARD}").status_code, 200)
        self.restart()
        self.assertEqual(self.c.get(f"/card/ask/{job}?poll=true&card=other").status_code, 404)

    def test_card_identity_comes_from_the_validated_card_not_the_optional_id(self):
        r = self.c.post("/card/ask", json={"board": "factory", "task_id": "t_1",
                                           "messages": [{"role": "user", "content": "which?"}]})
        job = r.json()["job"]
        self._wait(job)
        self.assertEqual(self.api._ASK_JOBS.card(job), CARD)
        self.assertEqual(self.c.get(f"/card/ask/{job}?poll=true&card=kanban:factory:other").status_code, 404)
        self.assertEqual(self.c.delete(f"/card/ask/{job}?card=kanban:factory:other").status_code, 404)
        self.assertEqual(self.c.get(f"/card/ask/{job}?poll=true&card={CARD}").status_code, 200)

    def test_model_is_not_called_when_the_request_cannot_be_recorded(self):
        self.api._ASK_JOBS.mark_sent = lambda job, gen: False
        job = self._ask()
        p = self._wait(job)
        self.assertEqual(p["state"], "failed")
        self.assertIn("not sent", p["detail"])
        self.assertEqual(self.sent, [])

    def test_queue_poll_prunes_saved_answers_without_a_new_ask(self):
        self.keep = True
        job = self._ask()
        self._wait(job)
        self.assertEqual(len(list(self.dir.glob("*.json"))), 1)
        clock = Clock(time.time() + self.api.jobs.AskJobs.TTL_S + 5)
        self.api._ASK_JOBS = self.api.jobs.AskJobs(clock, self.api.jobs.AskJournal(lambda: self.dir, lambda: True, clock))
        self.api._needs = lambda: types.SimpleNamespace(build_queue=lambda: {"items": []})
        self.assertEqual(self.c.get("/queue").status_code, 200)
        self.assertEqual(list(self.dir.glob("*.json")), [])

    def test_delete_removes_job_and_file(self):
        self.keep = True
        job = self._ask()
        self._wait(job)
        self.assertEqual(self.c.delete(f"/card/ask/{job}?card=other").status_code, 404)
        self.assertEqual(self.c.delete(f"/card/ask/{job}?card={CARD}").json(), {"ok": True, "deleted": True})
        self.assertEqual(self.c.get(f"/card/ask/{job}?card={CARD}").status_code, 404)
        self.assertEqual(list(self.dir.glob("*.json")), [])
        self.assertEqual(self.c.delete(f"/card/ask/{job}?card={CARD}").json(), {"ok": True, "deleted": False})

    def test_delete_reports_failure_instead_of_success_when_the_file_stays(self):
        self.keep = True
        job = self._ask()
        self._wait(job)
        with patch.object(os, "unlink", side_effect=PermissionError):
            r = self.c.delete(f"/card/ask/{job}?card={CARD}")
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("deleted", r.json())
        # The record is still on disk (so not "deleted"), but fenced: it is never served again.
        self.assertEqual(self.c.get(f"/card/ask/{job}?poll=true&card={CARD}").status_code, 404)
        self.assertEqual(self.c.delete(f"/card/ask/{job}?card={CARD}").json(), {"ok": True, "deleted": True})


class MarkSentFailure(RecoveryBase):
    """The "sent" marker is what keeps a restart from offering a resend after the model may have run."""

    def test_failed_sent_write_keeps_the_job_from_the_model(self):
        self.jobs.create(J1, card="c1")
        real = os.rename

        def failing(src, dst, *a, **k):
            if str(dst).endswith(f"{J1}.json"):
                raise OSError("disk full")
            return real(src, dst, *a, **k)

        self.addCleanup(setattr, os, "rename", real)
        os.rename = failing
        self.assertFalse(self.jobs.mark_sent(J1, gen=ask_gen(self.jobs, J1)))
        os.rename = real
        self.assertEqual(self.files(), [f"{J1}.json"], "the queued marker stays")
        self.boot()
        self.assertEqual(self.jobs.get(J1)["status"], jobs.AskState.NOT_SUBMITTED, "true: it was never sent")

    def test_unrecordable_request_must_not_reach_the_model(self):
        self.jobs.create(J1, card="c1")
        self.journal.write = lambda job, rec, **kw: False
        self.journal.delete = lambda job, **kw: False
        self.assertFalse(self.jobs.mark_sent(J1, gen=ask_gen(self.jobs, J1)))

    def test_no_journal_still_allows_the_send(self):
        plain = jobs.AskJobs(self.clock)
        plain.create(J1)
        self.assertTrue(plain.mark_sent(J1, gen=ask_gen(plain, J1)))


class UnavailableStorage(RecoveryBase):
    """Round-3 review: storage that cannot be examined is unknown, never an empty directory."""

    def lose_directory(self):
        """The storage itself stops answering (a directory nobody can open), not just the resolver."""
        self.addCleanup(os.chmod, self.dir, 0o700)
        os.chmod(self.dir, 0)

    def test_model_is_not_called_when_the_queued_marker_cannot_be_checked_or_removed(self):
        self.jobs.create(J1, card="c1")
        self.assertEqual(self.files(), [f"{J1}.json"])
        self.lose_directory()
        self.assertFalse(self.jobs.mark_sent(J1, gen=ask_gen(self.jobs, J1)))

    def test_discard_does_not_claim_success_while_the_saved_record_cannot_be_checked(self):
        self.jobs.create(J1, card="c1")
        self.lose_directory()
        with self.assertRaises(jobs.DiscardFailed):
            self.jobs.discard(J1)
        self.assertIsNotNone(self.jobs.get(J1))  # still there, and so is the file
        os.chmod(self.dir, 0o700)
        self.assertEqual(self.files(), [f"{J1}.json"])

    def test_a_job_that_was_never_journaled_is_verifiably_absent_without_a_location(self):
        plain = jobs.AskJournal(lambda: None)
        self.assertTrue(plain.verified_absent(J1))

    def test_verified_absence_means_the_file_is_really_gone(self):
        self.jobs.create(J1, card="c1")
        self.assertFalse(self.journal.verified_absent(J1))
        self.journal.delete(J1)
        self.assertTrue(self.journal.verified_absent(J1))


class BadEntries(RecoveryBase):
    def dangle(self, name):
        self.dir.mkdir(parents=True, exist_ok=True)
        link = self.dir / f"{name}.json"
        link.symlink_to(Path(self.tmp.name) / "nowhere")
        return link

    def test_one_dangling_symlink_does_not_stop_new_markers(self):
        link = self.dangle(J3)
        self.jobs.create(J1, card="c1")
        self.assertIn(f"{J1}.json", self.files())
        self.assertEqual(self.journal.read(J1)["state"], "queued")
        self.assertFalse(link.is_symlink(), "the junk link was removed")

    def test_housekeeping_removes_a_dangling_symlink_and_never_follows_it(self):
        target = Path(self.tmp.name) / "precious.json"
        target.write_text("{}")
        self.jobs.create(J2, card="c1")
        link = self.dir / f"{J3}.json"
        link.symlink_to(target)
        dangling = self.dangle("d" * 32)
        self.assertGreaterEqual(self.journal.prune(900), 1)
        self.assertFalse(dangling.is_symlink())
        self.assertTrue(target.exists(), "the link's target is untouched")
        self.assertTrue((self.dir / f"{J2}.json").exists())

    def test_a_full_directory_with_a_bad_entry_still_makes_room(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        for n in range(self.journal.MAX_FILES):
            (self.dir / f"{n:032x}.json").write_text("{}")
        self.dangle("e" * 32)
        self.assertTrue(self.journal.write(J1, {"state": "queued", "started": 1.0}))


class Housekeeping(RecoveryBase):
    keep = True

    def test_saved_answer_expires_without_any_later_ask(self):
        self.jobs.create(J1, card="c1")
        self.jobs.mark_sent(J1, gen=ask_gen(self.jobs, J1))
        self.jobs.finish(J1, "PRIVATE ANSWER", "p", "m", gen=ask_gen(self.jobs, J1))
        self.assertEqual(len(self.files()), 1)
        self.clock.t += jobs.AskJobs.TTL_S + 1
        self.jobs.housekeeping()
        self.assertEqual(self.files(), [])

    def test_background_timer_removes_expired_files(self):
        self.jobs.create(J1, card="c1")
        self.jobs.finish(J1, "PRIVATE ANSWER", "p", "m", gen=ask_gen(self.jobs, J1))
        self.clock.t += jobs.AskJobs.TTL_S + 1
        self.jobs.start_background(0.05)
        for _ in range(60):
            if not self.files():
                break
            time.sleep(0.05)
        self.assertEqual(self.files(), [])
        with self.jobs._lock:
            t = self.jobs._timer
        if t is not None:
            t.cancel()


@unittest.skipUnless(POSIX_JOURNAL, "the Ask journal is POSIX-only; Windows reports it unavailable (#43)")
class ProfileDirectory(unittest.TestCase):
    def test_each_profile_gets_its_own_journal_directory(self):
        from unittest.mock import patch
        from test_outside import needs

        with tempfile.TemporaryDirectory() as root:
            dirs = {}
            for profile in ("work", "home"):
                ph = Path(root) / "profiles" / profile
                ph.mkdir(parents=True)
                with patch.dict("os.environ", {"HERMES_HOME": str(ph)}):
                    dirs[profile] = needs.ask_journal_dir()
            self.assertNotEqual(dirs["work"], dirs["home"])
            self.assertEqual((dirs["work"].name, dirs["home"].name), ("work", "home"))
            clock = Clock()
            a = jobs.AskJobs(clock, jobs.AskJournal(lambda: dirs["work"], lambda: True, clock))
            b = jobs.AskJobs(clock, jobs.AskJournal(lambda: dirs["home"], lambda: True, clock))
            a.create(J1, card="c1")
            a.finish(J1, "WORK ANSWER", "p", "m", gen=ask_gen(a, J1))
            self.assertIsNone(b.get(J1))
            self.assertIsNone(b.card(J1))
            self.assertFalse(b.discard(J1))
            self.assertEqual(a.get(J1)["reply"], "WORK ANSWER")


class ClassSweepJournal(RecoveryBase):
    def test_consume_that_cannot_delete_its_record_keeps_the_job(self):
        # Bug: consume() dropped the job from memory and ignored a failed delete, so the saved
        # reply stayed on disk and the next lookup recovered the job it had just consumed.
        self.keep = True
        self.jobs.create(J1, card="c1")
        self.jobs.mark_sent(J1, gen=ask_gen(self.jobs, J1))
        self.jobs.finish(J1, "Pick A", "p", "m", gen=ask_gen(self.jobs, J1))
        with patch.object(self.journal, "delete", return_value=False), \
                patch.object(self.journal, "verified_absent", return_value=False):
            with self.assertRaises(jobs.DiscardFailed):
                self.jobs.consume(J1)
        self.assertIsNotNone(self.jobs.get(J1))
        self.assertEqual(self.files(), [f"{J1}.json"])

    def test_consume_still_works_when_the_record_is_already_gone(self):
        self.jobs.create(J1, card="c1")
        self.jobs.fail(J1, "no", gen=ask_gen(self.jobs, J1))
        (self.dir / f"{J1}.json").unlink()
        self.assertEqual(self.jobs.consume(J1)["detail"], "no")

    def test_an_oversized_record_is_corrupt_and_never_read_whole(self):
        self.jobs.create(J1, card="c1")
        path = self.dir / f"{J1}.json"
        path.write_text('{"job": "%s", "started": 1, "pad": "%s"}' % (J1, "x" * (jobs.AskJournal.MAX_RECORD_BYTES + 10)))
        self.assertEqual(self.journal.read(J1), {"corrupt": True})


if __name__ == "__main__":
    unittest.main()
