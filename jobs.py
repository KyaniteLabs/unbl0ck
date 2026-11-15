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
        done, reply too large to keep -> UNKNOWN (with a "too large" detail)
        queued/sent owned by a live process other than this one -> PENDING
        unreadable, oversized, foreign, symlinked or implausible file -> UNKNOWN
        storage that cannot be checked -> UNKNOWN (never "absent")
    The plugin never resubmits a recovered job on its own.

    consume is refused while PENDING; finish/fail are refused unless PENDING (a late
    worker for an expired or deleted job is a no-op, never a resurrection). A worker names
    its job by the generation token create returned, and a job is seen only from the
    journal directory (profile) it was created in.
"""

from __future__ import annotations

import contextlib
import errno
import json
import math
import os
import secrets
import stat
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - not available off POSIX
    fcntl = None  # type: ignore[assignment]


class IllegalTransition(RuntimeError):
    """A job was asked to move between two states that are not connected."""


class DiscardFailed(RuntimeError):
    """A saved Ask record could not be deleted, so the job still exists."""


class ScopeUnavailable(RuntimeError):
    """The caller's Ask journal directory (profile) cannot be resolved: it owns no job."""


# --- Summary (BLUF) jobs ---------------------------------------------------------------------

class BlufState(str, Enum):
    PENDING = "pending"   # queued on the pool or running
    FAILED = "failed"     # the last run raised; the card shows its raw text until a retry


class BlufJobs:
    """``{(item_id, version): {"state", "at", "error"}}`` with one function per move."""

    RETRY_S = 600      # the first retry waits this long; each later one waits twice as long
    MAX_ATTEMPTS = 3   # after this many failures a card version is not summarized again
    STUCK_S = 3600     # a pending job this old never reported back (the model call times out at 90 s)
    PRUNE_OVER = 500   # table size at which stuck pending jobs are dropped
    HARD_CAP = 5000    # hard bound: past it the oldest failed entries go, even ones out of retries

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
        # A failure that cooled down still carries its attempt count; dropping it would let a
        # card version that keeps failing restart from zero, so only stuck pending jobs go first.
        now = self._clock()
        for k in [k for k, st in self._jobs.items()
                  if st["state"] is BlufState.PENDING and now - st["at"] >= self.STUCK_S]:
            self._jobs.pop(k, None)
        # A version out of retries never "can start", so the pass above keeps it for ever. Bound
        # the table anyway: the oldest failures go first, pending jobs never.
        over = len(self._jobs) - (self.HARD_CAP - 1)
        if over > 0:
            failed = sorted((v["at"], k) for k, v in self._jobs.items() if v["state"] is BlufState.FAILED)
            for _, k in failed[:over]:
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


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name} is not valid in a record")


