"""The two background-job lifecycles behind the dashboard routes, named in one place.

Pure stdlib (no FastAPI, no Hermes) so the transitions are unit-tested everywhere CI runs.
The summary table lives in memory only. The Ask table keeps a small restart journal
(see ``AskJournal``): status markers always, reply text only when the caller opts in.

Summary (BLUF) job, one per item version::

    (absent) --start--> PENDING --succeed--> (absent; the summary is in bluf.json)
                           |
                           +----fail----> FAILED --start (after RETRY_S)--> PENDING
    PENDING --start (only after STUCK_S: the worker never reported)--> PENDING

    A PENDING job is never started a second time just because it has waited a while: the
    pool may still be working through a backlog. FAILED waits RETRY_S before a retry.

Ask job, one per question::

    (absent) --create--> PENDING --finish--> DONE  --consume--> (absent)
                            |                                  ^
                            +------fail----> ERROR --consume---+
    any state --expire (older than TTL_S)--> (absent)
    settled results remain repeatably readable, subject to MAX_SETTLED retention.

    Restart recovery (journal): a job is journaled "queued" at create, "sent" just before
    the model call, and "done"/"error" when it settles. After a restart a journaled job
    that is absent from memory reads as:
        queued           -> NOT_SUBMITTED  (never reached the model; resubmitting is safe)
        sent             -> UNKNOWN        (the provider outcome is uncertain; never resent)
        done + reply     -> DONE           (only when reply persistence is enabled)
        done, no reply   -> UNKNOWN        (finished, but the text was not kept)
        error            -> ERROR
        unreadable file  -> UNKNOWN
    The plugin never resubmits a recovered job on its own.

    consume is refused while PENDING; finish/fail are refused unless PENDING (a late
    worker for an expired job is a no-op, never a resurrection).
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional


class IllegalTransition(RuntimeError):
    """A job was asked to move between two states that are not connected."""


class DiscardFailed(RuntimeError):
    """A saved Ask record could not be deleted, so the job still exists."""


# --- Summary (BLUF) jobs ---------------------------------------------------------------------

class BlufState(str, Enum):
    PENDING = "pending"   # queued on the pool or running
    FAILED = "failed"     # the last run raised; the card shows its raw text until a retry


class BlufJobs:
    """``{(item_id, version): {"state", "at", "error"}}`` with one function per move."""

    RETRY_S = 600      # the first retry waits this long; each later one waits twice as long
    MAX_ATTEMPTS = 3   # after this many failures a card version is not summarized again
    STUCK_S = 3600     # a pending job this old never reported back (the model call times out at 90 s)
    PRUNE_OVER = 500   # table size at which settled entries are dropped

    def __init__(self, clock: Optional[Callable[[], float]] = None):
        self._clock = clock or (lambda: time.time())
        self._lock = threading.Lock()
        self._jobs: dict[tuple[str, str], dict[str, Any]] = {}

    def state(self, key: tuple[str, str]) -> Optional[BlufState]:
        st = self._jobs.get(key)
        return None if st is None else st["state"]

    def can_start(self, key: tuple[str, str]) -> bool:
        st = self._jobs.get(key)
        if st is None:
            return True
        age = self._clock() - st["at"]
        if st["state"] is BlufState.FAILED:
            n = st.get("attempts", 1)
            if n >= self.MAX_ATTEMPTS:
                return False
            return age >= self.RETRY_S * (2 ** (n - 1))
        return age >= self.STUCK_S

    def start(self, key: tuple[str, str]) -> bool:
        """absent | FAILED (cooled down) | PENDING (stuck) -> PENDING. False when refused."""
        with self._lock:
            if not self.can_start(key):
                return False
            prev = self._jobs.get(key)
            attempts = prev.get("attempts", 0) if prev and prev["state"] is BlufState.FAILED else 0
            self._prune()
            self._jobs[key] = {"state": BlufState.PENDING, "at": self._clock(), "error": None, "attempts": attempts}
            return True

    def succeed(self, key: tuple[str, str]) -> None:
        """PENDING -> absent. A job already dropped (pruned) stays absent."""
        with self._lock:
            st = self._jobs.get(key)
            if st is not None and st["state"] is not BlufState.PENDING:
                raise IllegalTransition(f"summary {key} is {st['state'].value}, not pending")
            self._jobs.pop(key, None)

    def fail(self, key: tuple[str, str], error: str) -> None:
        """PENDING (or pruned) -> FAILED."""
        with self._lock:
            st = self._jobs.get(key)
            if st is not None and st["state"] is not BlufState.PENDING:
                raise IllegalTransition(f"summary {key} is {st['state'].value}, not pending")
            attempts = (st.get("attempts", 0) if st is not None else 0) + 1
            self._jobs[key] = {"state": BlufState.FAILED, "at": self._clock(), "error": str(error), "attempts": attempts}

    def attempts(self, key: tuple[str, str]) -> int:
        st = self._jobs.get(key)
        return 0 if st is None else int(st.get("attempts", 0))

    def is_pending(self, key: tuple[str, str]) -> bool:
        return self.state(key) is BlufState.PENDING

    def _prune(self) -> None:
        """Drop entries that no longer hold anything back: cooled-down failures and stuck jobs."""
        if len(self._jobs) <= self.PRUNE_OVER:
            return
        for k in [k for k in list(self._jobs) if self.can_start(k)]:
            self._jobs.pop(k, None)

    def __len__(self) -> int:
        return len(self._jobs)


# --- Ask jobs --------------------------------------------------------------------------------

class AskState(str, Enum):
    PENDING = "pending"
    DONE = "done"
    ERROR = "error"
    # Only ever produced from the restart journal, never by a live transition.
    UNKNOWN = "unknown"
    NOT_SUBMITTED = "not_submitted"


ASK_SETTLED = frozenset({AskState.DONE, AskState.ERROR})

# Wire names for the API and UI: the five outcomes a client can tell apart.
ASK_WIRE = {
    AskState.PENDING: "pending",
    AskState.DONE: "complete",
    AskState.ERROR: "failed",
    AskState.UNKNOWN: "unknown",
    AskState.NOT_SUBMITTED: "not_submitted",
}


class AskJournal:
    """Owner-only, bounded, per-job restart markers for Ask under one directory.

    One small JSON file per job (0600 in a 0700 directory), written atomically. It holds
    the job id, state, timestamps, the card id (isolation), provider/model, an error
    detail and, only when ``keep_replies()`` is true, the reply text. Files expire with the
    job TTL and are capped. Any I/O failure degrades to in-memory behaviour: ``failures``
    counts them and Ask itself never breaks because of the journal."""

    MAX_FILES = 256

    def __init__(
        self,
        directory: Callable[[], Optional[Path]],
        keep_replies: Callable[[], bool] = lambda: False,
        clock: Optional[Callable[[], float]] = None,
    ):
        self._directory = directory
        self._keep_replies = keep_replies
        self._clock = clock or (lambda: time.time())
        self.failures = 0
        self.protect: Callable[[str], bool] = lambda job: False  # set by AskJobs: live jobs are never evicted

    def _dir(self, create: bool = False) -> Optional[Path]:
        try:
            d = self._directory()
            if d is None:
                return None
            if create:
                d.mkdir(mode=0o700, parents=True, exist_ok=True)
            return d
        except OSError:
            self.failures += 1
            return None

    @staticmethod
    def _valid(job: str) -> bool:
        return bool(job) and len(job) <= 64 and all(c in "0123456789abcdef" for c in job)

    def keeps_replies(self) -> bool:
        try:
            return bool(self._keep_replies())
        except Exception:  # noqa: BLE001 - a settings fault must not break Ask
            return False

    def _make_room(self, d: Path, keep: Callable[[str], bool]) -> bool:
        """Evict the oldest unprotected files so one more record fits under MAX_FILES."""
        try:
            files = sorted((f.stat().st_mtime, f) for f in d.iterdir() if f.suffix == ".json")
            while len(files) >= self.MAX_FILES:
                victim = next((i for i, (_, f) in enumerate(files) if not keep(f.stem)), None)
                if victim is None:
                    return False
                files.pop(victim)[1].unlink(missing_ok=True)
            return True
        except OSError:
            self.failures += 1
            return False

    def write(self, job: str, rec: dict[str, Any]) -> bool:
        """True when the record is on disk. False when it is not (or no journal is available).

        A new file first evicts the oldest unprotected ones (``protect`` names jobs alive in
        memory), so the directory never exceeds MAX_FILES however many processes have written
        to it; if nothing can be evicted the record stays in memory only."""
        d = self._dir(create=True)
        if d is None or not self._valid(job):
            return False
        if not (d / f"{job}.json").exists() and not self._make_room(d, self.protect):
            return False
        tmp = d / f".{job}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"v": 1, "job": job, **rec}, fh, ensure_ascii=False)
            os.replace(tmp, d / f"{job}.json")
            return True
        except OSError:
            self.failures += 1
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def read(self, job: str) -> Optional[dict[str, Any]]:
        """The record, ``{"corrupt": True}`` when unreadable, or None when absent."""
        d = self._dir()
        if d is None or not self._valid(job):
            return None
        f = d / f"{job}.json"
        try:
            st = os.lstat(f)
        except FileNotFoundError:
            return None
        except OSError:
            self.failures += 1
            return {"corrupt": True}
        if not stat.S_ISREG(st.st_mode):
            return {"corrupt": True}
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"corrupt": True}
        if not isinstance(data, dict) or data.get("job") != job or not isinstance(data.get("started"), (int, float)):
            return {"corrupt": True}
        return data

    def delete(self, job: str) -> bool:
        d = self._dir()
        if d is None or not self._valid(job):
            return False
        try:
            (d / f"{job}.json").unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            self.failures += 1
            return False

    def has_files(self) -> bool:
        d = self._dir()
        try:
            return d is not None and any(f.suffix == ".json" or f.name.endswith(".tmp") for f in d.iterdir())
        except OSError:
            return False

    def prune(self, ttl: float, keep: Callable[[str], bool] = lambda job: False) -> int:
        """Delete expired and unreadable files, then the oldest beyond MAX_FILES.

        ``keep(job)`` protects jobs that are alive in memory. Returns how many were removed."""
        d = self._dir()
        if d is None:
            return 0
        now = self._clock()
        removed = 0
        live: list[tuple[float, Path]] = []
        try:
            files = [f for f in d.iterdir() if f.suffix == ".json" or f.name.endswith(".tmp")]
        except OSError:
            self.failures += 1
            return 0
        for f in files:
            try:
                if f.name.endswith(".tmp"):
                    if now - f.stat().st_mtime > ttl:
                        f.unlink(missing_ok=True)
                        removed += 1
                    continue
                rec = self.read(f.stem)
                started = rec.get("started") if rec and not rec.get("corrupt") else None
                mtime = f.stat().st_mtime
                age_from = started if isinstance(started, (int, float)) else mtime
                if keep(f.stem):
                    live.append((mtime, f))
                elif now - age_from > ttl or started is None and now - mtime > ttl:
                    f.unlink(missing_ok=True)
                    removed += 1
                else:
                    live.append((mtime, f))
            except OSError:
                self.failures += 1
        live.sort()
        for _, f in live[: max(0, len(live) - self.MAX_FILES)]:
            if not keep(f.stem):
                try:
                    f.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    self.failures += 1
        return removed


class AskJobs:
    """``{job: {"status", "started", ...}}``. Every write goes through one named transition."""

    TTL_S = 900
    MAX_SETTLED = 128
    PRUNE_EVERY_S = 30  # journal housekeeping cadence; polls are every 1.5 s

    def __init__(self, clock: Optional[Callable[[], float]] = None, journal: Optional[AskJournal] = None):
        self._clock = clock or (lambda: time.time())
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._journal = journal
        if journal is not None:
            journal.protect = lambda j: j in self._jobs
        self._last_prune = float("-inf")
        self._bg_every: Optional[float] = None
        self._timer: Optional[threading.Timer] = None

    def get(self, job: str) -> Optional[dict[str, Any]]:
        with self._lock:
            self._expire()
            st = self._jobs.get(job)
            if st is not None:
                return dict(st)
            return self._recover(job)

    def card(self, job: str) -> Optional[str]:
        """The card id a job was created for, when known (memory first, then the journal)."""
        with self._lock:
            st = self._jobs.get(job)
            if st is not None:
                return st.get("card")
            rec = self._journal.read(job) if self._journal else None
            return rec.get("card") if rec and not rec.get("corrupt") else None

    def _recover(self, job: str) -> Optional[dict[str, Any]]:
        """A job absent from memory: read what the journal says survived a restart."""
        rec = self._journal.read(job) if self._journal else None
        if rec is None:
            return None
        started = rec.get("started") if isinstance(rec.get("started"), (int, float)) else self._clock()
        if self._clock() - started > self.TTL_S:
            self._journal.delete(job)  # past retention: gone, like an expired in-memory job
            return None
        base = {"started": started, "recovered": True, "card": rec.get("card")}
        if rec.get("corrupt"):
            return {**base, "status": AskState.UNKNOWN, "ok": False,
                    "detail": "the saved record for this request is unreadable, so its outcome is unknown"}
        state = rec.get("state")
        if state == "queued":
            return {**base, "status": AskState.NOT_SUBMITTED, "ok": False,
                    "detail": "the gateway restarted before this request reached the model; nothing was sent"}
        if state == "sent":
            return {**base, "status": AskState.UNKNOWN, "ok": False,
                    "detail": "the gateway restarted while the model was answering; the request may or may not have completed and it was not sent again"}
        if state == "error":
            return {**base, "status": AskState.ERROR, "ok": False, "detail": str(rec.get("detail") or "the model could not answer")}
        if state == "done" and isinstance(rec.get("reply"), str) and rec["reply"]:
            return {**base, "status": AskState.DONE, "ok": True, "reply": rec["reply"],
                    "provider": rec.get("provider"), "model": rec.get("model")}
        if state == "done":
            return {**base, "status": AskState.UNKNOWN, "ok": False,
                    "detail": "the model finished before the restart, but answers are not kept on disk; ask again to get a new one"}
        return {**base, "status": AskState.UNKNOWN, "ok": False, "detail": "the saved record for this request is unreadable, so its outcome is unknown"}

    def create(self, job: str, card: Optional[str] = None) -> None:
        """absent -> PENDING (after expiring old jobs)."""
        with self._lock:
            self._expire()
            if job in self._jobs:
                raise IllegalTransition(f"ask job {job} already exists")
            now = self._clock()
            self._jobs[job] = {"status": AskState.PENDING, "started": now, "card": card}
            if self._journal:
                self._journal.write(job, {"state": "queued", "started": now, "card": card})
                self._arm()

    def mark_sent(self, job: str) -> bool:
        """Record, durably, that the model call is about to start (before it starts).

        False means the caller must not call the model: the "sent" marker could not be written
        and the older "queued" marker could not be removed, so after a restart the job would
        claim that nothing was sent and offer a safe resubmit although the model may have run."""
        with self._lock:
            st = self._jobs.get(job)
            if st is None or st["status"] is not AskState.PENDING:
                return False
            st["sent"] = True
            if not self._journal:
                return True
            if self._journal.write(job, {"state": "sent", "started": st["started"], "card": st.get("card"),
                                         "sent_at": self._clock()}):
                return True
            # Could not record it. A stale "queued" marker would turn into a false not_submitted,
            # so remove it: an unknown job is reported as unknown, never as safe to resend.
            self._journal.delete(job)
            return self._journal.read(job) is None

    def discard(self, job: str) -> bool:
        """Delete a job and its journal file at the caller's request, whatever its state.

        Raises DiscardFailed, leaving the job in place, when the saved record cannot be removed:
        dropping only the memory entry would let the next GET recover the saved answer."""
        with self._lock:
            gone = False
            if self._journal:
                gone = self._journal.delete(job)
                if not gone and self._journal.read(job) is not None:
                    raise DiscardFailed(f"the saved record for ask job {job} could not be deleted")
            return (self._jobs.pop(job, None) is not None) or gone

    def finish(self, job: str, reply: str, provider: Any = None, model: Any = None) -> bool:
        """PENDING -> DONE. False when the job expired meanwhile (a late worker is a no-op)."""
        return self._settle(job, {"status": AskState.DONE, "ok": True, "reply": reply,
                                  "provider": provider, "model": model})

    def fail(self, job: str, detail: str) -> bool:
        """PENDING -> ERROR. False when the job expired meanwhile."""
        return self._settle(job, {"status": AskState.ERROR, "ok": False, "detail": detail})

    def _settle(self, job: str, out: dict[str, Any]) -> bool:
        with self._lock:
            self._expire()
            st = self._jobs.get(job)
            if st is None:
                return False
            if st["status"] is not AskState.PENDING:
                raise IllegalTransition(f"ask job {job} is already {st['status'].value}")
            # One new record, swapped in whole: a poller never sees a status without its reply.
            # Dict order must follow completion, not creation: a slow pending job can
            # otherwise evict itself immediately when newer jobs have already settled.
            self._jobs.pop(job)
            self._jobs[job] = {"started": st["started"], "card": st.get("card"), **out}
            if self._journal:
                done = out["status"] is AskState.DONE
                self._journal.write(job, {
                    "state": "done" if done else "error",
                    "started": st["started"], "card": st.get("card"), "settled_at": self._clock(),
                    "provider": out.get("provider"), "model": out.get("model"),
                    "detail": None if done else out.get("detail"),
                    **({"reply": out["reply"]} if done and self._journal.keeps_replies() else {}),
                })
            settled = [k for k, v in self._jobs.items() if v["status"] in ASK_SETTLED]
            for key in settled[:-self.MAX_SETTLED]:
                self._jobs.pop(key, None)
                if self._journal:
                    self._journal.delete(key)
            return True

    def consume(self, job: str) -> dict[str, Any]:
        """DONE | ERROR -> absent, returning the settled record."""
        with self._lock:
            self._expire()
            st = self._jobs.get(job)
            if st is None:
                raise KeyError(job)
            if st["status"] not in ASK_SETTLED:
                raise IllegalTransition(f"ask job {job} is still {st['status'].value}")
            if self._journal:
                self._journal.delete(job)
            return self._jobs.pop(job)

    def _expire(self) -> None:
        cutoff = self._clock() - self.TTL_S
        for k in [k for k, v in list(self._jobs.items()) if v["started"] < cutoff]:
            self._jobs.pop(k, None)
            if self._journal:
                self._journal.delete(k)
        now = self._clock()
        if self._journal and now - self._last_prune >= self.PRUNE_EVERY_S:
            self._last_prune = now
            self._journal.prune(self.TTL_S, keep=lambda j: j in self._jobs)

    def housekeeping(self) -> None:
        """Expire jobs and prune the journal now, without waiting for another Ask."""
        with self._lock:
            self._last_prune = float("-inf")
            self._expire()

    def start_background(self, every_s: float = 60.0) -> None:
        """Run housekeeping on a daemon timer while anything remains to expire.

        A private answer saved for a restart must not outlive its retention because nobody
        asked again. The timer re-arms only while jobs or journal files exist."""
        with self._lock:
            self._bg_every = every_s
            self._arm()

    def _arm(self) -> None:
        every = getattr(self, "_bg_every", None)
        if every is None or getattr(self, "_timer", None) is not None:
            return
        t = threading.Timer(every, self._tick)
        t.daemon = True
        self._timer = t
        t.start()

    def _tick(self) -> None:
        try:
            self.housekeeping()
        finally:
            with self._lock:
                self._timer = None
                if self._jobs or (self._journal and self._journal.has_files()):
                    self._arm()

    def __len__(self) -> int:
        return len(self._jobs)
