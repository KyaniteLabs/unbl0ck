"""The "needs you" queue: what is waiting on the human, from Kanban and from Bot Mode chats.

Two sources, one list:

- Kanban: a blocked card whose latest block asks the principal for something (an explicit
  ``needs_input`` block that names the principal, or a reason that is plainly a principal gate).
  Answering one writes back only through native ``hermes_cli.kanban_db`` calls: a comment by the
  principal, then ``unblock_task`` when asked to release it.
- Bot Mode: a canonical Bot Chat whose last visible turn is the bot asking the human something,
  with no human reply after it. Chats are read read-only; answering happens in the chat itself.

Nothing here generates, ranks or rewrites decisions, and nothing is accepted on silence. Items the
human hides with "Not for me" stay hidden until the source changes (a new block or a new turn).
This module imports Hermes lazily so it can be unit-tested without a Hermes install.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Optional

# Who the human is. Set in Hermes config: plugins.entries.unbl0ck.settings.principal_name
# (comma-separated names, e.g. "Ada, Ada Lovelace"). Generic words (CEO, principal, human)
# always count. Answers are commented as the first name, or comment_author if set.
_GENERIC = ("CEO", "principal", "human")


def _settings() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly  # type: ignore
        entries = (load_config_readonly().get("plugins") or {}).get("entries") or {}
        return dict((entries.get("unbl0ck") or {}).get("settings") or {})
    except Exception:  # noqa: BLE001 - unit tests and bare installs run without Hermes config
        return {}


def principal_names(settings: Optional[dict[str, Any]] = None) -> list[str]:
    raw = (settings if settings is not None else _settings()).get("principal_name") or os.environ.get("UNBL0CK_PRINCIPAL", "")
    return [n.strip() for n in str(raw).split(",") if n.strip()]


def principal_author(settings: Optional[dict[str, Any]] = None) -> str:
    s = settings if settings is not None else _settings()
    names = principal_names(s)
    return str(s.get("comment_author") or os.environ.get("UNBL0CK_AUTHOr") or (names[0] if names else "user")).strip()
CHAT_WINDOW_DAYS = 14
CHAT_TITLES = ("Bot Chat",)

_PATTERN_CACHE: dict[str, dict[str, re.Pattern[str]]] = {}


def _patterns(names: Optional[list[str]] = None) -> dict[str, re.Pattern[str]]:
    """Detection regexes for the configured principal name(s); cached per name set."""
    names = principal_names() if names is None else names
    alt = "|".join(re.escape(n) for n in names) or r"(?!x)x"
    key = alt
    if key in _PATTERN_CACHE:
        return _PATTERN_CACHE[key]
    pats: dict[str, re.Pattern[str]] = {}

    def _c(pattern: str, flags: int = 0) -> re.Pattern[str]:
        return re.compile(pattern.replace("{P}", alt), flags)

    # A reason that is a principal gate on its own, with or without a needs_input kind.
    pats["_GATE"] = _c(
        r"\b(CEO[- ](gate|gated|word|decision|approval|call|sign[- ]?off)"
        r"|({P}|CEO|principal)(['’]s)? (approval|decision|call|word|go|ok|sign[- ]?off|review|answer|reply|input|choice)"
        r"|(awaiting|waiting (on|for)|needs?|requires?|blocked on|pending) ({P}|the CEO|CEO|principal|human)"
        r"|({P}|CEO) (must|to) (approve|decide|choose|pick|confirm|review|answer|reply|open|click|sign)"
        r"|human[- ]gate)\b",
        re.I,
    )
    # Weaker cue, trusted only together with an explicit needs_input kind.
    pats["_NAMES"] = _c(r"\b({P}|CEO|principal|human)\b", re.I)
    # The supervisor/ops "stop" style blocks name the principal without asking anything.
    pats["_ORDER"] = _c(r"^\s*({P}|CEO)\s+(\d{1,2}:\d{2}|says|said|orders?|ordered)\b", re.I)

    pats["_ASK_STRONG"] = _c(
        r"\b(need(s)? (your|you to)|your (call|decision|approval|go|ok|sign[- ]?off|pick|answer)"
        r"|(please )?(approve|confirm|choose|pick one|reply with|say (yes|go))"
        r"|waiting on you|blocked on you|want (me|it) to|should I|shall I|do you want|which (one|option)"
        r"|decision (needed|for you)|needs? you"
        r"|(picks?|options?|choices?|decisions?|questions?) for (you|the CEO|{P})"
        r"|reply needed|your (pick|answer|word)|(CEO|{P})(['’]s)? (call|pick|decision|word)"
        r"|decision packet)\b",
        re.I,
    )
    pats["_YOU"] = _c(r"\b(you|your|{P}|CEO)\b", re.I)
    _PATTERN_CACHE[key] = pats
    return pats


def hermes_home() -> Path:
    """The org root home. A profile's plugin host runs with HERMES_HOME=<root>/profiles/<p>;
    the queue covers every profile, so climb back to the root."""
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    if home.parent.name == "profiles":
        home = home.parent.parent
    return home


def data_dir(home: Optional[Path] = None) -> Path:
    d = (home or hermes_home()) / "plugin-data" / "unbl0ck"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- hidden items -------------------------------------------------------------------------

def _hidden_path(home: Optional[Path]) -> Path:
    return data_dir(home) / "hidden.json"


def load_hidden(home: Optional[Path] = None) -> dict[str, str]:
    try:
        return json.loads(_hidden_path(home).read_text())
    except Exception:
        return {}


def hide(item_id: str, version: str, home: Optional[Path] = None) -> None:
    hidden = load_hidden(home)
    hidden[item_id] = version
    p = _hidden_path(home)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(hidden, indent=1, sort_keys=True))
    tmp.replace(p)


# --- classification (pure) -----------------------------------------------------------------

def kanban_needs_you(reason: str, kind: Optional[str]) -> Optional[str]:
    """Why a blocked card's latest block is waiting on the principal, or None."""
    reason = (reason or "").strip()
    pats = _patterns()
    if not reason or pats["_ORDER"].match(reason):
        return None
    if pats["_GATE"].search(reason):
        return "Block reason names a decision for you"
    if (kind or "") == "needs_input" and pats["_NAMES"].search(reason):
        return "Blocked for your input"
    return None


