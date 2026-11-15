"""Operator recovery for UNBL0CK's shared state files (issue #48).

``hidden.json``, ``pending-release.json`` and ``bluf.json`` are refused rather than overwritten when
they cannot be read. This module is the explicit way out: diagnose, preserve the original bytes, and
restore from a verified backup. It is stdlib only, runs outside the dashboard, and:

* never prints a record, id, version or any text from a store (counts, sizes and fixed sentences);
* never clears or bypasses a lock, and never substitutes defaults for a record it cannot read;
* ``restore`` changes nothing unless ``apply`` is set, and copies the current bytes aside first;
  ``preserve`` adds a backup file (creating the recovery directory on first use), sets that directory
  to 0700 and never changes the store; ``diagnose`` writes nothing;
* opens ``plugin-data``, ``unbl0ck`` and ``recovery`` one at a time through the directory above,
  never through a link, and works only through those open directories; a platform that cannot do
  that (Windows) is refused.

Backups live in ``plugin-data/unbl0ck/recovery/`` (directory 0700, files 0600). They hold the same
private content as the stores they copy and are never deleted here.

CLI::

    python3 recovery.py [--home DIR] diagnose
    python3 recovery.py [--home DIR] preserve STORE
    python3 recovery.py [--home DIR] restore STORE BACKUP [--apply]
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import heapq
import importlib.util
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable

try:
    import fcntl
except ImportError:  # Windows: no cross-process lock to probe
    fcntl = None  # type: ignore[assignment]

MAX_STORE_BYTES = 16 * 1024 * 1024
RECOVERY_DIR = "recovery"
LOCK_NAME = ".state.lock"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)  # absent on Windows
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW
# Every operation works relative to an open directory; a platform without that refuses outright.
_DIR_FD = bool(_NOFOLLOW) and all(f in os.supports_dir_fd for f in (os.open, os.mkdir, os.rename, os.unlink, os.stat))
# One diagnosis reads a bounded amount however many backups pile up (they are never deleted).
MAX_LISTED_BACKUPS = 100
BACKUP_READ_BUDGET = 64 * 1024 * 1024
BACKUP_SCAN_SECONDS = 5.0
BACKUP_NAME = re.compile(r"^(?P<stem>hidden|pending-release|bluf)-[A-Za-z0-9._-]{1,80}\.bak$")


def _hidden_ok(data: dict) -> bool:
    # Legacy and hand-edited entries may hold null or "": membership is what counts.
    return all(isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in data.items())


def _pending_ok(data: dict) -> bool:
    return all(isinstance(k, str) and isinstance(v, str) for k, v in data.items())


def _bluf_ok(data: dict) -> bool:
    return all(isinstance(k, str) and isinstance(v, dict) for k, v in data.items())


STORES: dict[str, tuple[str, Callable[[dict], bool]]] = {
    "hidden": ("hidden.json", _hidden_ok),
    "pending-release": ("pending-release.json", _pending_ok),
    "bluf": ("bluf.json", _bluf_ok),
}

ADVICE = {
    "healthy": "Nothing to repair.",
    "absent": "No record yet. Nothing to repair.",
    "corrupt": "The file is not valid JSON (truncated or damaged). Preserve it, then restore a backup.",
    "unsupported_schema": "The file is valid JSON in a shape this version does not read. Preserve it; do not edit it. "
                          "Restore a backup made by a compatible version.",
    "unsafe": "The path is not a regular file (for example a symlink). Inspect it by hand; this tool will not touch it.",
}


_NEEDS: Any = None


def _needs():
    """The sibling needs.py, for the Hermes home and the shared state lock (the same flock the
    plugin uses). Loaded once."""
    global _NEEDS
    if __package__:
        from . import needs  # noqa: PLC0415
        return needs
    if _NEEDS is None:
        spec = importlib.util.spec_from_file_location("unbl0ck_recovery_needs", Path(__file__).with_name("needs.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _NEEDS = mod
    return _NEEDS


class _Data:
    """Descriptors for ``plugin-data/unbl0ck`` and, on demand, its ``recovery/`` directory. Each is
    opened through its parent's descriptor without following a link, and every later read, write,
    replace and listing works through them, so a directory swapped for a link after it was opened
    cannot redirect anything. ``fd`` is None when the data directory does not exist."""

    def __init__(self, home: Path | None):
        if not _DIR_FD:
            raise RuntimeError("this platform cannot open the state directory without following links; refusing to use it")
        self._fds: list[int] = []
        base = Path(home) if home else _needs().hermes_home()
        fd = self._open(str(base), None, os.O_RDONLY | os.O_DIRECTORY)  # the home itself is the operator's choice
        for part in ("plugin-data", "unbl0ck"):
            fd = None if fd is None else self._open(part, fd)
        self.fd = fd
        self._rec: int | None | bool = False  # False: not opened yet

    def _open(self, name: str, at: int | None, flags: int = _DIR_FLAGS) -> int | None:
        try:
            fd = os.open(name, flags, dir_fd=at)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ENOTDIR, errno.ELOOP):
                raise RuntimeError("the plugin data path holds a link or a file where a directory belongs; refusing to use it") from exc
            raise
        self._fds.append(fd)
        return fd

    def recovery(self, create: bool) -> int | None:
        """The recovery directory's descriptor, or None when it does not exist and ``create`` is
        false. Creating it (or preparing it for a new backup) also sets it to 0700."""
        if self.fd is None:
            return None
        if self._rec is False or (self._rec is None and create):
            r = self._open(RECOVERY_DIR, self.fd)
            if r is None and create:
                os.mkdir(RECOVERY_DIR, 0o700, dir_fd=self.fd)
                _fsync_dir(self.fd)  # the new directory's entry lives in its parent
                r = self._open(RECOVERY_DIR, self.fd)
            self._rec = r
        if create and self._rec is not None:
            os.fchmod(self._rec, 0o700)
        return self._rec  # type: ignore[return-value]

    def __enter__(self) -> "_Data":
        return self

    def __exit__(self, *_exc: Any) -> None:
        for fd in self._fds:
            os.close(fd)


def _writer_active(dfd: int) -> bool | None:
    """True when another process holds the state lock, None when this platform or filesystem
    cannot say (no flock, an unreadable or linked lock file, a filesystem without locks)."""
    if fcntl is None:
        return None
    try:
        fd = os.open(LOCK_NAME, os.O_RDWR | _NOFOLLOW | _NONBLOCK, dir_fd=dfd)
    except FileNotFoundError:
        return False  # no lock file: nobody has ever held it
    except OSError:
        return None  # unreadable or a link: we cannot say, and never claim "no writer"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return None  # for example EOPNOTSUPP: unknown, never "no writer"
        return False
    finally:
        os.close(fd)  # closing drops our probe; the holder keeps its own lock


class _Budget:
    """What one diagnosis may spend on backups, charged by what is actually done: time from before
    the directory is listed, and every byte read."""

    def __init__(self) -> None:
        self.until = time.monotonic() + BACKUP_SCAN_SECONDS
        self.bytes = BACKUP_READ_BUDGET

    def expired(self) -> bool:
        return time.monotonic() > self.until


def _read_file(dfd: int, name: str, limit: int = MAX_STORE_BYTES, budget: _Budget | None = None):
    """``(raw, fstat, None)`` or ``(None, fstat or None, why)`` with why absent | unsafe | too_large.
    The bytes come from one descriptor opened in ``dfd`` that is not a link, FIFO or device, at
    most ``limit + 1`` of them are read (one more than allowed, so a longer file cannot pass), and
    each is charged to ``budget``; nothing is decided on a name that can change."""
    try:
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=dfd)
    except FileNotFoundError:
        return None, None, "absent"
    except OSError:
        return None, None, "unsafe"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, None, "unsafe"
        chunks, got = [], 0
        while got <= limit:
            part = os.read(fd, limit + 1 - got)
            if not part:
                break
            if budget is not None:
                budget.bytes -= len(part)
            chunks.append(part)
            got += len(part)
    except OSError:
        return None, None, "unsafe"
    finally:
        os.close(fd)
    return (None, st, "too_large") if got > limit else (b"".join(chunks), st, None)


def _owner_only(st: os.stat_result) -> bool:
    return not (st.st_mode & 0o077)


def _inspect_bytes(store: str, raw: bytes) -> dict[str, Any]:
    ok = STORES[store][1]
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"status": "corrupt", "entries": None}
    if not isinstance(data, dict) or not ok(data):
        return {"status": "unsupported_schema", "entries": None}
    out: dict[str, Any] = {"status": "healthy", "entries": len(data)}
    if store == "hidden":
        legacy = sum(1 for v in data.values() if not v)
        if legacy:
            out["legacy_empty_versions"] = legacy  # valid: older releases wrote null or ""
    return out


def _inspect(d: _Data, store: str) -> dict[str, Any]:
    name = STORES[store][0]
    base = {"store": store, "file": name}
    raw, st, why = _read_file(d.fd, name) if d.fd is not None else (None, None, "absent")
    if why == "absent":
        return {**base, "status": "absent", "entries": 0}
    if why == "too_large":
        return {**base, "bytes": st.st_size, "status": "corrupt", "entries": None, "error": "too large to be a state file"}
    if why:
        return {**base, "status": "unsafe"}
    return {**base, "bytes": st.st_size, "owner_only": _owner_only(st), **_inspect_bytes(store, raw)}


def diagnose(home: Path | None = None) -> dict[str, Any]:
    """Read-only. Per store: status, size and entry count; never any content."""
    with _Data(home) as d:
        writer = _writer_active(d.fd) if d.fd is not None else False
        stores = []
        for store in STORES:
            row = _inspect(d, store)
            row["advice"] = ADVICE[row["status"]]
            stores.append(row)
        listing = _list_backups(d)
    return {
        "stores": stores,
        "writer_active": writer,
        "lock_check": "could not be checked here" if writer is None else "ok",
        "recovery_dir": listing["state"],
        "backups": listing["items"],
        "backups_total": listing["total"],
        "backups_omitted": listing["omitted"],
        "backups_complete": listing["complete"],
        "note": "Counts only. A held lock is never cleared; wait for the writer or stop it in its owning installation.",
    }


def _fsync_dir(fd: int) -> None:
    """Make a directory's entries durable. A failure is raised: a backup or a replacement whose
    directory entry may not survive a crash must not be reported as done."""
    os.fsync(fd)


def _list_backups(d: _Data) -> dict[str, Any]:
    """Backups, newest label first, within one budget (count, bytes read and time, all counted from
    before the directory is listed), so a large archive cannot turn a read-only diagnosis into a
    multi-gigabyte scan. Every backup name found is either a row (``unsafe`` when it cannot even be
    examined, ``not_checked`` past the budget) or counted in ``omitted``; ``complete`` is false when
    time ran out while listing, so ``total`` is then a lower bound."""
    empty = {"items": [], "total": 0, "omitted": 0, "complete": True}
    budget = _Budget()
    try:
        r = d.recovery(create=False)
        if r is None:
            return {"state": "absent", **empty}
        newest: list[str] = []  # a min-heap of the MAX_LISTED_BACKUPS largest names seen
        total, complete = 0, True
        with os.scandir(r) as entries:
            for e in entries:
                if budget.expired():
                    complete = False
                    break
                if BACKUP_NAME.match(e.name):
                    total += 1
                    heapq.heappush(newest, e.name)
                    if len(newest) > MAX_LISTED_BACKUPS:
                        heapq.heappop(newest)
    except (OSError, RuntimeError):
        return {"state": "unsafe", **empty}
    out = []
    for name in sorted(newest, reverse=True):
        row: dict[str, Any] = {"name": name, "store": BACKUP_NAME.match(name)["stem"]}
        out.append(row)
        try:
            row["bytes"] = os.stat(name, dir_fd=r, follow_symlinks=False).st_size
        except OSError:
            row["status"] = "unsafe"  # listed but cannot be examined: shown, never dropped
            continue
        limit = min(MAX_STORE_BYTES, budget.bytes)
        if row["bytes"] > limit or budget.expired():
            row["status"] = "not_checked"  # over the budget of this diagnosis
            continue
        raw, st, why = _read_file(r, name, limit, budget)
        if why == "too_large":
            row["status"] = "corrupt" if limit == MAX_STORE_BYTES else "not_checked"
        elif why:
            row["status"] = "unsafe"
        else:
            row.update(bytes=st.st_size, status=_inspect_bytes(row["store"], raw)["status"], owner_only=_owner_only(st))
    return {"state": "ok", "items": out, "total": total, "omitted": total - len(out), "complete": complete}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_back(fd: int, expected: int) -> bytes:
    """The first ``expected + 1`` bytes of what is on disk behind *fd* (one more than written, so a
    longer file cannot pass as equal)."""
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    got = 0
    while got <= expected:
        chunk = os.read(fd, expected + 1 - got)
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _write_new(dfd: int, name: str, raw: bytes) -> None:
    """Create ``name`` in ``dfd`` exclusively with mode 0600 (never through a link), write, flush to
    disk, read the same descriptor back and check the name still holds it. A copy that does not
    match is removed."""
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=dfd)
    try:
        view = memoryview(raw)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        if _read_back(fd, len(raw)) != raw:
            raise OSError("written copy does not match the original bytes")
        if not os.path.samestat(os.fstat(fd), os.stat(name, dir_fd=dfd, follow_symlinks=False)):
            raise OSError("the new file was replaced before it could be verified")
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=dfd)
        raise
    finally:
        os.close(fd)


def _preserve_raw(d: _Data, store: str, raw: bytes) -> str:
    """The new backup's name. When the copy exists but its directory entry could not be made
    durable, the OSError raised carries the name as ``backup``: a file this created is never lost."""
    r = d.recovery(create=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    for n in range(100):
        name = f"{store}-{stamp}-{n:02d}.bak"
        try:
            _write_new(r, name, raw)
        except FileExistsError:
            continue
        try:
            _fsync_dir(r)
        except OSError as exc:
            exc.backup = f"{RECOVERY_DIR}/{name}"  # type: ignore[attr-defined]
            raise
        return f"{RECOVERY_DIR}/{name}"
    raise OSError("could not find a free backup name")


def _read_store(d: _Data, store: str) -> tuple[bytes | None, str | None]:
    """The store's exact bytes, or (None, reason) when it is absent, not a regular file or too large."""
    raw, _st, why = _read_file(d.fd, STORES[store][0]) if d.fd is not None else (None, None, "absent")
    return raw, why


