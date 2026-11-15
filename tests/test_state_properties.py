"""Hypothesis state machines for the two lifecycles a user can lose work in.

* Ask jobs (``jobs.AskJobs`` over a real ``AskJournal``): create, mark_sent, finish, fail,
  consume, discard and get, with worker generation tokens and gateway restarts on the same
  journal. A restart must never offer a resend for a request that may have reached the model.
* Outside items: local Done, "Not for me" (hide) and Undo (unhide, with and without a token).

Each rule is checked against a small model after every step. CI installs Hypothesis (pinned in
.github/workflows/validate.yml); example counts are kept low so this file runs in seconds.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from conftest import ask_gen

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


jobs = _load("ub_jobs_props", ROOT / "jobs.py")

FAST = settings(
    max_examples=200,
    database=None,  # no .hypothesis directory in the checkout
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# --- Ask jobs ----------------------------------------------------------------------------------

JOB_IDS = ("a" * 32, "b" * 32, "c" * 32)
TICK_MAX = 20  # 25 steps x 20 s stays under the 900 s TTL; expiry has its own rule


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class AskJobsProperties(RuleBasedStateMachine):
    """Model per job: ``mem`` (in-memory phase), ``disk`` (journal state), ``tomb`` (deleted id)
    and ``unsure`` (after expiry, where the tombstone's lifetime is not modelled)."""

    def __init__(self):
        super().__init__()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "ask"
        self.clock = Clock()
        self.keep = False
        self.model = {
            j: {"mem": None, "disk": None, "tomb": False, "unsure": False} for j in JOB_IDS
        }
        self.gens = {}  # job -> generation of the live in-memory entry
        self.stale = []  # (job, gen) pairs whose entry is gone: a late worker
        self.sent_ever = set()  # jobs whose current record was marked sent at least once
        self.boot()

    def boot(self):
        self.table = jobs.AskJobs(
            self.clock, jobs.AskJournal(lambda: self.dir, lambda: self.keep, self.clock)
        )

    def teardown(self):
        self.tmp.cleanup()

    @initialize(keep=st.booleans())
    def choose_persistence(self, keep):
        self.keep = keep

    def _retire(self, job):
        gen = self.gens.pop(job, None)
        if gen is not None:
            self.stale.append((job, gen))

    # rules

    @rule(job=st.sampled_from(JOB_IDS))
    def create(self, job):
        m = self.model[job]
        try:
            gen = self.table.create(job, card="c1")
        except jobs.IllegalTransition:
            assert m["unsure"] or m["mem"] or m["disk"] or m["tomb"], m
            return
        assert m["unsure"] or not (m["mem"] or m["disk"] or m["tomb"]), m
        assert gen == ask_gen(self.table, job)
        assert all(g != gen for _, g in self.stale)  # a new entry never reuses a generation
        m.update(mem="queued", disk="queued", tomb=False, unsure=False)
        self.gens[job] = gen
        self.sent_ever.discard(job)

    @rule(job=st.sampled_from(JOB_IDS))
    def mark_sent(self, job):
        m = self.model[job]
        gen = self.gens.get(job)
        if gen is None:
            return
        ok = self.table.mark_sent(job, gen=gen)
        assert ok == (m["mem"] in ("queued", "sent")), (ok, m)
        if ok:
            m.update(mem="sent", disk="sent")
            self.sent_ever.add(job)

    @rule(job=st.sampled_from(JOB_IDS), done=st.booleans())
    def settle(self, job, done):
        m = self.model[job]
        gen = self.gens.get(job)
        if gen is None:
            return
        call = (
            (lambda: self.table.finish(job, "Pick A", "p", "m", gen=gen))
            if done
            else (lambda: self.table.fail(job, "model call failed", gen=gen))
        )
        if m["mem"] in ("done", "error"):
            try:
                call()
            except jobs.IllegalTransition:
                return
            raise AssertionError("a settled job was settled again")
        assert call() is True
        phase = "done" if done else "error"
        m.update(mem=phase, disk=phase)

    @precondition(lambda self: self.stale)
    @rule(data=st.data(), done=st.booleans())
    def late_worker(self, data, done):
        """A worker from an expired, consumed, discarded or pre-restart entry is a no-op."""
        job, gen = data.draw(st.sampled_from(self.stale))
        before = self.table.get(job)
        assert self.table.mark_sent(job, gen=gen) is False
        if done:
            assert self.table.finish(job, "late", gen=gen) is False
        else:
            assert self.table.fail(job, "late", gen=gen) is False
        after = self.table.get(job)
        assert (before or {}).get("status") == (after or {}).get("status")

    @rule(job=st.sampled_from(JOB_IDS))
    def consume(self, job):
        m = self.model[job]
        if m["mem"] is None:
            try:
                self.table.consume(job)
            except KeyError:
                return
            raise AssertionError("consume returned a job that is not in memory")
        if m["mem"] in ("queued", "sent"):
            try:
                self.table.consume(job)
            except jobs.IllegalTransition:
                return
            raise AssertionError("consume returned a pending job")
        got = self.table.consume(job)
        assert got["status"].value == m["mem"]
        if m["mem"] == "done":
            assert got["reply"] == "Pick A"
        m.update(mem=None, disk=None)
        self._retire(job)

    @rule(job=st.sampled_from(JOB_IDS))
    def discard(self, job):
        m = self.model[job]
        gone = self.table.discard(job)
        if not m["unsure"]:
            assert gone == bool(m["mem"] or m["disk"]), (gone, m)
        if not m["unsure"]:
            # Only a removed record is fenced: discarding an id that holds nothing writes nothing.
            m.update(mem=None, disk=None, tomb=m["tomb"] or bool(m["disk"]))
        self._retire(job)

    @rule()
    def restart(self):
        """The gateway restarts: a new table and journal object over the same directory."""
        self.boot()
        for job, m in self.model.items():
            m["mem"] = None
            self._retire(job)

    @rule(s=st.integers(min_value=1, max_value=TICK_MAX))
    def tick(self, s):
        self.clock.t += s

    @rule()
    def expire(self):
        self.clock.t += jobs.AskJobs.TTL_S + 1
        for job, m in self.model.items():
            m.update(mem=None, disk=None, tomb=False, unsure=True)
            self._retire(job)
        self.sent_ever.clear()

    # invariants

    @invariant()
    def get_matches_model(self):
        for job, m in self.model.items():
            got = self.table.get(job)
            if m["unsure"] or (m["mem"] is None and m["disk"] is None):
                assert got is None, (job, m, got)
                continue
            status = got["status"]
            if m["mem"] is not None:
                want = {
                    "queued": jobs.AskState.PENDING,
                    "sent": jobs.AskState.PENDING,
                    "done": jobs.AskState.DONE,
                    "error": jobs.AskState.ERROR,
                }[m["mem"]]
                assert status is want, (job, m, got)
                continue
            # Recovered from the journal after a restart.
            assert got["recovered"] is True
            assert status is not jobs.AskState.PENDING  # no live owner in this process
            want = {
                "queued": jobs.AskState.NOT_SUBMITTED,
                "sent": jobs.AskState.UNKNOWN,
                "error": jobs.AskState.ERROR,
                "done": jobs.AskState.DONE if self.keep else jobs.AskState.UNKNOWN,
            }[m["disk"]]
            assert status is want, (job, m, got)
            if job in self.sent_ever:
                # The request may have reached the model: never offered as safe to resend.
                assert status is not jobs.AskState.NOT_SUBMITTED, (job, got)
            assert jobs.ASK_WIRE[status] != "resend"


AskJobsProperties.TestCase.settings = FAST
TestAskJobsProperties = AskJobsProperties.TestCase


# --- outside Done / Not for me / Undo ----------------------------------------------------------

from test_outside import needs  # noqa: E402  (the shared plugin load the outside tests use)

FILED = ("A", "B")
FOLDER = "folder note"
ITEMS = FILED + (FOLDER,)


class OutsideDismissalProperties(RuleBasedStateMachine):
    """Model per item: the token of its live Done (filed items only) and of its live hide.
    A card is in the queue exactly when it holds neither."""

    def __init__(self):
        super().__init__()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        folder = self.home / "exports"
        folder.mkdir()
        (folder / "n.txt").write_text(FOLDER)
        self.settings = {"outside_dirs": str(folder)}
        self.patches = [
            patch.object(needs, "hermes_home", lambda: self.home),
            patch.object(needs, "_settings", lambda: self.settings),
            patch.object(needs, "kanban_items", lambda **kw: []),
            patch.object(needs, "chat_items", lambda *a, **kw: []),
            patch.object(needs, "wait_items", lambda *a, **kw: []),
            patch.object(needs, "pairing_items", lambda *a, **kw: []),
            patch.dict(os.environ, {"UNBL0CK_OUTSIDE_DIRS": ""}),
        ]
        for p in self.patches:
            p.start()
        for i, title in enumerate(FILED):
            needs.file_outside(
                {"kind": "draft", "title": title, "body": "Ready.", "act_where": "Outlook",
                 "dedupe_key": f"k{i}"},
                profile="work", session_id="s1",
            )
        self.items = {it["title"]: it for it in needs.build_queue()["items"]}
        assert set(self.items) == set(ITEMS), set(self.items)
        self.done = {t: None for t in ITEMS}
        self.hidden = {t: None for t in ITEMS}
        self.tokens = []  # every token ever returned, live or consumed

    def teardown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    @rule(title=st.sampled_from(ITEMS))
    def mark_done(self, title):
        it = self.items[title]
        reply = needs.done_outside(it["id"], it["version"])
        assert reply["ok"] and reply["local_only"]
        if title == FOLDER:  # a folder Done is a hide at the version read
            self.hidden[title] = reply["token"]
        else:
            if self.done[title] is not None:
                assert reply["token"] == self.done[title]  # Done twice is the same Done
            self.done[title] = reply["token"]
        self.tokens.append(reply["token"])

    @rule(title=st.sampled_from(ITEMS))
    def not_for_me(self, title):
        it = self.items[title]
        token = needs.hide(it["id"], it["version"])
        assert token not in self.tokens  # every dismissal is named uniquely
        self.hidden[title] = token
        self.tokens.append(token)

    @rule(title=st.sampled_from(ITEMS))
    def undo_all(self, title):
        want = self.done[title] is not None or self.hidden[title] is not None
        assert needs.unhide(self.items[title]["id"]) is want
        self.done[title] = self.hidden[title] = None

    @precondition(lambda self: self.tokens)
    @rule(title=st.sampled_from(ITEMS), data=st.data())
    def undo_token(self, title, data):
        token = data.draw(st.sampled_from(self.tokens))
        want = token in (self.done[title], self.hidden[title])
        assert needs.unhide(self.items[title]["id"], token=token) is want
        if want:  # one matching token clears both dismissals of the card
            self.done[title] = self.hidden[title] = None

    @invariant()
    def queue_matches_model(self):
        shown = {it["title"] for it in needs.build_queue()["items"]}
        want = {t for t in ITEMS if self.done[t] is None and self.hidden[t] is None}
        assert shown == want, (shown, want, self.done, self.hidden)
        stored = needs.load_hidden()
        for t in ITEMS:
            assert (self.items[t]["id"] in stored) == (self.hidden[t] is not None), t


OutsideDismissalProperties.TestCase.settings = settings(FAST, max_examples=60, stateful_step_count=20)
TestOutsideDismissalProperties = OutsideDismissalProperties.TestCase
