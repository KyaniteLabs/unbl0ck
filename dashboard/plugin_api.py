"""UNBL0CK backend routes, mounted at /api/plugins/unbl0ck/.

GET  /queue                  the queue (Kanban, outside items, chats, live prompts, pairing requests)
GET  /card?id=               read-only context for one Kanban card
GET  /card/ask/{job}         poll a running Ask
POST /card/ask               ask the configured model about one card (multi-turn)
POST /kanban/answer          {board, task_id, text, release, version}: comment as the principal, optionally unblock
POST /hide                   {id, version}: "Not for me" until the source changes
POST /unhide                 {id}: undo "Not for me"
POST /outside/done           {id, version}: delete a filed record or hide a watched file version
GET  /pack                   the queue as a Pocket-format card pack (read-only)
POST /pack                   the same pack, also written to plugin-data/unbl0ck/pocket-pack.json
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

PLUGIN_DIR = Path(__file__).resolve().parent.parent
log = logging.getLogger("unbl0ck")
_PACKAGE = "hermes_unbl0ck_plugin"


def _needs():
    module = sys.modules.get(_PACKAGE + ".needs")
    if module is not None:
        return module
    if _PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            _PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
        )
        pkg = importlib.util.module_from_spec(spec)
        sys.modules[_PACKAGE] = pkg
        try:
            spec.loader.exec_module(pkg)
        except BaseException:
            sys.modules.pop(_PACKAGE, None)
            raise
    return importlib.import_module(_PACKAGE + ".needs")


def _load_jobs():
    """jobs.py by file path: pure stdlib, so it loads without the plugin package."""
    name = _PACKAGE + "_jobs"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "jobs.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return module


jobs = _load_jobs()


def _loaded_ctx():
    """The plugin context stashed by ``register(ctx)``, if this process loaded it.

    The dashboard backend and the agent-side plugin run in the same process (or, under
    ``plugins.isolation: host``, in the same plugin-host process). The backend module is
    imported under its own name, so find the loaded package by its ``__init__.py`` and
    read the context without depending on a module name.
    """
    target = (PLUGIN_DIR / "__init__.py").resolve()
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        try:
            if Path(filename).resolve() != target:
                continue
        except OSError:
            continue
        ctx = getattr(module, "_PLUGIN_CTX", None)
        if ctx is not None:
            return ctx
    return None


router = APIRouter()


MAX_ID = 300            # matches the hide-id limit in needs.hide
MAX_TEXT = 20_000       # one typed answer or one chat message, in characters
MAX_MESSAGES = 50       # Ask only uses the last 20; this is the most a client may send


class Answer(BaseModel):
    board: str = Field(max_length=MAX_ID)
    task_id: str = Field(max_length=MAX_ID)
    text: str = Field(max_length=MAX_TEXT)
    release: bool = True
    version: Optional[str] = Field(default=None, max_length=MAX_ID)   # the queue item version the human answered; stale -> 409


class Hide(BaseModel):
    id: str
    version: str


class CardAsk(BaseModel):
    id: str = Field(default="", max_length=MAX_ID)
    board: str = Field(default="", max_length=MAX_ID)
    task_id: str = Field(default="", max_length=MAX_ID)
    messages: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_MESSAGES)

    @field_validator("messages")
    @classmethod
    def _message_content_is_bounded(cls, msgs):
        for m in msgs:
            c = m.get("content") if isinstance(m, dict) else None
            if isinstance(c, str) and len(c) > MAX_TEXT:
                raise ValueError(f"message content is over {MAX_TEXT} characters")
        return msgs


def _card_ref(id_: str, board: str, task_id: str) -> tuple[str, str]:
    """Accept either the queue id (``kanban:<board>:<task>``) or board + task_id."""
    if id_:
        if id_.startswith("kanban:"):
            parts = id_.split(":", 2)
            if len(parts) == 3:
                return parts[1], parts[2]
        elif not task_id:
            task_id = id_
    return board, task_id


def _checked_card_ref(id_: str, board: str, task_id: str) -> tuple[str, str]:
    from hermes_cli import kanban_db  # type: ignore

    board, task_id = _card_ref(id_, board, task_id)
    board = str(board or "").strip()
    task_id = str(task_id or "").strip()
    if not board or not task_id:
        raise HTTPException(status_code=400, detail="board and task_id are required")
    try:
        board = _needs().normalize_board_slug(board)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not board or not kanban_db.board_exists(board):
        raise HTTPException(status_code=404, detail=f"board {board!r} does not exist")
    return board, task_id


@router.get("/queue")
def queue():
    n = _needs()
    q = n.build_queue()
    ctx = _loaded_ctx()
    if q.get("bluf_enabled") and ctx is not None and hasattr(ctx, "llm"):
        for it in q["items"]:
            if (not it.get("bluf") and it.get("source") not in getattr(n, "NO_BLUF_SOURCES", ())
                    and it.get("tier") not in ("stuck", "answered")):   # not asks: no model call each
                _queue_bluf(n, ctx, it)
    return q


# One model summary per item version, made in the background so the queue never waits.
# Lifecycle (pending / failed / retry) is jobs.BlufJobs; see the diagram there.
_BLUF_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="unbl0ck-bluf")
_BLUF_JOBS = jobs.BlufJobs()


def _queue_bluf(n: Any, ctx: Any, item: dict[str, Any]) -> None:
    key = (item["id"], item["version"])
    if _BLUF_JOBS.start(key):
        item["bluf_pending"] = True
        _BLUF_POOL.submit(_run_bluf, n, ctx, dict(item))
    elif _BLUF_JOBS.is_pending(key):
        item["bluf_pending"] = True   # still running: keep saying so on every poll
    # else failed recently: show the raw text, retry later


def _run_bluf(n: Any, ctx: Any, item: dict[str, Any]) -> None:
    key = (item["id"], item["version"])
    try:
        try:
            comments = n.recent_comments(item)
        except Exception as exc:  # noqa: BLE001 - the summary still works from the card text
            log.info("unbl0ck: comments for %s unavailable: %s", item["id"], exc)
            comments = []
        result = ctx.llm.complete(
            n.bluf_messages(item, comments), purpose="unbl0ck.bluf", temperature=0.1, max_tokens=BLUF_MAX_TOKENS, timeout=90
        )
        bluf, why = n.parse_bluf(result.text)
        n.save_bluf(item["id"], item["version"], bluf, why)
        _BLUF_JOBS.succeed(key)
    except Exception as exc:  # noqa: BLE001 - logged; the card keeps its raw text and retries later
        log.warning("unbl0ck: summary for %s failed: %s: %s", item["id"], type(exc).__name__, exc)
        _BLUF_JOBS.fail(key, f"{type(exc).__name__}: {exc}")


@router.get("/card")
def card(id: str = "", board: str = "", task_id: str = ""):
    board, task_id = _checked_card_ref(id, board, task_id)
    try:
        return _needs().card_context(board, task_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/card/ask")
def card_ask(body: CardAsk):
    n = _needs()
    if not n.ask_enabled():
        raise HTTPException(status_code=403, detail="Ask is disabled in UNBL0CK settings")
    board, task_id = _checked_card_ref(body.id, body.board, body.task_id)
    history = [m for m in body.messages if isinstance(m, dict)]
    if not any(
        m.get("role") == "user" and str(m.get("content") or "").strip() for m in history
    ):
        raise HTTPException(status_code=400, detail="messages must include a user question")
    try:
        card_data = n.card_context(board, task_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    messages = n.build_ask_messages(card_data, history)
    ctx = _loaded_ctx()
    if ctx is None or not hasattr(ctx, "llm"):
        raise HTTPException(
            status_code=503,
            detail="Ask is unavailable: the plugin host LLM is not reachable",
        )
    # The model often needs longer than the desktop's 30 s request limit, so run it in the
    # background and let the client poll GET /card/ask/{job}.
    job = uuid.uuid4().hex
    _ASK_JOBS.create(job)
    _ASK_POOL.submit(_run_ask, job, ctx, messages)
    return {"ok": True, "pending": True, "job": job}


# Lifecycle (pending -> done | error -> consumed, or expired) is jobs.AskJobs.
_ASK_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="unbl0ck-ask")
_ASK_JOBS = jobs.AskJobs()


def _run_ask(job: str, ctx: Any, messages: list[dict[str, str]]) -> None:
    try:
        result = ctx.llm.complete(
            messages,
            purpose="unbl0ck.card_ask",
            temperature=0.2,
            # Reasoning models spend part of the budget thinking before they answer; at 900
            # tokens MiniMax M3.1 Flash often ran out first and returned no text at all.
            max_tokens=ASK_MAX_TOKENS,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001 - surface the host error honestly
        log.warning("unbl0ck: ask model call failed: %s: %s", type(exc).__name__, exc)   # detail stays in the log
        _ASK_JOBS.fail(job, f"model call failed: {type(exc).__name__}")
        return
    reply = ask_reply_text(getattr(result, "text", ""))
    if not reply:
        model = getattr(result, "model", None) or "the model"
        _ASK_JOBS.fail(job, f"{model} returned no answer text (it may have used its whole budget thinking). Retry, or pick a non-reasoning model for plugins.")
        return
    _ASK_JOBS.finish(job, reply, getattr(result, "provider", None), getattr(result, "model", None))


ASK_MAX_TOKENS = 4000
# Reasoning models spend tokens thinking before the JSON; 200 cut them off mid-thought (F09).
BLUF_MAX_TOKENS = ASK_MAX_TOKENS
_THINK_RE = re.compile(r"<think(?:ing)?>.*?(?:</think(?:ing)?>|\Z)", re.S | re.I)


def ask_reply_text(text: Any) -> str:
    """The visible answer: drop inline <think> blocks some reasoning models emit."""
    return _THINK_RE.sub("", text if isinstance(text, str) else "").strip()


@router.get("/card/ask/{job}")
def card_ask_result(job: str):
    st = _ASK_JOBS.get(job)
    if st is None:
        raise HTTPException(status_code=404, detail="unknown or expired ask job")
    if st["status"] is jobs.AskState.PENDING:
        return {"ok": True, "pending": True, "job": job, "elapsed": round(time.time() - st["started"], 1)}
    try:
        st = _ASK_JOBS.consume(job)
    except KeyError:   # a concurrent poll consumed it first
        raise HTTPException(status_code=404, detail="unknown or expired ask job")
    if st["status"] is jobs.AskState.ERROR:
        raise HTTPException(status_code=502, detail=st["detail"])
    return {k: st[k] for k in ("ok", "reply", "provider", "model")}


@router.post("/kanban/answer")
def answer(body: Answer):
    from hermes_cli import kanban_db  # type: ignore

    try:
        board = _needs().normalize_board_slug(body.board)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not kanban_db.board_exists(board):
        raise HTTPException(status_code=404, detail=f"board {board!r} does not exist")
    try:
        return _needs().answer_card(board, body.task_id, body.text, release=body.release, version=body.version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/hide")
def hide(body: Hide):
    try:
        _needs().hide(body.id, body.version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@router.post("/outside/done")
def outside_done(body: Hide):
    try:
        return _needs().done_outside(body.id, body.version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except TimeoutError:
        raise HTTPException(status_code=409, detail="busy, retry")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


class Unhide(BaseModel):
    id: str


@router.post("/unhide")
def unhide(body: Unhide):
    return {"ok": True, "restored": _needs().unhide(body.id)}


@router.get("/pack")
def pack():
    n = _needs()
    return n.pocket_pack(n.build_queue())


@router.post("/pack")
def write_pack():
    n = _needs()
    q = n.build_queue()
    n.write_pocket_pack(q)
    return n.pocket_pack(q)