def preserve(store: str, home: Path | None = None) -> dict[str, Any]:
    """Copy the store's current bytes, readable or not, into the recovery directory (creating it on
    first use and setting it to 0700). The store itself is not changed."""
    if store not in STORES:
        return {"status": "refused", "reason": "unknown store"}
    with _Data(home) as d:
        raw, why = _read_store(d, store)
        if raw is None:
            return {"status": "refused", "reason": {"absent": "nothing to preserve: the store does not exist",
                                                    "unsafe": "the store is not a regular file; not copying it",
                                                    "too_large": "the store is too large to be a state file"}[why]}
        try:
            name = _preserve_raw(d, store, raw)
        except OSError as exc:
            return {"status": "failed", "error": type(exc).__name__, "backup": getattr(exc, "backup", None),
                    "reason": "the copy could not be completed and made durable; the store was not changed"}
    return {"status": "preserved", "backup": name, "bytes": len(raw), "sha256": _sha(raw)}


def _check_backup(d: _Data, store: str, backup: str) -> tuple[bytes | None, dict[str, Any]]:
    m = BACKUP_NAME.match(backup or "")
    if not m or m["stem"] != store:
        return None, {"status": "refused", "reason": f"backup name must be a file in {RECOVERY_DIR}/ named {store}-<label>.bak"}
    try:
        r = d.recovery(create=False)
    except RuntimeError:
        return None, {"status": "refused", "reason": "recovery directory is a symlink or not a directory"}
    raw, st, why = _read_file(r, backup) if r is not None else (None, None, "absent")
    if why == "absent":
        return None, {"status": "refused", "reason": "backup not found"}
    if why == "too_large":
        return None, {"status": "refused", "reason": "backup is too large to be a state file"}
    if why:
        return None, {"status": "refused", "reason": "backup is not a regular file"}
    if not _owner_only(st):
        return None, {"status": "refused", "reason": "backup is readable by others; chmod 600 it first"}
    info = _inspect_bytes(store, raw)
    if info["status"] != "healthy":
        return None, {"status": "refused", "reason": {"corrupt": "backup is not valid JSON",
                                                     "unsupported_schema": "backup is not in the shape this version reads"
                                                     }[info["status"]]}
    return raw, {"status": "ok", "entries": info["entries"], "sha256": _sha(raw)}


