"""Drive the generated Desktop card over HTTP against real Hermes Kanban state."""

import socket
import subprocess
from threading import Thread

from test_operator_recovery import HARNESS, ROOT


RELEASE_FLOW = r"""
const base = process.argv[2]
const taskId = process.argv[3]
let failureDetail
const ctx = {
  rest: async (path, options = {}) => {
    const response = await fetch(base + path, {
      method: options.method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(options.body),
      signal: AbortSignal.timeout(10000)
    })
    const body = await response.text()
    if (!response.ok) {
      failureDetail = JSON.parse(body).detail
      throw new Error("Error invoking remote method 'hermes:api': Error: " + response.status + ": " + body)
    }
    return JSON.parse(body)
  }
}
let current
const reload = async () => {
  const queue = await ctx.rest('/queue')
  current = queue.items.find(card => card.task_id === taskId)
}
const render = () => {
  cursor = 0
  return Item({ ctx, item: current, n: 1, refresh: reload, askEnabled: false })
}

;(async () => {
  await reload()
  assert.equal(current?.tier, 'ask')
  all(render()).find(node => node?.type === 'textarea').props.onChange({ target: { value: 'Blue' } })
  await find(render(), 'Answer + unbl0ck').onClick()
  assert.equal(current?.tier, 'release_failed')
  const alert = all(render()).find(node => node?.props?.role === 'alert')
  assert.match(failureDetail, /answer was posted.*could not be unblocked/i)
  assert.equal(alert?.props.children, failureDetail)
  await find(render(), 'Release (unbl0ck)').onClick()
  assert.equal(current, undefined, 'the refreshed queue must remove the released card')
})().catch(error => { console.error(error); process.exitCode = 1 })
"""


def test_desktop_shows_backend_release_failure_and_retries_without_duplicate_answer(tmp_path, monkeypatch):
    import uvicorn
    from fastapi import FastAPI
    from hermes_cli import kanban_db, kanban_db_connect

    from dashboard import plugin_api

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setattr(plugin_api._needs(), "_settings", lambda: {
        "principal_name": "operator", "comment_author": "operator", "bluf_enabled": False,
    })
    with kanban_db_connect.connect(board="default") as conn:
        task = kanban_db.create_task(conn, title="Choose a color", body="", assignee="worker")
        task_id = getattr(task, "id", task)
        assert kanban_db.block_task(conn, task_id, reason="Needs operator approval", kind="needs_input")

    unblock = kanban_db.unblock_task
    refused = False

    def refuse_once(conn, card):
        nonlocal refused
        if not refused:
            refused = True
            return False
        return unblock(conn, card)

    monkeypatch.setattr(kanban_db, "unblock_task", refuse_once)
    app = FastAPI()
    prefix = "/api/plugins/unbl0ck"
    app.include_router(plugin_api.router, prefix=prefix)
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", ws="none", log_level="error"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        url = f"http://127.0.0.1:{listener.getsockname()[1]}{prefix}"
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            result = subprocess.run(
                ["node", "-e", HARNESS + RELEASE_FLOW, str(ROOT / "desktop/plugin.js"), url, task_id],
                capture_output=True, text=True, timeout=30, check=False,
            )
        finally:
            server.should_exit = True
            thread.join(timeout=5)
        assert not thread.is_alive(), "the local HTTP test server did not exit"
    assert result.returncode == 0, result.stdout + result.stderr
    with kanban_db_connect.connect(board="default") as conn:
        assert kanban_db.get_task(conn, task_id).status == "ready"
        assert [(entry.author, entry.body) for entry in kanban_db.list_comments(conn, task_id)] == [
            ("operator", "Blue"),
        ]
