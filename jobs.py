"""The two background-job lifecycles behind the dashboard routes, named in one place.

Pure stdlib (no FastAPI, no Hermes) so the transitions are unit-tested everywhere CI runs.
Both tables live in memory only: nothing here is persisted.

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

    consume is refused while PENDING; finish/fail are refused unless PENDING (a late
    worker for an expired job is a no-op, never a resurrection).
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Any, Callable, Optional


class IllegalTransition(RuntimeError):
    """A job was asked to move between two states that are not connected."""


# --- Summary (BLUF) jobs ---------------------------------------------------------------------

class BlufState(str, Enum):
    PENDING = "pending"   # queued on the pool or running
    FAILED = "failed"     # the last run raised; the card shows its raw text until a retry


class BlufJobs:
    """``{(item_id, version): {"state", "at", "error"}}`` with one function per move."""

    RETRY_S = 600      # a failed summary is retried this long after it failed
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
            return age >= self.RETRY_S
        return age >= self.STUCK_S

    def start(self, key: tuple[str, str]) -> bool:
        """absent | FAILED (cooled down) | PENDING (stuck) -> PENDING. False when refused."""
        with self._lock:
            if not self.can_start(key):
                return False
            self._prune()
            self._jobs[key] = {"state": BlufState.PENDING, "at": self._clock(), "error": None}
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
            self._jobs[key] = {"state": BlufState.FAILED, "at": self._clock(), "error": str(error)}

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


ASK_SETTLED = frozenset({AskState.DONE, AskState.ERROR})


class AskJobs:
    """``{job: {"status", "started", ...}}``. Every write goes through one named transition."""

    TTL_S = 900

    def __init__(self, clock: Optional[Callable[[], float]] = None):
        self._clock = clock or (lambda: time.time())
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def get(self, job: str) -> Optional[dict[str, Any]]:
        st = self._jobs.get(job)
        return None if st is None else dict(st)

    def create(self, job: str) -> None:
        """absent -> PENDING (after expiring old jobs)."""
        with self._lock:
            self._expire()
            if job in self._jobs:
                raise IllegalTransition(f"ask job {job} already exists")
            self._jobs[job] = {"status": AskState.PENDING, "started": self._clock()}

    def finish(self, job: str, reply: str, provider: Any = None, model: Any = None) -> bool:
        """PENDING -> DONE. False when the job expired meanwhile (a late worker is a no-op)."""
        return self._settle(job, {"status": AskState.DONE, "ok": True, "reply": reply,
                                  "provider": provider, "model": model})

    def fail(self, job: str, detail: str) -> bool:
        """PENDING -> ERROR. False when the job expired meanwhile."""
        return self._settle(job, {"status": AskState.ERROR, "ok": False, "detail": detail})

    def _settle(self, job: str, out: dict[str, Any]) -> bool:
        with self._lock:
            st = self._jobs.get(job)
            if st is None:
                return False
            if st["status"] is not AskState.PENDING:
                raise IllegalTransition(f"ask job {job} is already {st['status'].value}")
            # One new record, swapped in whole: a poller never sees a status without its reply.
            self._jobs[job] = {"started": st["started"], **out}
            return True

    def consume(self, job: str) -> dict[str, Any]:
        """DONE | ERROR -> absent, returning the settled record."""
        with self._lock:
            st = self._jobs.get(job)
            if st is None:
                raise KeyError(job)
            if st["status"] not in ASK_SETTLED:
                raise IllegalTransition(f"ask job {job} is still {st['status'].value}")
            return self._jobs.pop(job)

    def _expire(self) -> None:
        cutoff = self._clock() - self.TTL_S
        for k in [k for k, v in list(self._jobs.items()) if v["started"] < cutoff]:
            self._jobs.pop(k, None)

    def __len__(self) -> int:
        return len(self._jobs)