def _plan(store: str, backup: str, check: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    plan = {
        "store": store,
        "backup": f"{RECOVERY_DIR}/{backup}",
        "backup_entries": check["entries"],
        "current_status": current["status"],
        "current_entries": current.get("entries"),
        "consequences": [
            "The current file is replaced by the backup.",
            "Anything changed since the backup was made is lost: dismissals, pending releases or summaries recorded after it.",
            "The current bytes are copied to the recovery directory first; nothing is deleted.",
        ],
    }
    if current["status"] == "healthy":
        plan["consequences"].append("The current file is healthy: this replaces working state.")
    return plan


def restore(store: str, backup: str, home: Path | None = None, *, apply: bool = False) -> dict[str, Any]:
    """Replace the store with a verified backup. Dry run unless ``apply``.

    Order: validate the backup, refuse if another process holds the state lock, copy the current
    bytes aside and verify the copy, write the backup beside the store, verify it, then replace
    atomically and read the installed file back. A failure is reported with ``store_unchanged``:
    true when the store still holds its previous bytes, false when it does not, and null when it
    could not be read to tell; and with the preserved copy of the previous bytes, if one was made.
    The plan is rebuilt under the lock, so what is reported is what was installed."""
    if store not in STORES:
        return {"status": "refused", "reason": "unknown store"}
    with _Data(home) as d:
        if d.fd is None:
            return {"status": "refused", "reason": "no plugin data directory"}
        raw, check = _check_backup(d, store, backup)
        if raw is None:
            return check
        current = _inspect(d, store)
        if current["status"] == "unsafe":
            return {"status": "refused", "reason": "the store is not a regular file; not replacing it"}
        plan = _plan(store, backup, check, current)
        if not apply:
            return {"status": "dry_run", **plan, "next": "Repeat with apply to make the change."}
        if fcntl is None:
            return {"status": "refused", **plan, "reason": "this platform cannot confirm no other process is writing; not restoring"}
        try:
            with _needs()._state_lock(Path(home) if home else None):
                return _apply(d, store, backup)
        except TimeoutError:
            return {"status": "writer_active", **plan, "reason": "another process holds the state lock; nothing was changed. "
                    "Wait for it or stop it in its owning installation. The lock is never cleared here."}


def _apply(d: _Data, store: str, backup: str) -> dict[str, Any]:
    raw, check = _check_backup(d, store, backup)  # again, now that no one else can write
    if raw is None:
        return check
    current = _inspect(d, store)
    plan = _plan(store, backup, check, current)  # what the lock now protects, not what was seen before it
    if current["status"] == "unsafe":
        return {"status": "refused", **plan, "reason": "the store is not a regular file; not replacing it"}
    target = STORES[store][0]
    before, why = _read_store(d, store)
    if why == "unsafe" or why == "too_large":
        return {"status": "refused", **plan, "reason": "the store is not a regular file of a sane size; not replacing it"}
    tmp = f".restore.{os.getpid()}.tmp"
    preserved = None
    try:
        if before is not None:
            preserved = _preserve_raw(d, store, before)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=d.fd)
        _write_new(d.fd, tmp, raw)
        os.replace(tmp, target, src_dir_fd=d.fd, dst_dir_fd=d.fd)
        _fsync_dir(d.fd)
        if _read_file(d.fd, target)[0] != raw:  # what the name holds now, whatever happened in between
            raise OSError("the installed file is not the verified backup")
    except BaseException as exc:  # noqa: BLE001 - report, never raise away the preserved reference
        preserved = preserved or getattr(exc, "backup", None)
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=d.fd)
        if not isinstance(exc, Exception):
            raise
        try:
            after, why = _read_store(d, store)
            # A store that cannot be read now is unknown, never "unchanged" because both reads failed.
            unchanged: bool | None = after == before if why in (None, "absent") else None
        except Exception:  # noqa: BLE001 - the reread failed too: say it is unknown, keep the rest
            unchanged = None
        return {"status": "failed", **plan, "error": type(exc).__name__, "preserved_current": preserved,
                "store_unchanged": unchanged}
    return {"status": "restored", **plan, "preserved_current": preserved, "sha256": check["sha256"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Diagnose and restore UNBL0CK shared state. Prints counts only.")
    ap.add_argument("--home", help="Hermes home (default: the configured one)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("diagnose")
    sub.add_parser("preserve").add_argument("store", choices=sorted(STORES))
    r = sub.add_parser("restore")
    r.add_argument("store", choices=sorted(STORES))
    r.add_argument("backup")
    r.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    home = Path(a.home) if a.home else None
    try:
        if a.cmd == "diagnose":
            out = diagnose(home)
            bad = any(s["status"] not in ("healthy", "absent") for s in out["stores"]) or out["recovery_dir"] == "unsafe"
        elif a.cmd == "preserve":
            out = preserve(a.store, home)
            bad = out["status"] != "preserved"
        else:
            out = restore(a.store, a.backup, home, apply=a.apply)
            bad = out["status"] not in ("restored", "dry_run")
    except (OSError, RuntimeError) as exc:
        # A damaged layout (a file where the recovery directory belongs, a link) is a refusal
        # with a fixed sentence and the error class, never a traceback or a path.
        out = {"status": "refused", "reason": f"the state directory layout is not safe to use ({type(exc).__name__}); inspect it by hand"}
        bad = True
    print(json.dumps(out, indent=1, sort_keys=True))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
