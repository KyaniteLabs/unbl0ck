"""Operator recovery for UNBL0CK's shared state files (issue #48).

``hidden.json``, ``pending-release.json`` and ``bluf.json`` are refused rather than overwritten when
they cannot be read. This module is the explicit way out: diagnose, preserve the original bytes, and
restore from a verified backup. It is stdlib only, runs outside the dashboard, and:

* never prints a record, id, version or any text from a store (counts, sizes and fixed sentences);
* never clears or bypasses a lock, and never substitutes defaults for a record it cannot read;
* ``restore`` changes nothing unless ``apply`` is set, and copies the current bytes aside first;
  ``preserve`` only adds a backup file (creating the recovery directory on first use) and never
  changes the store; ``diagnose`` writes nothing.

Backups live in ``plugin-data/unbl0ck/recovery/`` (directory created 0700 when absent, files 0600). They hold the same
private content as the stores they copy and are never deleted here.

CLI::

    python3 recovery.py [--home DIR] diagnose
    python3 recovery.py [--home DIR] preserve STORE
    python3 recovery.py [--home DIR] restore STORE BACKUP [--apply]
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import importlib.util
import json
import os
import re
import sys
import time
from contextlib import contextmanager
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
_POSIX_MODES = os.name != "nt"  # Windows reports 0666/0444 for any file: permission bits mean nothing there
# One diagnosis reads a bounded amount however many backups pile up (they are never deleted).
MAX_LISTED_BACKUPS = 100
MAX_SCANNED_ENTRIES = 10_000  # directory entries one diagnosis looks at, matching or not
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
    """The sibling needs.py, for the data directory, the shared state lock (the same flock the
    plugin uses) and the plain-file reader. Loaded once."""
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


def _is_link(p: Path) -> bool:
    return p.is_symlink() or bool(getattr(os.path, "isjunction", lambda _p: False)(p))


# A directory is checked once (no link, a real directory) and its identity is remembered. Every
# later read, backup and replacement opens that directory again, compares the identity, and then
# works through the descriptor, so a swap after the check is refused instead of followed.
_DIRFD = (os.name != "nt" and all(f in os.supports_dir_fd for f in (os.open, os.stat, os.unlink, os.rename, os.mkdir))  # os.replace takes the same dir_fd arguments as os.rename
          and os.scandir in os.supports_fd)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_TRUSTED: dict[str, tuple[int, int]] = {}


def _trust(p: Path) -> None:
    """Remember which directory *p* is now (identity, not name). Only a real directory, never a link."""
    prefix = str(p)
    for key in [k for k in _TRUSTED if k == prefix or k.startswith(prefix + os.sep)]:
        del _TRUSTED[key]  # a directory under a checked one is trusted again when it is next checked
    if not _DIRFD:
        return
    try:
        fd = os.open(p, os.O_RDONLY | _O_DIRECTORY | _NOFOLLOW)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError("the plugin data path contains a link or a non-directory; refusing to use it") from exc
    try:
        st = os.fstat(fd)
        _TRUSTED[str(p)] = (st.st_dev, st.st_ino)
    finally:
        os.close(fd)


@contextmanager
def _anchored(p: Path):
    """A descriptor for directory *p* that is the directory checked earlier (``None`` where the
    platform has no directory descriptors). A different directory under the same name is refused."""
    if not _DIRFD:
        yield None
        return
    fd = os.open(p, os.O_RDONLY | _O_DIRECTORY | _NOFOLLOW)
    try:
        st = os.fstat(fd)
        if _TRUSTED.setdefault(str(p), (st.st_dev, st.st_ino)) != (st.st_dev, st.st_ino):
            raise RuntimeError("a state directory changed after it was checked; refusing to use it")
        yield fd
    finally:
        os.close(fd)


def _open_at(pfd: int | None, p: Path, flags: int, mode: int = 0o777) -> int:
    return os.open(p, flags, mode) if pfd is None else os.open(p.name, flags, mode, dir_fd=pfd)


def _lstat_at(pfd: int | None, p: Path) -> os.stat_result:
    return os.lstat(p) if pfd is None else os.stat(p.name, dir_fd=pfd, follow_symlinks=False)


def _unlink_at(pfd: int | None, p: Path) -> None:
    try:
        os.unlink(p) if pfd is None else os.unlink(p.name, dir_fd=pfd)
    except FileNotFoundError:
        pass


def _dir(home: Path | None) -> Path:
    """The plugin data directory, without creating it. The two directories below the chosen
    home must be real directories: a link at either place would send every read, backup and
    replacement to another folder, whatever the file names there look like. The directory that
    passes is the one every later step uses (see ``_anchored``)."""
    base = Path(home) if home else _needs().hermes_home()
    for part in (base / "plugin-data", base / "plugin-data" / "unbl0ck"):
        if _is_link(part):
            raise RuntimeError("the plugin data path contains a link; refusing to use it")
    d = base / "plugin-data" / "unbl0ck"
    _trust(d)
    return d


def _writer_active(d: Path) -> bool | None:
    """True when another process holds the state lock, None when this platform or filesystem
    cannot say (no flock, an unreadable or linked lock file, a filesystem without locks)."""
    if fcntl is None:
        return None
    try:
        with _anchored(d) as dfd:  # the lock of the directory that was checked, whatever the name is now
            fd = _open_at(dfd, d / LOCK_NAME, os.O_RDWR | _NOFOLLOW | _NONBLOCK)
    except FileNotFoundError:
        return False  # no lock file: nobody has ever held it
    except RuntimeError:
        raise
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


def _read_file(p: Path, limit: int = MAX_STORE_BYTES):
    """``(raw, fstat, None, None)`` or ``(None, None, why, error)``. The bytes come from one open
    descriptor that is not a link, FIFO or device, are bounded by what is actually read, and
    carry the ``fstat`` of the file read, so nothing is decided on a name that can change."""
    try:
        with _anchored(p.parent) as pfd:
            raw, st = _needs()._read_plain_bytes_st(p, limit, dir_fd=pfd)
        return raw, st, None, None
    except FileNotFoundError:
        return None, None, "absent", None
    except OSError as exc:
        why = getattr(exc, "reason", None)
        return None, None, "too_large" if why == "too_large" else "unsafe", type(exc).__name__


def _owner_only(st: os.stat_result) -> bool | None:
    """None where the platform does not report real permission bits (Windows)."""
    return not (st.st_mode & 0o077) if _POSIX_MODES else None


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


def _inspect(d: Path, store: str) -> dict[str, Any]:
    p = d / STORES[store][0]
    base = {"store": store, "file": p.name}
    raw, st, why, err = _read_file(p)
    if why == "absent":
        return {**base, "status": "absent", "entries": 0}
    if why == "too_large":
        try:
            with _anchored(p.parent) as pfd:
                size = _lstat_at(pfd, p).st_size
        except OSError:
            size = None
        return {**base, "bytes": size, "status": "corrupt", "entries": None, "error": "too large to be a state file"}
    if why:
        return {**base, "status": "unsafe", **({"error": err} if err else {})}
    out = {**base, "bytes": st.st_size}
    owner = _owner_only(st)
    if owner is not None:
        out["owner_only"] = owner
    return {**out, **_inspect_bytes(store, raw)}


def diagnose(home: Path | None = None) -> dict[str, Any]:
    """Read-only. Per store: status, size and entry count; never any content."""
    d = _dir(home)
    writer = _writer_active(d) if d.is_dir() else False
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


def _fsync_dir(p: Path) -> None:
    """Make a directory's entries durable. A failure is raised: a backup or a replacement whose
    directory entry may not survive a crash must not be reported as done. Windows cannot open a
    directory to sync it, so it is skipped there."""
    if not _POSIX_MODES:
        return
    with _anchored(p) as fd:
        if fd is None:
            fd = os.open(p, os.O_RDONLY)
        os.fsync(fd)


class _NotDurable(OSError):
    """A backup was created and verified, but its directory entry could not be synced. It exists
    on disk, so the caller must still be able to name it."""

    def __init__(self, name: str) -> None:
        super().__init__("the backup was created but its directory entry could not be made durable")
        self.backup_name = name


def _recovery_dir(d: Path, create: bool) -> Path | None:
    """The recovery directory. It is created (0700) only when absent; an existing one keeps its
    mode, because ``preserve`` only adds a file. Its identity is remembered for later steps."""
    r = d / RECOVERY_DIR
    if _is_link(r):
        raise RuntimeError("recovery directory is a symlink; refusing to use it")
    if r.exists() and not r.is_dir():
        raise RuntimeError("recovery path is not a directory; refusing to use it")
    if not r.exists():
        if not create:
            return None
        with _anchored(d) as dfd:
            if dfd is None:
                r.mkdir(mode=0o700)
            else:
                os.mkdir(r.name, 0o700, dir_fd=dfd)
        _fsync_dir(d)  # the new directory's entry lives in its parent
    _trust(r)
    return r


def _list_backups(d: Path) -> dict[str, Any]:
    """Backups, newest label first, within a fixed budget (entries looked at, time, count shown and
    bytes read), enforced while enumerating and reading, so a large archive cannot turn a
    read-only diagnosis into a long or multi-gigabyte scan. What a budget left unchecked is
    counted or marked, never silently dropped; ``complete`` is false when enumeration stopped
    early (the totals are then lower bounds)."""
    r = d / RECOVERY_DIR
    if _is_link(r) or (r.exists() and not r.is_dir()):
        return {"state": "unsafe", "items": [], "total": 0, "omitted": 0, "complete": True}
    if not r.is_dir():
        return {"state": "absent", "items": [], "total": 0, "omitted": 0, "complete": True}
    deadline = time.monotonic() + BACKUP_SCAN_SECONDS
    newest: list[str] = []  # a min-heap of the MAX_LISTED_BACKUPS greatest names: bounded memory
    total = seen = 0
    complete = True
    try:
        with _anchored(r) as rfd:
            with os.scandir(r if rfd is None else rfd) as entries:
                for e in entries:
                    if seen >= MAX_SCANNED_ENTRIES or time.monotonic() > deadline:
                        complete = False
                        break
                    seen += 1
                    if BACKUP_NAME.match(e.name):
                        total += 1
                        if len(newest) < MAX_LISTED_BACKUPS:
                            heapq.heappush(newest, e.name)
                        else:
                            heapq.heappushpop(newest, e.name)
    except OSError:
        return {"state": "unsafe", "items": [], "total": 0, "omitted": 0, "complete": True}
    shown = sorted(newest, reverse=True)
    budget = BACKUP_READ_BUDGET
    read_deadline = time.monotonic() + BACKUP_SCAN_SECONDS
    out = []
    for name in shown:
        p = r / name
        m = BACKUP_NAME.match(name)
        row: dict[str, Any] = {"name": name, "store": m["stem"]}
        out.append(row)
        if budget <= 0 or time.monotonic() > read_deadline:
            row["status"] = "not_checked"  # over the read budget of this diagnosis
            continue
        limit = min(MAX_STORE_BYTES, budget)
        raw, st, why, _err = _read_file(p, limit)
        if why == "too_large":
            budget -= limit  # the read consumed up to the limit before it was refused
            row["status"] = "corrupt" if limit == MAX_STORE_BYTES else "not_checked"
        elif why:
            row["status"] = "not_checked" if why == "absent" else "unsafe"  # gone since listed, or unreadable
        else:
            budget -= len(raw)
            row["bytes"] = st.st_size
            row["status"] = _inspect_bytes(m["stem"], raw)["status"]
            owner = _owner_only(st)
            if owner is not None:
                row["owner_only"] = owner
    return {"state": "ok", "items": out, "total": total, "omitted": total - len(shown), "complete": complete}


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


def _write_new(path: Path, raw: bytes) -> os.stat_result:
    """Create ``path`` exclusively with mode 0600 (never through a link), write, flush to disk and
    read the same descriptor back. Returns the ``fstat`` of the file written, so a caller can
    tell later whether the name still holds it. A copy that does not match is removed. The file
    is created inside the directory that was checked, whatever that name points at now."""
    with _anchored(path.parent) as pfd:
        fd = _open_at(pfd, path, os.O_RDWR | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
        try:
            view = memoryview(raw)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
            if _read_back(fd, len(raw)) != raw:
                raise OSError("written copy does not match the original bytes")
            st = os.fstat(fd)
            if not os.path.samestat(st, _lstat_at(pfd, path)):
                raise OSError("the new file was replaced before it could be verified")
            return st
        except BaseException:
            try:
                _unlink_at(pfd, path)
            except OSError:
                pass
            raise
        finally:
            os.close(fd)


def _preserve_raw(d: Path, store: str, raw: bytes) -> str:
    r = _recovery_dir(d, create=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    for n in range(100):
        name = f"{store}-{stamp}-{n:02d}.bak"
        try:
            _write_new(r / name, raw)
        except FileExistsError:
            continue
        try:
            _fsync_dir(r)
        except OSError as exc:
            raise _NotDurable(name) from exc  # the copy exists: the caller must be able to name it
        return name
    raise OSError("could not find a free backup name")


def _read_store(d: Path, store: str) -> tuple[bytes | None, str | None]:
    """The store's exact bytes, or (None, reason) when it is absent, not a regular file or too large."""
    raw, _st, why, _err = _read_file(d / STORES[store][0])
    return (raw, None) if why is None else (None, why)


