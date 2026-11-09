"""UNBL0CK backend routes, mounted at /api/plugins/unbl0ck/.

GET  /queue                  the queue (Kanban + Bot Mode asks)
GET  /card?id=               read-only context for one Kanban card
POST /card/ask               ask the configured model about one card (multi-turn)
POST /kanban/answer          {board, task_id, text, release}: comment as the principal, optionally unblock
POST /hide                   {id, version}: "Not for me" until the source changes
GET  /pack                   the queue as a Pocket-format card pack (also written to plugin-data/unbl0ck)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

PLUGIN_DIR = Path(__file__).resolve().parent.parent
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
    return _needs().build_queue()


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
    try:
        result = ctx.llm.complete(
            messages,
            purpose="unbl0ck.card_ask",
            temperature=0.2,
            max_tokens=900,
            timeout=90,
        )
    except Exception as exc:  # noqa: BLE001 - surface the host error honestly
        raise HTTPException(
            status_code=502,
            detail=f"model call failed: {type(exc).__name__}: {exc}",
        )
    return {
        "ok": True,
        "reply": result.text,
        "provider": getattr(result, "provider", None),
        "model": getattr(result, "model", None),
    }


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


@router.get("/pack")
def pack():
    n = _needs()
    q = n.build_queue()
    n.write_pocket_pack(q)
    return n.pocket_pack(q)
