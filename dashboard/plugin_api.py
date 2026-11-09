"""UNBL0CK backend routes, mounted at /api/plugins/unbl0ck/.

GET  /queue                  the queue (Kanban + Bot Mode asks)
GET  /card?id=               read-only context for one Kanban card
POST /card/ask               ask the configured model about one card (multi-turn)
POST /kanban/answer          {board, task_id, text, release}: comment as the principal, optionally unblock
POST /hide                   {id, version}: "Not for me" until the source changes
POST /unhide                 {id}: undo "Not for me"
GET  /pack                   the queue as a Pocket-format card pack (also written to plugin-data/unbl0ck)
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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


class Answer(BaseModel):
    board: str
    task_id: str
    text: str
    release: bool = True


class Hide(BaseModel):
    id: str
    version: str


class CardAsk(BaseModel):
    id: str = ""
    board: str = ""
    task_id: str = ""
    messages: list[dict[str, Any]] = []


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
        board = kanban_db._normalize_board_slug(board)
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
            if not it.get("bluf"):
                _queue_bluf(n, ctx, it)
    return q


# One model summary per item version, made in the background so the queue never waits.
_BLUF_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="unbl0ck-bluf")
_BLUF_STATE: dict[tuple[str, str], dict[str, Any]] = {}   # {"at": started, "error": str | None}
_BLUF_RETRY = 600


def _queue_bluf(n: Any, ctx: Any, item: dict[str, Any]) -> None:
    key = (item["id"], item["version"])
    st = _BLUF_STATE.get(key)
    if st and time.time() - st["at"] < _BLUF_RETRY:
        if st["error"] is None:
            item["bluf_pending"] = True   # still running: keep saying so on every poll
        return                            # failed recently: show the raw text, retry later
    if len(_BLUF_STATE) > 500:
        cutoff = time.time() - _BLUF_RETRY
        for k in [k for k, v in list(_BLUF_STATE.items()) if v["at"] < cutoff]:
            _BLUF_STATE.pop(k, None)
    _BLUF_STATE[key] = {"at": time.time(), "error": None}
    item["bluf_pending"] = True
    _BLUF_POOL.submit(_run_bluf, n, ctx, dict(item))


def _run_bluf(n: Any, ctx: Any, item: dict[str, Any]) -> None:
    key = (item["id"], item["version"])
    try:
        try:
            comments = n.recent_comments(item)
        except Exception as exc:  # noqa: BLE001 - the summary still works from the card text
            log.info("unbl0ck: comments for %s unavailable: %s", item["id"], exc)
            comments = []
        result = ctx.llm.complete(
            n.bluf_messages(item, comments), purpose="unbl0ck.bluf", temperature=0.1, max_tokens=200, timeout=90
        )
        bluf, why = n.parse_bluf(result.text)
        n.save_bluf(item["id"], item["version"], bluf, why)
        _BLUF_STATE.pop(key, None)
    except Exception as exc:  # noqa: BLE001 - logged; the card keeps its raw text and retries later
        log.warning("unbl0ck: summary for %s failed: %s: %s", item["id"], type(exc).__name__, exc)
        _BLUF_STATE[key] = {"at": time.time(), "error": f"{type(exc).__name__}: {exc}"}


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
    _prune_jobs()
    job = uuid.uuid4().hex
    _ASK_JOBS[job] = {"status": "pending", "started": time.time()}
    _ASK_POOL.submit(_run_ask, job, ctx, messages)
    return {"ok": True, "pending": True, "job": job}


_ASK_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="unbl0ck-ask")
_ASK_JOBS: dict[str, dict[str, Any]] = {}
_ASK_TTL = 900


def _prune_jobs() -> None:
    cutoff = time.time() - _ASK_TTL
    for k in [k for k, v in list(_ASK_JOBS.items()) if v.get("started", 0) < cutoff]:
        _ASK_JOBS.pop(k, None)


def _run_ask(job: str, ctx: Any, messages: list[dict[str, str]]) -> None:
    try:
        result = ctx.llm.complete(
            messages,
            purpose="unbl0ck.card_ask",
            temperature=0.2,
            max_tokens=900,
            timeout=120,
        )
        out = {
            "status": "done",
            "ok": True,
            "reply": result.text,
            "provider": getattr(result, "provider", None),
            "model": getattr(result, "model", None),
        }
    except Exception as exc:  # noqa: BLE001 - surface the host error honestly
        out = {"status": "error", "ok": False, "detail": f"model call failed: {type(exc).__name__}: {exc}"}
    if job in _ASK_JOBS:
        _ASK_JOBS[job].update(out)


@router.get("/card/ask/{job}")
def card_ask_result(job: str):
    st = _ASK_JOBS.get(job)
    if st is None:
        raise HTTPException(status_code=404, detail="unknown or expired ask job")
    if st["status"] == "pending":
        return {"ok": True, "pending": True, "job": job, "elapsed": round(time.time() - st["started"], 1)}
    _ASK_JOBS.pop(job, None)
    if st["status"] == "error":
        raise HTTPException(status_code=502, detail=st["detail"])
    return {k: st[k] for k in ("ok", "reply", "provider", "model")}


@router.post("/kanban/answer")
def answer(body: Answer):
    from hermes_cli import kanban_db  # type: ignore

    try:
        board = kanban_db._normalize_board_slug(body.board)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not kanban_db.board_exists(board):
        raise HTTPException(status_code=404, detail=f"board {board!r} does not exist")
    try:
        return _needs().answer_card(board, body.task_id, body.text, release=body.release)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/hide")
def hide(body: Hide):
    _needs().hide(body.id, body.version)
    return {"ok": True}


class Unhide(BaseModel):
    id: str


@router.post("/unhide")
def unhide(body: Unhide):
    return {"ok": True, "restored": _needs().unhide(body.id)}


@router.get("/pack")
def pack():
    n = _needs()
    q = n.build_queue()
    n.write_pocket_pack(q)
    return n.pocket_pack(q)