def preserve(store: str, home: Path | None = None) -> dict[str, Any]:
    """Copy the store's current bytes, readable or not, into the recovery directory (creating it,
    0700, on first use). The store itself is not changed."""
    if store not in STORES:
        return {"status": "refused", "reason": "unknown store"}
    d = _dir(home)
    raw, why = _read_store(d, store)
    if raw is None:
        return {"status": "refused", "reason": {"absent": "nothing to preserve: the store does not exist",
                                                "unsafe": "the store is not a regular file; not copying it",
                                                "too_large": "the store is too large to be a state file"}[why]}
    try:
        name = _preserve_raw(d, store, raw)
    except _NotDurable as exc:
        return {"status": "failed", "error": type(exc.__cause__ or exc).__name__,
                "backup": f"{RECOVERY_DIR}/{exc.backup_name}",
                "reason": "the copy was created and verified but its directory entry could not be made "
                          "durable; it may not survive a crash. The store was not changed"}
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, RuntimeError):
            raise
        return {"status": "failed", "error": type(exc).__name__,
                "reason": "the copy could not be completed and made durable; the store was not changed"}
    return {"status": "preserved", "backup": f"{RECOVERY_DIR}/{name}", "bytes": len(raw), "sha256": _sha(raw)}


def _check_backup(d: Path, store: str, backup: str) -> tuple[bytes | None, dict[str, Any]]:
    m = BACKUP_NAME.match(backup or "")
    if not m or m["stem"] != store:
        return None, {"status": "refused", "reason": f"backup name must be a file in {RECOVERY_DIR}/ named {store}-<label>.bak"}
    r = d / RECOVERY_DIR
    if _is_link(r):
        return None, {"status": "refused", "reason": "recovery directory is a symlink"}
    raw, st, why, _err = _read_file(r / backup)
    if why == "absent":
        return None, {"status": "refused", "reason": "backup not found"}
    if why == "too_large":
        return None, {"status": "refused", "reason": "backup is too large to be a state file"}
    if why:
        return None, {"status": "refused", "reason": "backup is not a regular file"}
    if _POSIX_MODES and st.st_mode & 0o077:  # Windows reports 0666/0444 for any file: not a signal there
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
    atomically and read the installed file back. A failure before the replacement leaves the
    store as it was; a failure after it is reported as failed, with the preserved copy of the
    previous bytes and ``store_unchanged``, which says what the store holds:
    false when it differs from before, true when it is byte-for-byte what it was (a backup
    identical to the current file), null when it cannot be read back to say. The plan is
    rebuilt under the lock, so what is reported is what was installed."""
    if store not in STORES:
        return {"status": "refused", "reason": "unknown store"}
    d = _dir(home)
    if not d.is_dir():
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


def _apply(d: Path, store: str, backup: str) -> dict[str, Any]:
    raw, check = _check_backup(d, store, backup)  # again, now that no one else can write
    if raw is None:
        return check
    current = _inspect(d, store)
    plan = _plan(store, backup, check, current)  # what the lock now protects, not what was seen before it
    if current["status"] == "unsafe":
        return {"status": "refused", **plan, "reason": "the store is not a regular file; not replacing it"}
    target = d / STORES[store][0]
    before, why = _read_store(d, store)
    if why == "unsafe" or why == "too_large":
        return {"status": "refused", **plan, "reason": "the store is not a regular file of a sane size; not replacing it"}
    tmp = d / f".restore.{os.getpid()}.tmp"
    preserved = None
    try:
        if before is not None:
            try:
                preserved = f"{RECOVERY_DIR}/{_preserve_raw(d, store, before)}"
            except _NotDurable as nd:
                preserved = f"{RECOVERY_DIR}/{nd.backup_name}"  # created, though its entry is not durable
                raise
        with _anchored(d) as dfd:
            _unlink_at(dfd, tmp)
        written = _write_new(tmp, raw)
        with _anchored(d) as dfd:
            if not os.path.samestat(written, _lstat_at(dfd, tmp)):  # the name must still hold the verified bytes
                raise OSError("the restore file was replaced before it was installed")
            if dfd is None:
                os.replace(tmp, target)
            else:
                os.replace(tmp.name, target.name, src_dir_fd=dfd, dst_dir_fd=dfd)
        _fsync_dir(d)
        installed, _st, why, _err = _read_file(target)
        if installed != raw:
            raise OSError("the installed file is not the verified backup")
    except BaseException as exc:  # noqa: BLE001 - report, never raise away the preserved reference
        try:
            with _anchored(d) as dfd:
                _unlink_at(dfd, tmp)
        except (OSError, RuntimeError):
            pass
        if not isinstance(exc, Exception):
            raise
        try:
            after, after_why = _read_store(d, store)
            if after_why is None:
                unchanged: bool | None = after == before
            elif after_why == "absent":
                unchanged = before is None  # still absent, or gone since: either way known
            else:
                unchanged = None  # unreadable or unsafe now: the store cannot be compared, never "unchanged"
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
