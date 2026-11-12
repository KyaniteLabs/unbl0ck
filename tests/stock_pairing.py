"""A stand-in for stock Hermes's ``gateway.pairing.PairingStore`` when tests run outside
Hermes. It honours the public ``list_pending()`` contract (request_id, user_id, user_name,
age_minutes; never the code) over the same on-disk layout, so the plugin is tested
against the API, not the files. test_pairing_stock.py checks the real store when present."""

import json
import time
from pathlib import Path

TTL = 3600


def factory(home: Path):
    def make(profile: str):
        return FakeStore(home if profile == "default" else home / "profiles" / profile)

    return make


class FakeStore:
    def __init__(self, phome: Path):
        new, old = phome / "platforms" / "pairing", phome / "pairing"
        self._dir = new if new.is_dir() or not old.is_dir() else old

    def list_pending(self, platform=None):
        out = []
        for f in sorted(self._dir.glob("*-pending.json")):
            p = f.name[: -len("-pending.json")]
            try:
                data = json.loads(f.read_text())
            except (OSError, ValueError):
                data = {}
            for k, v in (data if isinstance(data, dict) else {}).items():
                c = v.get("created_at") if isinstance(v, dict) else None
                if not isinstance(c, (int, float)) or time.time() - c > TTL:
                    continue
                modern = isinstance(v.get("hash"), str) and isinstance(
                    v.get("salt"), str
                )
                out.append(
                    {
                        "platform": p,
                        "request_id": str(k) if modern else "",
                        "user_id": v.get("user_id", ""),
                        "user_name": v.get("user_name", ""),
                        "age_minutes": int((time.time() - c) / 60),
                    }
                )
        return out


class patched:
    """Point the plugin's default store at the fake for code paths that build it themselves."""

    def __init__(self, needs, home):
        self.needs, self.make = needs, factory(home)

    def __enter__(self):
        self.saved = self.needs._stock_pairing_store
        self.needs._stock_pairing_store = self.make

    def __exit__(self, *exc):
        self.needs._stock_pairing_store = self.saved
