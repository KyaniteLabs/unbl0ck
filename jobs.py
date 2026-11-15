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
    Without fcntl (no lock other processes honour) nothing is written: a restart reads such a
    job as unknown, and the journal never claims a lock it does not hold.

    consume is refused while PENDING; finish/fail are refused unless PENDING (a late
    worker for an expired job is a no-op, never a resurrection).
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


class JournalConflict(Exception):
    """A guarded write found another worker's record where it expected its own (or none)."""


def _finite(x: Any) -> bool:
    """A real, finite number (not a bool). An integer too large for a float is not finite here."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    try:
        return math.isfinite(x)
    except OverflowError:
        return False


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name} is not valid in a record")


class AskJournal:
    """Owner-only, bounded, per-job restart markers for Ask under one directory.

    One small JSON file per job (0600 in a 0700 directory), written atomically. It holds
    the job id, state, timestamps, the card id (isolation), provider/model, an error
    detail, the writing worker and, only when ``keep_replies()`` is true, the reply text.
    Files expire with the job TTL and are capped. Any I/O failure degrades to in-memory
    behaviour: ``failures`` counts them and Ask itself never breaks because of the journal.

    Every operation works through one directory descriptor opened without following
    symlinks (the directory and its parent must be real directories owned by this user), so
    a path swapped for a link after a check cannot redirect a read, write or delete. A write
    holds an advisory lock on the directory for the count, the eviction and the publish, so two
    workers cannot both take the last slot. Records are synced before they are trusted."""

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
        self.protect: Callable[[str, Optional[Path]], bool] = lambda job, d=None: False  # set by AskJobs: live jobs are never evicted

    # --- locating and opening the directory -------------------------------------------------

    def resolve(self) -> tuple[bool, Optional[Path]]:
        """(resolved, directory). A resolver that fails is (False, None): not the same thing as
        "no journal configured" (True, None), which callers must never confuse."""
        try:
            d = self._directory()
        except Exception:  # noqa: BLE001 - an unresolvable profile is "no location", never a crash
            self.failures += 1
            return False, None
        return True, None if d is None else Path(d)

    def where(self) -> Optional[Path]:
        """The directory the resolver names right now, or None (no journal, or it cannot be resolved)."""
        return self.resolve()[1]

    def _acquire(self, create: bool, d: Optional[Path]) -> tuple[Optional[int], str]:
        """(directory fd, status). status is ok | none (no journal configured) | missing (nothing
        stored there) | unavailable (cannot be examined or is not trustworthy: never "empty")."""
        if d is None:
            try:
                d = self._directory()
            except Exception:  # noqa: BLE001
                self.failures += 1
                return None, "unavailable"
        if d is None:
            return None, "none"
        d = Path(d)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        dirflag = getattr(os, "O_DIRECTORY", 0)
        fd = pfd = None
        try:
            if create:
                try:
                    parent_kind = os.lstat(d.parent).st_mode
                except FileNotFoundError:
                    os.makedirs(d.parent, mode=0o700, exist_ok=True)
                else:
                    if not stat.S_ISDIR(parent_kind):  # a link here would send the new directory elsewhere
                        self.failures += 1
                        return None, "unavailable"
            for p in (d.parent, d):  # a link in either place is never followed
                try:
                    kind = os.lstat(p).st_mode
                except FileNotFoundError:
                    if create and p == d:
                        break  # created below, relative to the parent descriptor
                    raise
                if not stat.S_ISDIR(kind):
                    self.failures += 1
                    return None, "unavailable"
            # The parent is opened once and checked against its own entry; the journal directory is
            # then opened relative to that descriptor, so a parent swapped for a link after the
            # checks cannot send the open (or any later write) to another directory.
            pfd = os.open(d.parent, os.O_RDONLY | dirflag | nofollow)
            pseen, pagain = os.fstat(pfd), os.lstat(d.parent)
            if (pseen.st_ino, pseen.st_dev) != (pagain.st_ino, pagain.st_dev):
                raise PermissionError("the parent directory changed")
            if create:
                try:
                    os.mkdir(d.name, 0o700, dir_fd=pfd)
                except FileExistsError:
                    pass
            fd = os.open(d.name, os.O_RDONLY | dirflag | nofollow, dir_fd=pfd)
            seen, again = os.fstat(fd), os.stat(d.name, dir_fd=pfd, follow_symlinks=False)
            if (seen.st_ino, seen.st_dev) != (again.st_ino, again.st_dev) or seen.st_uid != os.geteuid():
                raise PermissionError("not our directory")
            if seen.st_mode & 0o077:
                os.fchmod(fd, 0o700)
            out, fd = fd, None
            return out, "ok"
        except FileNotFoundError:
            return None, "missing"
        except NotADirectoryError:
            self.failures += 1
            return None, "missing"  # a path component is a file: nothing can be stored there
        except OSError:
            self.failures += 1
            return None, "unavailable"
        finally:
            if fd is not None:
                os.close(fd)
            if pfd is not None:
                os.close(pfd)

    @contextlib.contextmanager
    def _session(self, create: bool = False, d: Optional[Path] = None):
        fd, status = self._acquire(create, d)
        try:
            yield fd, status
        finally:
            if fd is not None:
                os.close(fd)

    @contextlib.contextmanager
    def _locked(self, fd: int, wait: Optional[float] = None):
        """Hold the directory lock; yields False (fail closed) when it cannot be taken in time.

        ``wait`` caps how long to wait (0 tries once). Without ``fcntl`` there is no lock that
        other processes honour, so the lock is never "taken": nothing is persisted rather than
        letting two writers both claim the last slot."""
        lfd = None
        got = False
        try:
            lfd = os.open(".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=fd)
            if fcntl is not None:
                deadline = time.monotonic() + (self.LOCK_WAIT_S if wait is None else wait)
                while True:
                    try:
                        fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        got = True
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() >= deadline:
                            break
                        time.sleep(0.01)
        except OSError:
            got = False
        if not got:
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
        pid, inst = owner.get("pid"), owner.get("inst")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or inst == self.inst or pid == self.pid:
            return False
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OverflowError, ValueError):
            return False  # no such process (or not a process id at all)
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
            entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if (entry.st_ino, entry.st_dev) != (st.st_ino, st.st_dev):
                return {"corrupt": True}  # the entry is a link (or was swapped): not the file that was opened
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
        started = data.get("started")
        if not _finite(started):
            return False
        return started <= self._clock() + self.FUTURE_SLACK_S

    def read(self, job: str, d: Optional[Path] = None) -> Optional[dict[str, Any]]:
        """The record, ``{"corrupt": True}`` when unreadable, ``{"unavailable": True}`` when the
        storage cannot be examined (so the job may exist), or None when it is not there."""
        if not self._valid(job):
            return None
        with self._session(False, d) as (fd, status):
            if status in ("none", "missing"):
                return None
            if fd is None:
                return {"unavailable": True}
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
        """True when another worker deleted this job while its owner was still running."""
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

    def _installed(self, fd: int, name: str, body: bytes) -> bool:
        """True when the file now at ``name`` is a regular file holding exactly ``body``."""
        try:
            rfd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=fd)
        except OSError:
            return False
        try:
            st = os.fstat(rfd)
            if not stat.S_ISREG(st.st_mode) or st.st_size != len(body):
                return False
            chunks, total = [], 0
            while total <= len(body):
                part = os.read(rfd, 65536)
                if not part:
                    break
                total += len(part)
                chunks.append(part)
            return b"".join(chunks) == body
        except OSError:
            return False
        finally:
            os.close(rfd)

    def _publish(self, fd: int, job: str, body: bytes) -> bool:
        tmp = f".{job}.{secrets.token_hex(6)}.tmp"
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
                # The name must still be the file just written, or the rename installs a stranger.
                if not os.path.samestat(os.fstat(tfd), os.stat(tmp, dir_fd=fd, follow_symlinks=False)):
                    raise OSError("the temporary record was replaced before it was installed")
            finally:
                os.close(tfd)
            os.rename(tmp, f"{job}.json", src_dir_fd=fd, dst_dir_fd=fd)
            if not self._installed(fd, f"{job}.json", body):
                # Something else took the temporary file's place before the rename: what is
                # installed is not what was meant to be published, so it must not stand.
                self.failures += 1
                try:
                    os.unlink(f"{job}.json", dir_fd=fd)
                except OSError:
                    pass
                self._written.discard(job)
                return False
        except OSError:
            self.failures += 1
            try:
                os.unlink(tmp, dir_fd=fd)
            except OSError:
                pass
            return False
        self._written.add(job)
        try:
            os.fsync(fd)
        except OSError:
            self.failures += 1
            return False
        return True

    def _expectation_met(self, expect: Any, cur: Optional[dict[str, Any]]) -> bool:
        if expect == "absent":
            return cur is None
        # A tuple of states this process may move the record on from: its own record in one of
        # them, or nothing usable. Another worker's valid record is never overwritten.
        if cur is None or cur.get("corrupt") or cur.get("unavailable"):
            return True
        owner = cur.get("owner")
        return isinstance(owner, dict) and owner.get("inst") == self.inst and cur.get("state") in expect

    def write(self, job: str, rec: dict[str, Any], d: Optional[Path] = None, expect: Any = None) -> bool:
        """True when the record is on disk and synced. False when it is not (or no journal is available).

        ``expect`` makes the write a compare-and-set under the directory lock: ``"absent"`` claims
        the id (nothing may be stored under it), a tuple of states moves this process's own record
        on from one of them. A record that is not as expected raises ``JournalConflict``, so two
        workers that both looked and found nothing cannot both take the same request.

        A new file first evicts the oldest evictable ones (``protect`` names jobs alive in
        memory), under the directory lock, so the directory never exceeds MAX_FILES however many
        workers have written to it; if nothing can be evicted the record stays in memory only."""
        if not self._valid(job):
            return False
        try:
            body = self._encode(job, rec)
        except (TypeError, ValueError, RecursionError, OverflowError):
            self.failures += 1
            return False
        with self._session(True, d) as (fd, _status):
            if fd is None:
                return False
            with self._locked(fd) as got:
                if not got or self._exists(fd, f"{job}.gone"):
                    return False
                if expect is not None and not self._expectation_met(expect, self._read_fd(fd, f"{job}.json", job)):
                    raise JournalConflict(job)
                if not self._exists(fd, f"{job}.json") and not self._make_room(fd, lambda stem: self.protect(stem, d)):
                    return False
                return self._publish(fd, job, body)

    def _touch(self, fd: int, name: str) -> bool:
        try:
            os.close(os.open(name, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=fd))
        except OSError:
            self.failures += 1
            return False
        return True

    def delete(self, job: str, d: Optional[Path] = None, tombstone: bool = False, wait: Optional[float] = None) -> bool:
        """True when a record was removed. With ``tombstone``, a job another running worker still
        owns (queued, sent or already settled, which that worker may still serve from memory) is
        also marked deleted, so that worker's later write is refused and its next read drops the
        answer. When the mark cannot be made nothing is deleted and False is returned: a deletion
        the owner can undo must not be reported as done."""
        if not self._valid(job):
            return False
        with self._session(False, d) as (fd, _status):
            if fd is None:
                return False
            with self._locked(fd, wait) as got:
                if not got:
                    return False
                name = f"{job}.json"
                if tombstone:
                    rec = self._read_fd(fd, name, job)
                    if (isinstance(rec, dict) and rec.get("state") in self.STATES
                            and not rec.get("corrupt") and self.owner_live(rec)):
                        if not self._touch(fd, f"{job}.gone"):
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
              wait: Optional[float] = None) -> int:
        """Delete expired and unreadable files, then the oldest beyond MAX_FILES.

        ``keep(job)`` protects jobs that are alive in memory. Returns how many were removed."""
        with self._session(False, d) as (fd, _status):
            if fd is None:
                return 0
            with self._locked(fd, wait) as got:
                return self._prune_locked(fd, ttl, keep) if got else 0

    def _prune_locked(self, fd: int, ttl: float, keep: Callable[[str], bool]) -> int:
        now = self._clock()
        removed = 0
        live: list[tuple[float, str]] = []
        try:
            names = os.listdir(fd)
        except OSError:
            self.failures += 1
            return 0
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
                started = rec.get("started") if rec and not rec.get("corrupt") and not rec.get("unavailable") else None
                mtime = st.st_mtime
                age_from = started if isinstance(started, (int, float)) else mtime
                if keep(stem):
                    live.append((mtime, name))
                elif now - age_from > ttl or started is None and now - mtime > ttl:
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

    def enforce_settled(self, limit: int, keep: Callable[[str], bool], d: Optional[Path] = None,
                        on_fail: Optional[Callable[[str, float], None]] = None, wait: Optional[float] = None) -> int:
        """Keep at most ``limit`` settled (done or error) records on disk, oldest settled first,
        whoever wrote them: answers recovered after a restart count against the same bound.

        Records ``keep`` protects are skipped and the next oldest goes instead, so a protected
        record never stops the bound from being enforced. A record that cannot be deleted is
        reported to ``on_fail(job, started)`` (the caller must stop serving it) and counts as gone."""
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
                        at = rec.get("settled_at")
                        settled.append((at if _finite(at) else rec["started"], stem, rec["started"]))
            except OSError:
                self.failures += 1
                return 0
        settled.sort()
        removed, excess = 0, len(settled) - limit
        for _, stem, started in settled:
            if excess <= 0:
                break
            if keep(stem):
                continue
            if self.delete(stem, d=d, wait=wait):
                removed += 1
                excess -= 1
            elif on_fail is not None:
                on_fail(stem, started)
                excess -= 1
        return removed


class JournalUnavailable(RuntimeError):
    """The journal location cannot be resolved right now, so a job cannot be scoped to a profile."""


class AskTicket(str):
    """A job id bound to the directory (profile) and generation it was created under.

    ``create`` returns one. A worker that completes the job through it can never touch a later
    job that reuses the same id after this one was deleted or expired."""

    dir: Optional[Path]
    gen: int

    def __new__(cls, job: str, dir: Optional[Path] = None, gen: int = 0):
        obj = super().__new__(cls, job)
        obj.dir = dir
        obj.gen = gen
        return obj


_PRIVATE = ("gen", "keep")


def _public(st: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in st.items() if k not in _PRIVATE}


class AskJobs:
    """``{(directory, job): {"status", "started", ...}}``. Every write goes through one named transition.

    A job lives under the journal directory (profile) it was created under, and the same id may
    exist in two profiles. A caller sees only jobs of the directory it resolves right now: when
    that cannot be resolved nothing in memory is served (it is reported unavailable, never as a
    match). Later calls from a worker thread, which carries no request context, use the
    ``AskTicket`` that ``create`` returned (or, for a plain id, the one table entry that has it)."""

    TTL_S = 900
    MAX_SETTLED = 128
    PRUNE_EVERY_S = 30  # journal housekeeping cadence; polls are every 1.5 s
    MAX_UNDELETED = 1024  # past this, entries already beyond their retention are forgotten; live ones never are
    MAX_KNOWN_DIRS = 256
    HOUSEKEEPING_BUDGET_S = 2.0  # total time one cleanup pass may spend waiting on journal locks

    def __init__(self, clock: Optional[Callable[[], float]] = None, journal: Optional[AskJournal] = None):
        self._clock = clock or (lambda: time.time())
        self._lock = threading.Lock()
        self._jobs: dict[tuple[Optional[Path], str], dict[str, Any]] = {}
        self._gen = 0
        self._journal = journal
        if journal is not None:
            journal.protect = lambda j, d=None: (d, j) in self._jobs
        # Records that could not be deleted: treated as gone, retried. Value: the job's start time.
        self._undeleted: dict[tuple[Optional[Path], str], float] = {}
        # Directories this table has served or written, so cleanup keeps visiting them after
        # their last in-memory job is gone (recovered answers and leftover temporary files).
        self._known: dict[Path, None] = {}
        self._last_prune = float("-inf")
        self._deadline = float("inf")  # monotonic time by which the current pass stops waiting on locks
        self._bg_every: Optional[float] = None
        self._timer: Optional[threading.Timer] = None

    # --- scope ------------------------------------------------------------------------------

    def _scope(self) -> tuple[bool, Optional[Path]]:
        """(resolved, directory) for the caller right now. Unresolved is never the same scope as anything."""
        if self._journal is None:
            return True, None
        return self._journal.resolve()

    def _note_dir(self, d: Optional[Path]) -> None:
        if d is None:
            return
        self._known.pop(d, None)
        self._known[d] = None
        while len(self._known) > self.MAX_KNOWN_DIRS:
            self._known.pop(next(iter(self._known)))

    def _entry(self, job: str) -> tuple[Optional[tuple[Optional[Path], str]], Optional[dict[str, Any]]]:
        """The in-memory entry this caller may act on. A ticket names its own directory and generation."""
        if isinstance(job, AskTicket):
            key = (job.dir, str(job))
            st = self._jobs.get(key)
            return (key, st) if st is not None and st.get("gen") == job.gen else (None, None)
        ok, here = self._scope()
        if ok:
            key = (here, str(job))
            st = self._jobs.get(key)
            if st is not None:
                return key, st
        return None, None

    def _worker_entry(self, job: str) -> tuple[Optional[tuple[Optional[Path], str]], Optional[dict[str, Any]]]:
        """For a worker thread: its ticket, else the caller's own entry, else the only entry with this id."""
        key, st = self._entry(job)
        if st is not None or isinstance(job, AskTicket):
            return key, st
        found = [(k, v) for k, v in self._jobs.items() if k[1] == job]
        return found[0] if len(found) == 1 else (None, None)

    def _gone_elsewhere(self, key: tuple[Optional[Path], str], st: dict[str, Any]) -> bool:
        """Another worker deleted this job (tombstone): drop it here so it is not served or resurrected."""
        if self._journal is not None and key[0] is not None and self._journal.tombstoned(key[1], d=key[0]):
            self._jobs.pop(key, None)
            return True
        return False

    # --- reading ----------------------------------------------------------------------------

    def get(self, job: str) -> Optional[dict[str, Any]]:
        with self._lock:
            self._expire()
            ok, here = self._scope()
            if ok:
                key = (here, job)
                if key in self._undeleted:
                    return None  # deleted or expired; only the removal of its file is still pending
                st = self._jobs.get(key)
                if st is not None:
                    if self._gone_elsewhere(key, st):
                        return None
                    return _public(st)
            return self._recover(job, here if ok else None)

    def card(self, job: str) -> Optional[str]:
        """The card id a job was created for, when known (memory first, then the journal)."""
        with self._lock:
            ok, here = self._scope()
            if ok and (here, job) in self._undeleted:
                return None
            if ok:
                st = self._jobs.get((here, job))
                if st is not None:
                    return st.get("card")
            rec = self._journal.read(job, d=here if ok else None) if self._journal else None
            return rec.get("card") if rec and not rec.get("corrupt") and not rec.get("unavailable") else None

    def _recover(self, job: str, here: Optional[Path]) -> Optional[dict[str, Any]]:
        """A job absent from memory: read what the journal says survived a restart."""
        rec = self._journal.read(job, d=here) if self._journal else None
        if rec is None:
            return None
        if rec.get("unavailable"):
            return {"started": self._clock(), "recovered": True, "card": None, "unavailable": True,
                    "status": AskState.UNKNOWN, "ok": False,
                    "detail": "the saved state for this request cannot be read right now, so its outcome is unknown"}
        self._note_dir(here)
        started = rec.get("started") if isinstance(rec.get("started"), (int, float)) else self._clock()
        if self._clock() - started > self.TTL_S:
            self._drop_record(job, here, started)  # past retention: gone, like an expired in-memory job
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

    # --- transitions ------------------------------------------------------------------------

    def create(self, job: str, card: Optional[str] = None) -> AskTicket:
        """absent -> PENDING (after expiring old jobs). Returns the ticket the worker completes it with.

        Raises JournalUnavailable when the journal location cannot be resolved: a job without a
        known profile could be shown to another one."""
        with self._lock:
            self._expire()
            ok, where = self._scope()
            if not ok:
                raise JournalUnavailable("the Ask journal location cannot be resolved")
            key = (where, job)
            if key in self._jobs:
                raise IllegalTransition(f"ask job {job} already exists")
            now = self._clock()
            # The profile's persistence setting is read here, in the request's own context: the
            # worker thread that settles the job resolves the default profile instead.
            keep = self._journal.keeps_replies() if self._journal else False
            if self._journal and where is not None:
                # Claim the id on disk first, atomically: another worker that also found nothing
                # under it must not go on to send the same request a second time. A record this
                # table already treats as deleted (its removal is only pending) may be replaced.
                try:
                    self._journal.write(job, {"state": "queued", "started": now, "card": card}, d=where,
                                        expect=None if key in self._undeleted else "absent")
                except JournalConflict:
                    raise IllegalTransition(f"ask job {job} already exists") from None
                self._undeleted.pop(key, None)
                self._note_dir(where)
            self._gen += 1
            self._jobs[key] = {"status": AskState.PENDING, "started": now, "card": card, "dir": where,
                               "gen": self._gen, "keep": keep}
            if self._journal:
                self._arm()
            return AskTicket(job, where, self._gen)

    def mark_sent(self, job: str) -> bool:
        """Record, durably, that the model call is about to start (before it starts).

        False means the caller must not call the model: the "sent" marker could not be written
        and the older "queued" marker could not be removed, so after a restart the job would
        claim that nothing was sent and offer a safe resubmit although the model may have run.
        It is also False when another worker has deleted the job meanwhile."""
        with self._lock:
            key, st = self._worker_entry(job)
            if st is None or st["status"] is not AskState.PENDING:
                return False
            st["sent"] = True
            where = st.get("dir")
            if not self._journal or where is None:
                return True
            try:
                if self._journal.write(key[1], {"state": "sent", "started": st["started"], "card": st.get("card"),
                                                "sent_at": self._clock()}, d=where, expect=("queued",)):
                    return True
            except JournalConflict:
                # Another worker owns this request now: it is theirs to send, not ours.
                self._jobs.pop(key, None)
                return False
            if self._gone_elsewhere(key, st):
                return False
            # Could not record it. A stale "queued" marker would turn into a false not_submitted,
            # so remove it: an unknown job is reported as unknown, never as safe to resend.
            self._journal.delete(key[1], d=where)
            return self._journal.verified_absent(key[1], owned=True, d=where)

    def discard(self, job: str) -> bool:
        """Delete a job and its journal file at the caller's request, whatever its state.

        Raises DiscardFailed, leaving the job in place, when the saved record cannot be removed:
        dropping only the memory entry would let the next GET recover the saved answer."""
        with self._lock:
            key, st = self._entry(job)
            where = st.get("dir") if st is not None else None
            gone = False
            if self._journal:
                gone = self._journal.delete(str(job), d=where, tombstone=True)
                if not gone and not self._journal.verified_absent(str(job), owned=st is not None, d=where):
                    raise DiscardFailed(f"the saved record for ask job {job} could not be deleted")
            ok, here = self._scope()
            self._undeleted.pop((where if st is not None else (here if ok else None), str(job)), None)
            return (st is not None and self._jobs.pop(key, None) is not None) or gone

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
            key, st = self._worker_entry(job)
            if st is None:
                return False
            if st["status"] is not AskState.PENDING:
                raise IllegalTransition(f"ask job {job} is already {st['status'].value}")
            if self._gone_elsewhere(key, st):
                return False  # deleted by a client while this worker was running: never resurrect it
            where, jid = st.get("dir"), key[1]
            # One new record, swapped in whole: a poller never sees a status without its reply.
            # Dict order must follow completion, not creation: a slow pending job can
            # otherwise evict itself immediately when newer jobs have already settled.
            self._jobs.pop(key)
            self._jobs[key] = {"started": st["started"], "card": st.get("card"), "dir": where,
                               "gen": st["gen"], "keep": st["keep"], **out}
            if self._journal and where is not None:
                done = out["status"] is AskState.DONE
                try:
                    wrote = self._journal.write(jid, {
                        "state": "done" if done else "error",
                        "started": st["started"], "card": st.get("card"), "settled_at": self._clock(),
                        "provider": out.get("provider"), "model": out.get("model"),
                        "detail": None if done else out.get("detail"),
                        **({"reply": out["reply"]} if done and st["keep"] else {}),
                    }, d=where, expect=("queued", "sent"))
                except JournalConflict:
                    self._jobs.pop(key, None)  # another worker's record: never answer over it
                    return False
                if not wrote and self._journal.tombstoned(jid, d=where):
                    self._jobs.pop(key, None)
                    return False
            settled = [k for k, v in self._jobs.items() if v["status"] in ASK_SETTLED]
            for k in settled[:-self.MAX_SETTLED]:
                evicted = self._jobs.pop(k, None)
                self._drop_record(k[1], k[0], evicted["started"] if evicted else None)
            if self._journal and where is not None:
                # Answers recovered from an earlier run are not in memory but still count against the bound.
                self._journal.enforce_settled(self.MAX_SETTLED, keep=lambda j: (where, j) in self._jobs, d=where,
                                              on_fail=lambda j, started: self._drop_record(j, where, started),
                                              wait=self._wait())
            return True

    def consume(self, job: str) -> dict[str, Any]:
        """DONE | ERROR -> absent, returning the settled record."""
        with self._lock:
            self._expire()
            key, st = self._entry(job)
            if st is None:
                raise KeyError(job)
            if st["status"] not in ASK_SETTLED:
                raise IllegalTransition(f"ask job {job} is still {st['status'].value}")
            where = st.get("dir")
            if (self._journal and where is not None and not self._journal.delete(key[1], d=where)
                    and not self._journal.verified_absent(key[1], owned=True, d=where)):
                # The saved reply is still on disk and would be recovered as this very job.
                raise DiscardFailed(f"the saved record for ask job {job} could not be deleted")
            return _public(self._jobs.pop(key))

    # --- retention --------------------------------------------------------------------------

    def _drop_record(self, job: str, where: Optional[Path], started: Optional[float] = None) -> None:
        """Remove a job's file. When it cannot be removed the job is still treated as gone (never
        served) and the removal is retried by housekeeping, so a failed delete cannot bypass a limit."""
        if not self._journal or where is None:
            return
        if self._journal.delete(job, d=where, wait=self._wait()) or self._journal.verified_absent(job, owned=True, d=where):
            self._undeleted.pop((where, job), None)
            return
        self._undeleted[(where, job)] = started if started is not None else self._clock()
        if len(self._undeleted) > self.MAX_UNDELETED:
            self._forget_lapsed()

    def _wait(self) -> float:
        """How long the next journal lock may be waited for: what is left of this pass's budget."""
        return max(0.0, min(self._journal.LOCK_WAIT_S if self._journal else 0.0, self._deadline - time.monotonic()))

    def _forget_lapsed(self) -> None:
        """Forget undeleted records past their retention: reading one drops it as expired anyway.
        A record still inside its retention is never forgotten, or its evicted answer would be served again."""
        cutoff = self._clock() - self.TTL_S
        for k in [k for k, started in self._undeleted.items() if started < cutoff]:
            self._undeleted.pop(k, None)

    def _retry_undeleted(self) -> None:
        for (where, job), started in list(self._undeleted.items()):
            self._drop_record(job, where, started)
        self._forget_lapsed()

    def _journal_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        ok, here = self._scope()
        for d in [here if ok else None, *[k[0] for k in self._jobs], *[k[0] for k in self._undeleted], *self._known]:
            if d is not None and d not in dirs:
                dirs.append(d)
        return dirs

    def _expire(self) -> None:
        self._deadline = time.monotonic() + self.HOUSEKEEPING_BUDGET_S
        cutoff = self._clock() - self.TTL_S
        for k in [k for k, v in list(self._jobs.items()) if v["started"] < cutoff]:
            gone = self._jobs.pop(k, None)
            self._drop_record(k[1], k[0], gone["started"] if gone else None)
        now = self._clock()
        if self._journal and now - self._last_prune >= self.PRUNE_EVERY_S:
            self._last_prune = now
            for d in self._journal_dirs():
                self._journal.prune(self.TTL_S, keep=lambda j, d=d: (d, j) in self._jobs, d=d, wait=self._wait())
            self._retry_undeleted()

    def housekeeping(self) -> None:
        """Expire jobs and prune the journal now, without waiting for another Ask."""
        with self._lock:
            self._last_prune = float("-inf")
            self._expire()

    def start_background(self, every_s: float = 60.0) -> None:
        """Run housekeeping on a daemon timer while anything remains to expire.

        A private answer saved for a restart must not outlive its retention because nobody
        asked again. The timer re-arms while jobs, undeleted records or journal files exist,
        and after a failed pass: a transient storage fault must not end cleanup for good."""
        with self._lock:
            self._bg_every = every_s
            self._arm()

    def stop_background(self) -> None:
        """Cancel the housekeeping timer and do not re-arm it (shutdown, and tests that load the API)."""
        with self._lock:
            self._bg_every = None
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _arm(self) -> None:
        every = getattr(self, "_bg_every", None)
        if every is None or getattr(self, "_timer", None) is not None:
            return
        t = threading.Timer(every, self._tick)
        t.daemon = True
        self._timer = t
        t.start()

    def _more(self) -> bool:
        """True while anything may remain to expire: jobs, undeleted records, or files in a known directory."""
        if self._jobs or self._undeleted:
            return True
        if not self._journal:
            return False
        more = False
        for d in self._journal_dirs():
            if self._journal.has_files(d):
                more = True
            else:
                self._known.pop(d, None)  # nothing left there: stop visiting it
        return more

    def _tick(self) -> None:
        failed = False
        try:
            self.housekeeping()
        except Exception:  # noqa: BLE001 - one bad pass must not end the cleanup timer
            failed = True
        finally:
            with self._lock:
                self._timer = None
                try:
                    more = self._more()
                except Exception:  # noqa: BLE001
                    more = True
                if more or failed:
                    self._arm()

    def __len__(self) -> int:
        return len(self._jobs)
