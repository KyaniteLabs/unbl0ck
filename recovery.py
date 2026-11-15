"""Operator recovery for UNBL0CK's shared state files (issue #48).

``hidden.json``, ``pending-release.json`` and ``bluf.json`` are refused rather than overwritten when
they cannot be read. This module is the explicit way out: diagnose, preserve the original bytes, and
restore from a verified backup. It is stdlib only, runs outside the dashboard, and:

* never prints a record, id, version or any text from a store (counts, sizes and fixed sentences);
* never clears or bypasses a lock, and never substitutes defaults for a record it cannot read;
* changes nothing unless ``apply`` is set, and copies the current bytes aside first.

Backups live in ``plugin-data/unbl0ck/recovery/`` (directory 0700, files 0600). They hold the same
private content as the stores they copy and are never deleted here.

CLI::

    python3 recovery.py [--home DIR] diagnose
    python3 recovery.py [--home DIR] preserve STORE
    python3 recovery.py [--home DIR] restore STORE BACKUP [--apply]
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
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


def _needs():
    """The sibling needs.py, for the data directory and the shared state lock (same flock the plugin uses)."""
    if __package__:
        from . import needs  # noqa: PLC0415
        return needs
    spec = importlib.util.spec_from_file_location("unbl0ck_recovery_needs", Path(__file__).with_name("needs.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dir(home: Path | None) -> Path:
    """The plugin data directory, without creating it."""
    return (Path(home) if home else _needs().hermes_home()) / "plugin-data" / "unbl0ck"


def _writer_active(d: Path) -> bool | None:
    """True when another process holds the state lock, None when this platform cannot say."""
    if fcntl is None:
        return None
    try:
        fd = os.open(d / LOCK_NAME, os.O_RDWR | _NOFOLLOW | _NONBLOCK)
    except FileNotFoundError:
        return False  # no lock file: nobody has ever held it
    except OSError:
        return None  # unreadable or a link: we cannot say, and never claim "no writer"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)  # closing drops our probe; the holder keeps its own lock


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
    try:
        st = p.lstat()
    except FileNotFoundError:
        return {"store": store, "file": p.name, "status": "absent", "entries": 0}
    except OSError as exc:
        return {"store": store, "file": p.name, "status": "unsafe", "error": type(exc).__name__}
    if not _regular(st):
        return {"store": store, "file": p.name, "status": "unsafe"}
    out: dict[str, Any] = {"store": store, "file": p.name, "bytes": st.st_size, "owner_only": not (st.st_mode & 0o077)}
    if st.st_size > MAX_STORE_BYTES:
        return {**out, "status": "corrupt", "entries": None, "error": "too large to be a state file"}
    try:
        raw = p.read_bytes()
    except OSError as exc:
        return {**out, "status": "unsafe", "error": type(exc).__name__}
    return {**out, **_inspect_bytes(store, raw)}


def _regular(st: os.stat_result) -> bool:
    import stat  # noqa: PLC0415
    return stat.S_ISREG(st.st_mode)


def diagnose(home: Path | None = None) -> dict[str, Any]:
    """Read-only. Per store: status, size and entry count; never any content."""
    d = _dir(home)
    writer = _writer_active(d) if d.is_dir() else False
    stores = []
    for store in STORES:
        row = _inspect(d, store)
        row["advice"] = ADVICE[row["status"]]
        stores.append(row)
    return {
        "stores": stores,
        "writer_active": writer,
        "lock_check": "could not be checked here" if writer is None else "ok",
        "backups": _list_backups(d),
        "note": "Counts only. A held lock is never cleared; wait for the writer or stop it in its owning installation.",
    }


def _recovery_dir(d: Path, create: bool) -> Path | None:
    r = d / RECOVERY_DIR
    if r.is_symlink():
        raise RuntimeError("recovery directory is a symlink; refusing to use it")
    if r.exists() and not r.is_dir():
        raise RuntimeError("recovery path is not a directory; refusing to use it")
    if not r.exists():
        if not create:
            return None
        r.mkdir(mode=0o700, parents=True)
    os.chmod(r, 0o700)
    return r


def _list_backups(d: Path) -> list[dict[str, Any]]:
    r = d / RECOVERY_DIR
    if r.is_symlink() or not r.is_dir():
        return []
    out = []
    for p in sorted(r.iterdir()):
        m = BACKUP_NAME.match(p.name)
        if not m:
            continue
        try:
            st = p.lstat()
        except OSError:
            continue
        row: dict[str, Any] = {"name": p.name, "store": m["stem"], "bytes": st.st_size}
        if not _regular(st):
            row["status"] = "unsafe"
        elif st.st_size > MAX_STORE_BYTES:
            row["status"] = "corrupt"
        else:
            try:
                row["status"] = _inspect_bytes(m["stem"], p.read_bytes())["status"]
            except OSError:
                row["status"] = "unsafe"
            row["owner_only"] = not (st.st_mode & 0o077)
        out.append(row)
    return out


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fsync_dir(p: Path) -> None:
    try:
        fd = os.open(p, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_new(path: Path, raw: bytes) -> None:
    """Create ``path`` exclusively with mode 0600, write, flush to disk and read it back."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if path.read_bytes() != raw:
        path.unlink(missing_ok=True)
        raise OSError("written copy does not match the original bytes")


