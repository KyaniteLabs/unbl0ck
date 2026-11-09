"""UNBL0CK backend routes, mounted at /api/plugins/unbl0ck/.

GET  /queue                  the queue (Kanban + Bot Mode asks)
POST /kanban/answer          {board, task_id, text, release}: comment as the principal, optionally unblock
POST /hide                   {id, version}: "Not for me" until the source changes
GET  /pack                   the queue as a Pocket-format card pack (also written to plugin-data/unbl0ck)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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


router = APIRouter()


class Answer(BaseModel):
    board: str
    task_id: str
    text: str
    release: bool = True


class Hide(BaseModel):
    id: str
    version: str


@router.get("/queue")
def queue():
    return _needs().build_queue()


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