def chat_asks_you(text: str) -> bool:
    """Does a bot's final turn ask the human for something?"""
    text = (text or "").strip()
    tail = text[-900:]
    if not tail:
        return False
    pats = _patterns()
    if pats["_ASK_STRONG"].search(text[-4000:]):
        return True
    return "?" in tail[-400:] and bool(pats["_YOU"].search(tail[-400:]))


def _excerpt(text: str, n: int = 480) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text if len(text) <= n else "…" + text[-n:]


# --- Kanban source -------------------------------------------------------------------------

def _boards() -> list[str]:
    from hermes_cli import kanban_db_boards as kb  # type: ignore

    out = []
    for b in kb.list_boards(include_archived=False):
        slug = b.get("slug") or b.get("name")
        if slug and not b.get("archived"):
            out.append(slug)
    return out or ["default"]


def kanban_items(boards: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    from hermes_cli import kanban_db  # type: ignore
    from hermes_cli import kanban_db_connect as kbc  # type: ignore

    items: list[dict[str, Any]] = []
    author = principal_author()
    for board in boards or _boards():
        try:
            conn = kbc.connect(board=board)
        except Exception:
            continue
        with closing(conn):
            for task in kanban_db.list_tasks(conn, status="blocked"):
                events = kanban_db.list_events(conn, task.id)
                block = next((e for e in reversed(events) if e.kind == "blocked"), None)
                payload = (block.payload or {}) if block else {}
                reason = str(payload.get("reason") or "")
                why = kanban_needs_you(reason, payload.get("kind"))
                if not why:
                    continue
                since = block.created_at if block else task.created_at
                answered = any(
                    c.author.lower() == author.lower() and c.created_at >= (since or 0)
                    for c in kanban_db.list_comments(conn, task.id)
                )
                if answered:
                    continue
                items.append({
                    "id": f"kanban:{board}:{task.id}",
                    "version": str(block.id if block else since),
                    "source": "kanban",
                    "board": board,
                    "task_id": task.id,
                    "title": task.title,
                    "ask": reason,
                    "why": why,
                    "owner": task.assignee,
                    "priority": task.priority,
                    "since": since,
                })
    return items


# --- Bot Mode source -----------------------------------------------------------------------

def _state_dbs(home: Path) -> list[tuple[str, Path]]:
    out = []
    root = home / "state.db"
    if root.exists():
        out.append(("default", root))
    for db in sorted((home / "profiles").glob("*/state.db")):
        out.append((db.parent.name, db))
    return out


def _bot_name(home: Path, profile: str) -> str:
    """The name Bot Mode shows for a seat (profile.yaml ui_meta.hermes-bots.title).
    A line scan, not a YAML parser: the plugin host may not ship PyYAML."""
    try:
        lines = (home / "profiles" / profile / "profile.yaml").read_text().splitlines()
    except OSError:
        return profile
    in_bots = False
    for line in lines:
        if re.match(r"^\s{2}hermes-bots:\s*$", line):
            in_bots = True
        elif in_bots and re.match(r"^\s{0,2}\S", line):
            in_bots = False
        elif in_bots:
            m = re.match(r"^\s{4}title:\s*['\"]?(.+?)['\"]?\s*$", line)
            if m:
                return m.group(1)
    return profile


def chat_items(home: Optional[Path] = None, days: int = CHAT_WINDOW_DAYS) -> list[dict[str, Any]]:
    home = home or hermes_home()
    since = time.time() - days * 86400
    items: list[dict[str, Any]] = []
    marks = ",".join("?" * len(CHAT_TITLES))
    for profile, db in _state_dbs(home):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        except sqlite3.Error:
            continue
        with closing(conn):
            try:
                sessions = conn.execute(
                    f"SELECT id, title FROM sessions WHERE title IN ({marks}) AND COALESCE(archived,0)=0",
                    CHAT_TITLES,
                ).fetchall()
            except sqlite3.Error:
                continue
            for sid, title in sessions:
                try:
                    row = conn.execute(
                        "SELECT id, role, content, timestamp FROM messages WHERE session_id=? "
                        "AND role IN ('user','assistant') AND COALESCE(display_kind,'') != 'hidden' "
                        "AND COALESCE(active,1)=1 AND content IS NOT NULL AND TRIM(content) != '' "
                        "ORDER BY timestamp DESC, id DESC LIMIT 1",
                        (sid,),
                    ).fetchone()
                except sqlite3.Error:
                    continue
                if not row:
                    continue
                mid, role, content, ts = row
                if role != "assistant" or (ts or 0) < since or not chat_asks_you(content):
                    continue
                items.append({
                    "id": f"chat:{profile}:{sid}",
                    "version": str(mid),
                    "source": "chat",
                    "profile": profile,
                    "session_id": sid,
                    "title": f"{_bot_name(home, profile)} is asking you",
                    "ask": _excerpt(content),
                    "why": "Last turn in this Bot Chat is a question for you",
                    "owner": profile,
                    "priority": 0,
                    "since": int(ts or 0),
                })
    return items


# --- the queue -----------------------------------------------------------------------------

def build_queue(home: Optional[Path] = None, *, include_hidden: bool = False) -> dict[str, Any]:
    home = home or hermes_home()
    errors: list[str] = []
    items: list[dict[str, Any]] = []
    for name, fn in (("kanban", kanban_items), ("chat", lambda: chat_items(home))):
        try:
            items.extend(fn())
        except Exception as exc:  # one broken source must not hide the other
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    hidden = load_hidden(home)
    shown = [i for i in items if include_hidden or hidden.get(i["id"]) != i["version"]]
    shown.sort(key=lambda i: (-(i.get("since") or 0)))
    return {
        "generated_at": int(time.time()),
        "count": len(shown),
        "hidden": len(items) - len(shown),
        "items": shown,
        "errors": errors,
    }


def answer_card(board: str, task_id: str, text: str, *, release: bool) -> dict[str, Any]:
    """Comment as the principal through native Kanban, then unblock if asked."""
    from hermes_cli import kanban_db  # type: ignore
    from hermes_cli import kanban_db_connect as kbc  # type: ignore

    text = (text or "").strip()
    if not text:
        raise ValueError("answer text is required")
    with closing(kbc.connect(board=board)) as conn:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise LookupError(f"card {task_id} not found on {board}")
        if task.status != "blocked":
            raise RuntimeError(f"card {task_id} is {task.status}, not blocked")
        comment_id = kanban_db.add_comment(conn, task_id, author=principal_author(), body=text)
        released = bool(kanban_db.unblock_task(conn, task_id)) if release else False
    return {"ok": True, "comment_id": comment_id, "released": released}


# --- Pocket pack (TV / X55 card format) ----------------------------------------------------

def pocket_pack(queue: dict[str, Any], limit: int = 6) -> dict[str, Any]:
    """The queue in the Liam Pocket card format, for gadgets that opt in. Written next to the
    plugin's data, never over the gadget plugin's own pocket-pack.json."""
    now = int(time.time())
    cards = []
    for it in queue["items"][:limit]:
        if it["source"] == "kanban":
            options = [
                {"label": "Answer + unblock", "send": f"Answer and unblock {it['task_id']}", "effect": "Card resumes", "tone": "primary"},
                {"label": "Keep hold", "send": f"Keep {it['task_id']} blocked", "effect": "No change", "tone": "neutral"},
            ]
        else:
            options = [{"label": "Open chat", "send": f"Open {it['profile']} Bot Chat", "effect": "Reply in the chat", "tone": "primary"}]
        cards.append({
            "id": it["id"],
            "kind": "decide",
            "title": it["title"][:90],
            "short": it["ask"][:160],
            "choices": [o["label"] for o in options],
            "task_id": it.get("task_id", ""),
            "options": options,
            "prompt": it["ask"],
            "why_now": it["why"],
            "owner": it.get("owner") or "",
            "authority": "ceo",
            "reversibility": "reversible",
            "evidence": [it["id"]],
            "ttl_s": 3600,
        })
    return {
        "version": 1,
        "pack_id": f"unblock-{now}",
        "generated_at": now,
        "expires_at": now + 3600,
        "mode": "unblock",
        "cards": cards,
    }


def write_pocket_pack(queue: dict[str, Any], home: Optional[Path] = None) -> Path:
    p = data_dir(home) / "pocket-pack.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(pocket_pack(queue), indent=1))
    tmp.replace(p)
    return p