def _preserve_raw(d: Path, store: str, raw: bytes) -> str:
    r = _recovery_dir(d, create=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    for n in range(100):
        name = f"{store}-{stamp}-{n:02d}.bak"
        try:
            _write_new(r / name, raw)
        except FileExistsError:
            continue
        _fsync_dir(r)
        return name
    raise OSError("could not find a free backup name")


def _read_store(d: Path, store: str) -> tuple[bytes | None, str | None]:
    """The store's exact bytes, or (None, reason) when it is absent or not a regular file."""
    p = d / STORES[store][0]
    try:
        st = p.lstat()
    except FileNotFoundError:
        return None, "absent"
    if not _regular(st):
        return None, "unsafe"
    if st.st_size > MAX_STORE_BYTES:
        return None, "too_large"
    return p.read_bytes(), None


def preserve(store: str, home: Path | None = None) -> dict[str, Any]:
    """Copy the store's current bytes, readable or not, into the recovery directory. Changes nothing else."""
    if store not in STORES:
        return {"status": "refused", "reason": "unknown store"}
    d = _dir(home)
    raw, why = _read_store(d, store)
    if raw is None:
        return {"status": "refused", "reason": {"absent": "nothing to preserve: the store does not exist",
                                                "unsafe": "the store is not a regular file; not copying it",
                                                "too_large": "the store is too large to be a state file"}[why]}
    name = _preserve_raw(d, store, raw)
    return {"status": "preserved", "backup": f"{RECOVERY_DIR}/{name}", "bytes": len(raw), "sha256": _sha(raw)}


def _check_backup(d: Path, store: str, backup: str) -> tuple[bytes | None, dict[str, Any]]:
    m = BACKUP_NAME.match(backup or "")
    if not m or m["stem"] != store:
        return None, {"status": "refused", "reason": f"backup name must be a file in {RECOVERY_DIR}/ named {store}-<label>.bak"}
    r = d / RECOVERY_DIR
    p = r / backup
    if r.is_symlink():
        return None, {"status": "refused", "reason": "recovery directory is a symlink"}
    try:
        st = p.lstat()
    except FileNotFoundError:
        return None, {"status": "refused", "reason": "backup not found"}
    if not _regular(st):
        return None, {"status": "refused", "reason": "backup is not a regular file"}
    if st.st_mode & 0o077:
        return None, {"status": "refused", "reason": "backup is readable by others; chmod 600 it first"}
    if st.st_size > MAX_STORE_BYTES:
        return None, {"status": "refused", "reason": "backup is too large to be a state file"}
    raw = p.read_bytes()
    info = _inspect_bytes(store, raw)
    if info["status"] != "healthy":
        return None, {"status": "refused", "reason": {"corrupt": "backup is not valid JSON",
                                                     "unsupported_schema": "backup is not in the shape this version reads"
                                                     }[info["status"]]}
    return raw, {"status": "ok", "entries": info["entries"], "sha256": _sha(raw)}


def restore(store: str, backup: str, home: Path | None = None, *, apply: bool = False) -> dict[str, Any]:
    """Replace the store with a verified backup. Dry run unless ``apply``.

    Order: validate the backup, refuse if another process holds the state lock, copy the current
    bytes aside and verify the copy, write the backup beside the store, verify it, then replace
    atomically. A failure at any step leaves the store as it was."""
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
    if not apply:
        return {"status": "dry_run", **plan, "next": "Repeat with apply to make the change."}
    if fcntl is None:
        return {"status": "refused", **plan, "reason": "this platform cannot confirm no other process is writing; not restoring"}
    try:
        with _needs()._state_lock(Path(home) if home else None):
            return _apply(d, store, backup, plan)
    except TimeoutError:
        return {"status": "writer_active", **plan, "reason": "another process holds the state lock; nothing was changed. "
                "Wait for it or stop it in its owning installation. The lock is never cleared here."}


def _apply(d: Path, store: str, backup: str, plan: dict[str, Any]) -> dict[str, Any]:
    raw, check = _check_backup(d, store, backup)  # again, now that no one else can write
    if raw is None:
        return check
    target = d / STORES[store][0]
    before, why = _read_store(d, store)
    if why == "unsafe" or why == "too_large":
        return {"status": "refused", "reason": "the store is not a regular file of a sane size; not replacing it"}
    tmp = d / f".restore.{os.getpid()}.tmp"
    preserved = None
    try:
        if before is not None:
            preserved = f"{RECOVERY_DIR}/{_preserve_raw(d, store, before)}"
        tmp.unlink(missing_ok=True)
        _write_new(tmp, raw)
        os.replace(tmp, target)
        _fsync_dir(d)
    except BaseException as exc:  # noqa: BLE001 - report, leave the store as it was
        tmp.unlink(missing_ok=True)
        after, _ = _read_store(d, store)
        if not isinstance(exc, Exception):
            raise
        return {"status": "failed", **plan, "error": type(exc).__name__, "preserved_current": preserved,
                "store_unchanged": after == before}
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
            bad = any(s["status"] not in ("healthy", "absent") for s in out["stores"])
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