def finite_number(v: Any, lo: int = -2**53, hi: int = 2**53) -> Optional[float]:
    """A record's number as a float, or None: never a bool, never NaN or infinity, never outside
    [lo, hi]. An int is range-checked as an int, before any float conversion could overflow."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return float(v) if lo <= v <= hi else None


def valid_pid(v: Any) -> Optional[int]:
    """A process id ``os.kill`` accepts (1 to 2**31-1), or None."""
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 2**31 else None


class AskJournal:
    """Owner-only, bounded, per-job restart markers for Ask under one directory.

    One small JSON file per job (0600 in a 0700 directory), written atomically. It holds
    the job id, state, timestamps, the card id (isolation), provider/model, an error
    detail, the writing worker and, only when the job was created with reply persistence on,
    the reply text. Files expire with the job TTL and are capped. ``failures`` counts I/O faults; a job
    whose first record cannot be claimed is refused, since another worker may be claiming the same id.

    The directory is opened through its parent's descriptor, neither followed through a link
    (the directory and its parent must be real directories, the directory owned by this user),
    and every operation works through that descriptor, so a path swapped for a link after a check
    cannot redirect a read, write or delete. A write holds an advisory lock on the directory for
    the count, the eviction and the publish, so two workers cannot both take the last slot, and
    checks afterwards that the installed entry is the file it wrote. A deleted job leaves a
    tombstone (``job.gone``) until it expires: no worker writes that id again and no reader serves it."""

    MAX_FILES = 256
    MAX_RECORD_BYTES = 262144  # a record holds one reply of at most a few thousand tokens
    LOCK_WAIT_S = 5.0
    STATES = ("queued", "sent", "done", "error")
    FUTURE_SLACK_S = 60.0

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
        self.pid = os.getpid()
        self.inst = secrets.token_hex(8)  # one per journal object: a restart in the same process is still a new owner
        self._written: set[str] = set()  # jobs whose marker reached disk at some point
        self.protect: Callable[[Optional[Path], str], bool] = lambda d, job: False  # set by AskJobs: live jobs are never evicted

    # --- locating and opening the directory -------------------------------------------------

    def where(self) -> Optional[Path]:
        """The directory the resolver names right now, or None when no journal is configured.
        Raises ScopeUnavailable when it cannot be resolved: an unknown scope is nobody's, never everyone's."""
        try:
            d = self._directory()
        except Exception as exc:  # noqa: BLE001 - an unresolvable profile is "unavailable", never a crash
            self.failures += 1
            raise ScopeUnavailable("the Ask journal directory cannot be resolved") from exc
        return None if d is None else Path(d)

    def _acquire(self, create: bool, d: Optional[Path]) -> tuple[Optional[int], str]:
        """(directory fd, status). status is ok | none (no journal configured) | missing (nothing
        stored there) | unavailable (cannot be examined or is not trustworthy: never "empty")."""
        if d is None:
            try:
                d = self.where()
            except ScopeUnavailable:
                return None, "unavailable"
            if d is None:
                return None, "none"
        d = Path(d)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        pfd = fd = None
        try:
            if create:
                os.makedirs(d.parent, mode=0o700, exist_ok=True)
            pfd = os.open(d.parent, flags)  # a link in either place is never followed
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(d.name, 0o700, dir_fd=pfd)
            fd = os.open(d.name, flags, dir_fd=pfd)
            seen = os.fstat(fd)
            if seen.st_uid != os.geteuid():
                raise PermissionError("not our directory")
            if seen.st_mode & 0o077:
                os.fchmod(fd, 0o700)
            fd, out = None, fd
            return out, "ok"
        except FileNotFoundError:
            return None, "missing"
        except NotADirectoryError:
            # A link or a file where a directory belongs cannot be trusted; a file further up
            # means nothing can be stored there.
            self.failures += 1
            try:
                os.lstat(d.name, dir_fd=pfd) if pfd is not None else os.lstat(d.parent)
            except OSError:
                return None, "missing"
            return None, "unavailable"
        except OSError:
            self.failures += 1
            return None, "unavailable"
        finally:
            for f in (fd, pfd):
                if f is not None:
                    os.close(f)

    @contextlib.contextmanager
    def _session(self, create: bool = False, d: Optional[Path] = None):
        fd, status = self._acquire(create, d)
        try:
            yield fd, status
        finally:
            if fd is not None:
                os.close(fd)

    def deadline(self) -> float:
        """A ``time.monotonic`` deadline one lock wait from now."""
        return time.monotonic() + self.LOCK_WAIT_S

    @contextlib.contextmanager
    def _locked(self, fd: int, until: float):
        """Hold the directory lock until ``until`` (monotonic) at most; yields "ok", "busy" (another
        holder kept it past the deadline) or "failed"."""
        lfd = None
        got = "failed"
        try:
            lfd = os.open(".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=fd)
            if fcntl is None:
                got = "ok"
            else:
                while True:
                    try:
                        fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        got = "ok"
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EAGAIN, errno.EACCES):
                            break
                        if time.monotonic() >= until:
                            got = "busy"
                            break
                        time.sleep(0.01)
        except OSError:
            got = "failed"
        if got != "ok":
            self.failures += 1
        try:
            yield got
        finally:
            if lfd is not None:
                os.close(lfd)  # closing releases the lock

    @staticmethod
    def _valid(job: str) -> bool:
        return bool(job) and len(job) <= 64 and all(c in "0123456789abcdef" for c in job)

    def keeps_replies(self) -> bool:
        try:
            return bool(self._keep_replies())
        except Exception:  # noqa: BLE001 - a settings fault must not break Ask
            return False

    # --- reading ----------------------------------------------------------------------------

    def owner_live(self, rec: dict[str, Any]) -> bool:
        """True when another running process (not this journal object) wrote this record."""
        owner = rec.get("owner")
        if not isinstance(owner, dict):
            return False
        pid = valid_pid(owner.get("pid"))
        if pid is None or owner.get("inst") == self.inst or pid == self.pid:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _read_fd(self, fd: int, name: str, job: str) -> Optional[dict[str, Any]]:
        """None when absent, ``{"corrupt": True}`` when it cannot be trusted, else the record."""
        try:
            rfd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=fd)
        except FileNotFoundError:
            return None
        except OSError:
            self.failures += 1
            return {"corrupt": True}
        try:
            st = os.fstat(rfd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > self.MAX_RECORD_BYTES or st.st_uid != os.geteuid():
                return {"corrupt": True}  # a link, FIFO, other owner or too big: never read into memory
            if st.st_mode & 0o022:
                return {"corrupt": True}  # somebody else could have written it
            if st.st_mode & 0o044:
                try:
                    os.fchmod(rfd, 0o600)
                except OSError:
                    pass
            chunks, total = [], 0
            while True:
                part = os.read(rfd, 65536)
                if not part:
                    break
                total += len(part)
                if total > self.MAX_RECORD_BYTES:
                    return {"corrupt": True}
                chunks.append(part)
        except OSError:
            self.failures += 1
            return {"corrupt": True}
        finally:
            os.close(rfd)
        try:
            data = json.loads(b"".join(chunks).decode("utf-8"), parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            return {"corrupt": True}
        return data if self._plausible(data, job) else {"corrupt": True}

    def _plausible(self, data: Any, job: str) -> bool:
        if not isinstance(data, dict) or data.get("job") != job or data.get("state") not in self.STATES:
            return False
        v = data.get("v")
        if isinstance(v, bool) or v != 1 or not isinstance(v, int):
            return False
        if "card" not in data or not isinstance(data["card"], (str, type(None))):
            return False
        started = finite_number(data.get("started"))
        return started is not None and started <= self._clock() + self.FUTURE_SLACK_S

    def read(self, job: str, d: Optional[Path] = None) -> Optional[dict[str, Any]]:
        """The record, ``{"corrupt": True}`` when unreadable, ``{"unavailable": True}`` when the
        storage cannot be examined (so the job may exist), or None when it is not there or was
        deleted (tombstoned), whatever a failed removal left behind."""
        if not self._valid(job):
            return None
        with self._session(False, d) as (fd, status):
            if status in ("none", "missing"):
                return None
            if fd is None:
                return {"unavailable": True}
            if self._exists(fd, f"{job}.gone"):
                return None
            return self._read_fd(fd, f"{job}.json", job)

    def _exists(self, fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            self.failures += 1
            return True

    def tombstoned(self, job: str, d: Optional[Path] = None) -> bool:
        """True when this job was deleted (by any worker) and its tombstone has not expired."""
        if not self._valid(job):
            return False
        with self._session(False, d) as (fd, _status):
            return fd is not None and self._exists(fd, f"{job}.gone")

    def verified_absent(self, job: str, owned: bool = False, d: Optional[Path] = None) -> bool:
        """True only when the marker is known not to exist. Storage that cannot be examined is
        unknown, never absent: a caller deciding whether a model call may start, or whether a saved
        answer is gone, must not treat a failed look as an empty directory. ``owned`` says this
        process created the job, so a marker that never reached disk (``_written``) cannot be
        hiding there."""
        if not self._valid(job):
            return job not in self._written
        with self._session(False, d) as (fd, status):
            if status == "none":
                return job not in self._written  # no location: only safe if nothing was ever stored
            if status == "missing":
                return True
            if fd is None:
                return owned and job not in self._written
            try:
                os.stat(f"{job}.json", dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return True
            except OSError:
                self.failures += 1
                return False
            return False

    # --- writing ----------------------------------------------------------------------------

    @staticmethod
    def _dangling(fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=fd)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False

    def _entries(self, fd: int) -> list[tuple[float, str]]:
        """(mtime, name) of the regular .json files, judged without following symlinks. A link named
        like a record is junk in a directory only this plugin writes: it is removed (the link,
        never a target) and any one entry that cannot be examined is skipped, so a single bad entry
        cannot stop every later record from being written or cleaned up."""
        out: list[tuple[float, str]] = []
        for name in os.listdir(fd):
            if not name.endswith(".json"):
                continue
            try:
                st = os.lstat(name, dir_fd=fd)
                if stat.S_ISLNK(st.st_mode):
                    if self._dangling(fd, name):  # a live link is left for read() to call corrupt
                        os.unlink(name, dir_fd=fd)
                elif stat.S_ISREG(st.st_mode):
                    out.append((st.st_mtime, name))
            except OSError:
                self.failures += 1
        return out

    def _make_room(self, fd: int, keep: Callable[[str], bool]) -> bool:
        """Evict the oldest evictable files so one more record fits under MAX_FILES. Never a job
        alive in this process, nor a queued or sent job of another running worker."""
        try:
            entries = sorted(self._entries(fd))
            count = len(entries)
            for _, name in entries:
                if count < self.MAX_FILES:
                    break
                stem = name[: -len(".json")]
                if keep(stem):
                    continue
                rec = self._read_fd(fd, name, stem)
                if (isinstance(rec, dict) and not rec.get("corrupt") and rec.get("state") in ("queued", "sent")
                        and self.owner_live(rec)):
                    continue
                os.unlink(name, dir_fd=fd)
                count -= 1
            return count < self.MAX_FILES
        except OSError:
            self.failures += 1
            return False

    @staticmethod
    def _dumps(obj: dict[str, Any]) -> bytes:
        try:
            return json.dumps(obj, ensure_ascii=False, default=str, allow_nan=False).encode("utf-8")
        except UnicodeEncodeError:  # a lone surrogate in the text: escape it instead of losing the record
            return json.dumps(obj, ensure_ascii=True, default=str, allow_nan=False).encode("ascii")

    def _encode(self, job: str, rec: dict[str, Any]) -> bytes:
        base = {"v": 1, "job": job, **rec, "owner": {"pid": self.pid, "inst": self.inst}}
        body = self._dumps(base)
        if len(body) > self.MAX_RECORD_BYTES and "reply" in base:
            # Too big to read back: keep the state and say why the answer is not there.
            base = {k: v for k, v in base.items() if k != "reply"}
            base["reply_too_large"] = True
            body = self._dumps(base)
        if len(body) > self.MAX_RECORD_BYTES:
            raise ValueError("record too large")
        return body

    def _publish(self, fd: int, job: str, body: bytes) -> bool:
        tmp, dst = f".{job}.{secrets.token_hex(6)}.tmp", f"{job}.json"
        try:
            tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=fd)
        except OSError:
            self.failures += 1
            return False
        try:
            try:
                os.fchmod(tfd, 0o600)
                view = memoryview(body)
                while view:
                    view = view[os.write(tfd, view):]
                os.fsync(tfd)
                os.rename(tmp, dst, src_dir_fd=fd, dst_dir_fd=fd)
                # What the rename installed must be the file just written: a temporary name swapped
                # meanwhile would install a stranger (for example an older state) under this job.
                if not os.path.samestat(os.fstat(tfd), os.stat(dst, dir_fd=fd, follow_symlinks=False)):
                    os.unlink(dst, dir_fd=fd)
                    raise OSError("the installed record is not the one written")
            finally:
                os.close(tfd)
        except OSError:
            self.failures += 1
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=fd)
            return False
        self._written.add(job)
        try:
            os.fsync(fd)
        except OSError:
            self.failures += 1
            return False
        return True

    def _put(self, job: str, rec: dict[str, Any], d: Optional[Path], claim: bool) -> str:
        """"ok" (on disk and synced), "taken" (a tombstone, or with ``claim`` a record, holds this
        id), "busy" (another holder kept the lock) or "failed" (no journal, no room, or an I/O fault)."""
        if not self._valid(job):
            return "failed"
        try:
            body = self._encode(job, rec)
        except (TypeError, ValueError, RecursionError, OverflowError):
            self.failures += 1
            return "failed"
        with self._session(True, d) as (fd, _status):
            if fd is None:
                return "failed"
            with self._locked(fd, self.deadline()) as got:
                if got != "ok":
                    return got
                if self._exists(fd, f"{job}.gone"):
                    return "taken"  # deleted: never written again, by its owner or anyone else
                fresh = not self._exists(fd, f"{job}.json")
                if claim and not fresh:
                    return "taken"
                if fresh and not self._make_room(fd, lambda j: self.protect(d, j)):
                    return "failed"
                return "ok" if self._publish(fd, job, body) else "failed"

    def write(self, job: str, rec: dict[str, Any], d: Optional[Path] = None) -> bool:
        """True when the record is on disk and synced; a new file first evicts the oldest evictable ones."""
        return self._put(job, rec, d, claim=False) == "ok"

    def claim(self, job: str, rec: dict[str, Any], d: Optional[Path] = None) -> str:
        """Write a first record under the directory lock unless a record or tombstone holds the id:
        "ok", "taken", "busy" or "failed"."""
        return self._put(job, rec, d, claim=True)

    def _touch(self, fd: int, name: str) -> bool:
        """Create ``name`` (or leave it); False when it could not be."""
        try:
            os.close(os.open(name, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=fd))
        except OSError:
            self.failures += 1
            return False
        return True

    def delete(self, job: str, d: Optional[Path] = None, tombstone: bool = False,
               until: Optional[float] = None) -> bool:
        """True when a record was removed; with ``tombstone`` it is fenced first, and a failed fence
        removes nothing."""
        if not self._valid(job):
            return False
        with self._session(False, d) as (fd, _status):
            if fd is None:
                return False
            name = f"{job}.json"
            with self._locked(fd, until or self.deadline()) as got:
                if got != "ok":
                    return False
                # Fenced under the lock, so a record published while this waited is fenced too.
                if tombstone and self._exists(fd, name) and not self._touch(fd, f"{job}.gone"):
                    return False
                try:
                    os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    return False
                except OSError:
                    self.failures += 1
                    return False
                self._written.discard(job)
                try:
                    os.fsync(fd)
                except OSError:
                    self.failures += 1
                return True

    def has_files(self, d: Optional[Path] = None) -> bool:
        """True while anything may remain to expire. Storage that cannot be listed counts as yes."""
        with self._session(False, d) as (fd, status):
            if status in ("none", "missing"):
                return False
            if fd is None:
                return True
            try:
                return any(n.endswith((".json", ".tmp", ".gone")) for n in os.listdir(fd))
            except OSError:
                self.failures += 1
                return True

    def prune(self, ttl: float, keep: Callable[[str], bool] = lambda job: False, d: Optional[Path] = None,
              until: Optional[float] = None) -> int:
        """Delete expired and unreadable files, then the oldest beyond MAX_FILES.

        ``keep(job)`` protects jobs that are alive in memory. Returns how many were removed."""
        with self._session(False, d) as (fd, _status):
            if fd is None:
                return 0
            with self._locked(fd, until or self.deadline()) as got:
                return self._prune_locked(fd, ttl, keep) if got == "ok" else 0

    def _prune_locked(self, fd: int, ttl: float, keep: Callable[[str], bool]) -> int:
        now = self._clock()
        removed = 0
        live: list[tuple[float, str]] = []
        try:
            names = os.listdir(fd)
        except OSError:
            self.failures += 1
            return 0
        fenced = {n[: -len(".gone")] for n in names if n.endswith(".gone")}
        for name in names:
            try:
                if name.endswith((".tmp", ".gone")):
                    if now - os.lstat(name, dir_fd=fd).st_mtime > ttl:
                        os.unlink(name, dir_fd=fd)
                        removed += 1
                    continue
                if not name.endswith(".json"):
                    continue
                st = os.lstat(name, dir_fd=fd)
                if stat.S_ISLNK(st.st_mode) and self._dangling(fd, name):
                    os.unlink(name, dir_fd=fd)  # the link itself, never what it points at
                    removed += 1
                    continue
                stem = name[: -len(".json")]
                rec = self._read_fd(fd, name, stem) if self._valid(stem) else None
                started = finite_number(rec.get("started")) if rec and not rec.get("corrupt") else None
                mtime = st.st_mtime
                if keep(stem):
                    live.append((mtime, name))
                elif stem in fenced or now - (mtime if started is None else started) > ttl:  # deleted, or expired
                    os.unlink(name, dir_fd=fd)
                    removed += 1
                else:
                    live.append((mtime, name))
            except OSError:
                self.failures += 1
        live.sort()
        for _, name in live[: max(0, len(live) - self.MAX_FILES)]:
            if not keep(name[: -len(".json")]):
                try:
                    os.unlink(name, dir_fd=fd)
                    removed += 1
                except OSError:
                    self.failures += 1
        if removed:
            try:
                os.fsync(fd)
            except OSError:
                self.failures += 1
        return removed

    def enforce_settled(self, limit: int, keep: Callable[[str], bool], d: Optional[Path] = None) -> int:
        """Keep at most ``limit`` settled (done or error) records on disk, oldest settled first,
        whoever wrote them: answers recovered after a restart count against the same bound. A
        ``keep`` job is skipped without using up an eviction, and the walk goes on until the count
        is within the limit; a record that could not be removed but is tombstoned no longer counts."""
        with self._session(False, d) as (fd, _status):
            if fd is None:
                return 0
            try:
                names = [n for n in os.listdir(fd) if n.endswith(".json")]
                if len(names) <= limit:
                    return 0
                settled: list[tuple[float, str]] = []
                for name in names:
                    stem = name[: -len(".json")]
                    rec = self._read_fd(fd, name, stem) if self._valid(stem) else None
                    if rec and not rec.get("corrupt") and rec.get("state") in ("done", "error"):
                        at = finite_number(rec.get("settled_at"))
                        settled.append((finite_number(rec["started"]) if at is None else at, stem))
            except OSError:
                self.failures += 1
                return 0
        excess = len(settled) - limit
        removed = 0
        for _, stem in sorted(settled):
            if excess <= 0:
                break
            if keep(stem):
                continue
            if self.delete(stem, d=d, tombstone=True):
                removed += 1
                excess -= 1
            elif self.tombstoned(stem, d=d):
                excess -= 1
        return removed


class AskJobs:
    """``{(dir, job): {"status", ...}}``, one named transition per write. A job belongs to the
    scope (journal directory) its request resolved; workers name it by ``create``'s generation."""

    TTL_S = 900
    MAX_SETTLED = 128
    PRUNE_EVERY_S = 30  # journal housekeeping cadence; polls are every 1.5 s

    def __init__(self, clock: Optional[Callable[[], float]] = None, journal: Optional[AskJournal] = None):
        self._clock = clock or (lambda: time.time())
        self._lock = threading.Lock()
        self._jobs: dict[tuple[Optional[Path], str], dict[str, Any]] = {}
        self._journal = journal
        if journal is not None:
            journal.protect = lambda d, j: (d, j) in self._jobs
        self._dirs: set[Path] = set()
        self._last_prune = float("-inf")
        self._bg_every: Optional[float] = None
        self._timer: Optional[threading.Timer] = None

    def _scope(self) -> Optional[Path]:
        """The caller's journal directory (None: no journal). Raises ScopeUnavailable."""
        return self._journal.where() if self._journal else None

    def _unavailable(self) -> dict[str, Any]:
        return {"started": self._clock(), "recovered": True, "card": None, "unavailable": True,
                "status": AskState.UNKNOWN, "ok": False,
                "detail": "the saved state for this request cannot be read right now, so its outcome is unknown"}

    def _gone_elsewhere(self, key: tuple[Optional[Path], str], st: dict[str, Any]) -> bool:
        """Another worker deleted this job (tombstone): drop it here so it is not served or resurrected."""
        if self._journal is not None and key[0] is not None and self._journal.tombstoned(key[1], d=key[0]):
            self._jobs.pop(key, None)
            return True
        return False

    def _entry(self, job: str, gen: str) -> Optional[tuple[Optional[Path], str]]:
        """The key of the job a worker was started for: the same id and the same generation."""
        return next((k for k, st in self._jobs.items() if k[1] == job and st["gen"] == gen), None)

    def get(self, job: str) -> Optional[dict[str, Any]]:
        self._sweep(False)
        with self._lock:
            try:
                where = self._scope()
            except ScopeUnavailable:
                return self._unavailable()
            st = self._jobs.get((where, job))
            if st is not None:
                return None if self._gone_elsewhere((where, job), st) else dict(st)
            return self._recover(job, where)

    def card(self, job: str) -> Optional[str]:
        """The card id a job was created for, when known (memory first, then the journal)."""
        with self._lock:
            try:
                where = self._scope()
            except ScopeUnavailable:
                return None
            st = self._jobs.get((where, job))
            if st is not None:
                return st.get("card")
            rec = self._journal.read(job, d=where) if self._journal and where is not None else None
            return rec.get("card") if rec and not rec.get("corrupt") and not rec.get("unavailable") else None

    def _recover(self, job: str, where: Optional[Path]) -> Optional[dict[str, Any]]:
        """A job absent from memory: read what the journal says survived a restart."""
        rec = self._journal.read(job, d=where) if self._journal and where is not None else None
        if rec is None:
            return None
        if rec.get("unavailable"):
            return self._unavailable()
        self._dirs.add(where)  # a recovered record must be cleaned up even if nobody asks again
        self._arm()
        started = finite_number(rec.get("started"))
        started = self._clock() if started is None else started
        if self._clock() - started > self.TTL_S:
            self._evict(job, where)  # past retention: gone, like an expired in-memory job
            return None
        base = {"started": started, "recovered": True, "card": rec.get("card")}
        if rec.get("corrupt"):
            return {**base, "status": AskState.UNKNOWN, "ok": False,
                    "detail": "the saved record for this request is unreadable, so its outcome is unknown"}
        state = rec.get("state")
        if state in ("queued", "sent") and self._journal.owner_live(rec):
            # Another running worker still owns it: it is in progress there, not lost.
            return {**base, "status": AskState.PENDING}
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
        if state == "done" and rec.get("reply_too_large"):
            return {**base, "status": AskState.UNKNOWN, "ok": False,
                    "detail": "the model finished before the restart, but the answer was too large to keep on disk; ask again to get a new one"}
        if state == "done":
            return {**base, "status": AskState.UNKNOWN, "ok": False,
                    "detail": "the model finished before the restart, but answers are not kept on disk; ask again to get a new one"}
        return {**base, "status": AskState.UNKNOWN, "ok": False, "detail": "the saved record for this request is unreadable, so its outcome is unknown"}

    def create(self, job: str, card: Optional[str] = None) -> str:
        """absent -> PENDING in the caller's scope; returns the worker's generation token. Raises
        ScopeUnavailable or IllegalTransition."""
        self._sweep(False)
        with self._lock:
            where = self._scope()
            if (where, job) in self._jobs:
                raise IllegalTransition(f"ask job {job} already exists")
            now = self._clock()
            if self._journal and where is not None:
                got = self._journal.claim(job, {"state": "queued", "started": now, "card": card}, d=where)
                if got == "taken":
                    raise IllegalTransition(f"ask job {job} already exists")
                if got != "ok":  # unclaimed, another worker may admit this id too: never admit it twice
                    raise ScopeUnavailable(f"ask job {job} could not be claimed")
                self._dirs.add(where)
            gen = secrets.token_hex(8)
            self._jobs[(where, job)] = {"status": AskState.PENDING, "started": now, "card": card, "dir": where,
                                        "gen": gen, "keep": bool(self._journal and self._journal.keeps_replies())}
            self._arm()
            return gen

    def mark_sent(self, job: str, *, gen: str) -> bool:
        """Record, durably, that the model call is about to start (before it starts).

        False means the caller must not call the model: the "sent" marker could not be written (or
        the job was deleted meanwhile), so the "queued" marker still truthfully says nothing was sent."""
        with self._lock:
            key = self._entry(job, gen)
            st = self._jobs.get(key) if key else None
            if st is None or st["status"] is not AskState.PENDING:
                return False
            where = key[0]
            if not self._journal or where is None:
                return True
            return self._journal.write(job, {"state": "sent", "started": st["started"], "card": st.get("card"),
                                             "sent_at": self._clock()}, d=where)

    def discard(self, job: str) -> bool:
        """Delete a job and its journal file at the caller's request, whatever its state.

        Raises DiscardFailed, leaving the job in place, when the saved record cannot be removed:
        dropping only the memory entry would let the next GET recover the saved answer."""
        with self._lock:
            try:
                where = self._scope()
            except ScopeUnavailable as exc:
                raise DiscardFailed(f"the saved record for ask job {job} cannot be reached") from exc
            gone = False
            if self._journal and where is not None:
                gone = self._journal.delete(job, d=where, tombstone=True)
                if not gone and not self._journal.verified_absent(job, owned=(where, job) in self._jobs, d=where):
                    raise DiscardFailed(f"the saved record for ask job {job} could not be deleted")
            return self._jobs.pop((where, job), None) is not None or gone

    def finish(self, job: str, reply: str, provider: Any = None, model: Any = None, *, gen: str) -> bool:
        """PENDING -> DONE. False when the job expired or was deleted meanwhile (a late worker is a no-op)."""
        return self._settle(job, gen, {"status": AskState.DONE, "ok": True, "reply": reply,
                                       "provider": provider, "model": model})

    def fail(self, job: str, detail: str, *, gen: str) -> bool:
        """PENDING -> ERROR. False when the job expired or was deleted meanwhile."""
        return self._settle(job, gen, {"status": AskState.ERROR, "ok": False, "detail": detail})

    def _settle(self, job: str, gen: str, out: dict[str, Any]) -> bool:
        self._sweep(False, adopt=False)  # a worker thread has no request context
        with self._lock:
            key = self._entry(job, gen)
            st = self._jobs.get(key) if key else None
            if st is None:
                return False
            if st["status"] is not AskState.PENDING:
                raise IllegalTransition(f"ask job {job} is already {st['status'].value}")
            if self._gone_elsewhere(key, st):
                return False  # deleted by a client while this worker was running: never resurrect it
            where = key[0]
            # One new record, swapped in whole: a poller never sees a status without its reply.
            # Dict order must follow completion, not creation: a slow pending job can
            # otherwise evict itself immediately when newer jobs have already settled.
            self._jobs.pop(key)
            self._jobs[key] = {"started": st["started"], "card": st.get("card"), "dir": where, "gen": gen,
                               "keep": st["keep"], **out}
            if self._journal and where is not None:
                done = out["status"] is AskState.DONE
                wrote = self._journal.write(job, {
                    "state": "done" if done else "error",
                    "started": st["started"], "card": st.get("card"), "settled_at": self._clock(),
                    "provider": out.get("provider"), "model": out.get("model"),
                    "detail": None if done else out.get("detail"),
                    **({"reply": out["reply"]} if done and st["keep"] else {}),
                }, d=where)
                if not wrote and self._journal.tombstoned(job, d=where):
                    self._jobs.pop(key, None)
                    return False
            settled = [k for k, v in self._jobs.items() if v["status"] in ASK_SETTLED]
            for k in settled[:-self.MAX_SETTLED]:
                self._jobs.pop(k, None)
                self._evict(k[1], k[0])
            if self._journal and where is not None:
                # Answers recovered from an earlier run are not in memory but still count against the bound.
                self._journal.enforce_settled(self.MAX_SETTLED, keep=lambda j: (where, j) in self._jobs, d=where)
            return True

    def consume(self, job: str) -> dict[str, Any]:
        """DONE | ERROR -> absent, returning the settled record."""
        self._sweep(False)
        with self._lock:
            try:
                where = self._scope()
            except ScopeUnavailable as exc:
                raise KeyError(job) from exc
            st = self._jobs.get((where, job))
            if st is None:
                raise KeyError(job)
            if st["status"] not in ASK_SETTLED:
                raise IllegalTransition(f"ask job {job} is still {st['status'].value}")
            if (self._journal and where is not None and not self._journal.delete(job, d=where)
                    and not self._journal.verified_absent(job, owned=True, d=where)):
                # The saved reply is still on disk and would be recovered as this very job.
                raise DiscardFailed(f"the saved record for ask job {job} could not be deleted")
            return self._jobs.pop((where, job))

    def _evict(self, job: str, where: Optional[Path], until: Optional[float] = None) -> None:
        """Remove an expired or evicted job's record. Its tombstone is written first, so a record
        whose removal fails is still never served or written again; the TTL prune removes both."""
        if self._journal and where is not None:
            self._journal.delete(job, d=where, tombstone=True, until=until)

    def _sweep(self, prune: bool, adopt: bool = True) -> None:
        """Expire jobs and, when due, prune every known directory outside the table lock under one
        deadline; ``adopt`` adds the caller's directory."""
        where = None
        if adopt and self._journal is not None:
            with contextlib.suppress(ScopeUnavailable):
                where = self._scope()
        with self._lock:
            if where is not None:
                self._dirs.add(where)
            cutoff = self._clock() - self.TTL_S
            doomed = [k for k, v in self._jobs.items() if v["started"] < cutoff]
            for k in doomed:
                self._jobs.pop(k, None)
            now = self._clock()
            due = self._journal is not None and (prune or now - self._last_prune >= self.PRUNE_EVERY_S)
            if due:
                self._last_prune = now
            dirs = sorted(self._dirs) if due else []
        if self._journal is None:
            return
        until = self._journal.deadline()
        for where, job in doomed:
            self._evict(job, where, until)
        for d in dirs:
            self._journal.prune(self.TTL_S, keep=lambda j, d=d: (d, j) in self._jobs, d=d, until=until)

    def housekeeping(self) -> None:
        """Expire jobs and prune the journal now (the caller's directory included), without
        waiting for another Ask, and keep the timer going while anything remains."""
        self._sweep(True)
        self._rearm(False)

    def start_background(self, every_s: float = 60.0) -> None:
        """Run housekeeping on a daemon timer while anything remains to expire.

        A private answer saved for a restart must not outlive its retention because nobody
        asked again. The timer is armed only while jobs or journal files exist in a known
        directory (so setting the cadence starts nothing by itself), and after a failed pass:
        a transient storage fault must not end cleanup for good. It never resolves a directory
        itself: a timer thread has no request, so it visits only directories already known."""
        with self._lock:
            self._bg_every = every_s
        self._rearm(False)

    def _arm(self) -> None:
        if self._bg_every is None or self._timer is not None:
            return
        t = threading.Timer(self._bg_every, self._tick)
        t.daemon = True
        self._timer = t
        t.start()

    def _rearm(self, failed: bool) -> None:
        with self._lock:
            more, dirs = failed or bool(self._jobs), sorted(self._dirs)
        if not more and self._journal is not None:
            try:
                more = any(self._journal.has_files(d) for d in dirs)
            except Exception:  # noqa: BLE001
                more = True
        if more:
            with self._lock:
                self._arm()

    def _tick(self) -> None:
        with self._lock:
            self._timer = None
        failed = False
        try:
            self._sweep(True, adopt=False)  # a timer thread has no request: known directories only
        except Exception:  # noqa: BLE001 - one bad pass must not end the cleanup timer
            failed = True
        self._rearm(failed)

    def __len__(self) -> int:
        return len(self._jobs)
