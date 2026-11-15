#!/usr/bin/env python3
"""Print one acceptance record for this host: what UNBL0CK depends on and whether it is present.

Run it with the Python that runs Hermes, on the machine being accepted:

    python scripts/host_compat.py            # JSON record on stdout; exit 1 if any interface is missing

The record carries versions, the OS, the state-lock mode and interface names only. It never reads
settings, chats, cards or credentials. Paste it into the acceptance matrix in docs/verification.md.
"""

from __future__ import annotations

import importlib.util
import json
import platform
import re
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_needs():
    spec = importlib.util.spec_from_file_location("unbl0ck_needs_compat", ROOT / "needs.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["unbl0ck_needs_compat"] = mod
    spec.loader.exec_module(mod)
    return mod


def _plugin_version() -> str:
    match = re.search(r'^version:\s*"?([^"\n]+)"?', (ROOT / "plugin.yaml").read_text(), re.M)
    return match.group(1).strip() if match else "unknown"


_PLACEHOLDERS = {"", "0.0.0", "0", "unknown", "none"}


def _text(value) -> str:
    """An external identity value as stripped text; None, a missing field or a non-string is ""."""
    return value.strip() if isinstance(value, str) else ""


def _hermes_identity() -> dict:
    """The Hermes release this process runs, from the runtime's own provenance.

    ``hermes_cli.__version__`` and ``__release_date__`` identify the checkout that is imported.
    Distribution metadata is only a fallback, and its placeholder ``0.0.0`` is never recorded
    as a version, because every revision of an editable install carries it.
    """
    version = date = ""
    try:
        import hermes_cli  # type: ignore

        version = _text(getattr(hermes_cli, "__version__", None))
        date = _text(getattr(hermes_cli, "__release_date__", None))
    except Exception:  # noqa: BLE001 - an unimportable Hermes has no runtime identity
        pass
    for dist in ("hermes-agent", "hermes_agent", "hermes"):
        if version.lower() not in _PLACEHOLDERS:
            break
        try:
            version = _text(metadata.version(dist))
        except Exception:  # noqa: BLE001 - absent or malformed metadata is no identity
            version = ""
    if version.lower() in _PLACEHOLDERS:
        version = ""
    return {"hermes_version": version or "unknown", "hermes_release_date": date or "unknown"}


def _real_time(text: str, fmt: str) -> bool:
    """True when text is a real calendar date or time in fmt: month 99 or hour 25 is not one."""
    try:
        datetime.strptime(text, fmt)
    except ValueError:
        return False
    return True


def matrix_row_problems(row: list[str]) -> list[str]:
    """Why a run row of the docs/verification.md acceptance matrix (five cells) does not identify
    its run, per the format stated above that table; a "not run" row has none."""
    _receipt, _host, revision, when, result = row
    if result.strip().lower().startswith("not run"):
        return []
    problems = []
    if not (re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", when.strip())
            and _real_time(when.strip(), "%Y-%m-%dT%H:%M:%SZ")):
        problems.append("recorded at is not a UTC time")
    if not re.search(r"\bsource [0-9a-f]{7,40}\b", revision):
        problems.append("no source commit")
    hermes = re.search(r"\bHermes (\S+)(.*)", revision)
    if not hermes or hermes.group(1).lower() in _PLACEHOLDERS:
        problems.append("no Hermes version")
    dates = re.finditer(r"\b(\d{4})([.-])(\d{1,2})\2(\d{1,2})\b", hermes.group(2)) if hermes else ()
    if not any(_real_time("-".join(m.group(1, 3, 4)), "%Y-%m-%d") for m in dates):
        problems.append("no Hermes release date")
    return problems


def record() -> dict:
    needs = _load_needs()
    report = needs.host_compat()
    return {
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plugin_version": _plugin_version(),
        **_hermes_identity(),
        "python": platform.python_version(),
        "os": f"{platform.system()} {platform.release()} {platform.machine()}",
        "state_lock": needs.state_lock_mode(),
        "interfaces": report,
    }


def main() -> int:
    rec = record()
    print(json.dumps(rec, indent=1, sort_keys=True))
    return 0 if rec["interfaces"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
