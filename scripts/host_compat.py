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


def _hermes_identity() -> dict:
    """The Hermes release this process runs, from the runtime's own provenance.

    ``hermes_cli.__version__`` and ``__release_date__`` identify the checkout that is imported.
    Distribution metadata is only a fallback, and its placeholder ``0.0.0`` is never recorded
    as a version, because every revision of an editable install carries it.
    """
    version = date = ""
    try:
        import hermes_cli  # type: ignore

        version = str(getattr(hermes_cli, "__version__", "") or "")
        date = str(getattr(hermes_cli, "__release_date__", "") or "")
    except Exception:  # noqa: BLE001 - an unimportable Hermes has no runtime identity
        pass
    if version.strip().lower() in _PLACEHOLDERS:
        version = ""
        for dist in ("hermes-agent", "hermes_agent", "hermes"):
            try:
                found = metadata.version(dist)
            except Exception:  # noqa: BLE001 - absent or malformed metadata is no identity
                continue
            # A distribution without a Version field answers None: that is no identity either.
            if isinstance(found, str) and found.strip().lower() not in _PLACEHOLDERS:
                version = found
                break
    return {"hermes_version": version or "unknown", "hermes_release_date": date or "unknown"}


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
