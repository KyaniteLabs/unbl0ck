"""Backend routes from the v0.4.1 audit: a bad hide id is a 400 (not a 500), and the queue never
asks the model to summarise a live prompt or a pairing request."""
import importlib.util, sys, types, unittest
from pathlib import Path

try:
    import fastapi
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - plain CI without the Hermes venv
    fastapi = None

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(fastapi is None, "fastapi not installed")
class ApiQA(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("ub_api_qa", ROOT / "dashboard/plugin_api.py")
        self.api = importlib.util.module_from_spec(spec); sys.modules["ub_api_qa"] = self.api
        spec.loader.exec_module(self.api)
        nspec = importlib.util.spec_from_file_location("ub_needs_api_qa", ROOT / "needs.py")
        real = importlib.util.module_from_spec(nspec); nspec.loader.exec_module(real)
        self.queued = []
        items = [{"id": f"{s}:1", "version": "1", "source": s} for s in ("prompt", "pairing", "kanban", "chat")]
        items.append({"id": "kanban:2", "version": "1", "source": "kanban", "tier": "stuck"})
        self.written = []
        self.answers = []

        def hide(i, v):
            if not i or len(i) > 300:
                raise ValueError("bad id")
        self.needs = types.SimpleNamespace(
            NO_BLUF_SOURCES=real.NO_BLUF_SOURCES, hide=hide, StaleAnswer=real.StaleAnswer,
            pocket_pack=lambda q: {"cards": len(q["items"])},
            write_pocket_pack=lambda q: self.written.append(q),
            answer_card=self._answer,
            build_queue=lambda: {"items": [dict(i) for i in items], "bluf_enabled": True, "count": 5, "errors": []})
        self.api._needs = lambda: self.needs
        self.api._loaded_ctx = lambda: types.SimpleNamespace(llm=object())
        self.api._queue_bluf = lambda n, ctx, it: self.queued.append(it["source"])
        app = fastapi.FastAPI(); app.include_router(self.api.router); self.c = TestClient(app)

    def _answer(self, board, task_id, text, *, release, version=None):
        self.answers.append(version)
        if version == "old":
            raise self.needs.StaleAnswer("blocked again")
        return {"ok": True, "comment_id": 1, "released": release}

    def test_queue_summarises_only_kanban_and_chats(self):
        self.assertEqual(self.c.get("/queue").status_code, 200)
        self.assertEqual(sorted(self.queued), ["chat", "kanban"])   # never the stuck card

    def test_get_pack_writes_nothing_post_pack_writes(self):
        self.assertEqual(self.c.get("/pack").json(), {"cards": 5})
        self.assertEqual(self.written, [])
        self.assertEqual(self.c.post("/pack").json(), {"cards": 5})
        self.assertEqual(len(self.written), 1)

    def test_answer_carries_the_version_and_stale_is_409(self):
        kdb = types.ModuleType("hermes_cli.kanban_db")
        kdb.board_exists = lambda b: True
        pkg = types.ModuleType("hermes_cli"); pkg.kanban_db = kdb
        saved = {k: sys.modules.get(k) for k in ("hermes_cli", "hermes_cli.kanban_db")}
        sys.modules.update({"hermes_cli": pkg, "hermes_cli.kanban_db": kdb})
        try:
            body = {"board": "b", "task_id": "t", "text": "go", "release": True}
            self.assertEqual(self.c.post("/kanban/answer", json={**body, "version": "11"}).status_code, 200)
            self.assertEqual(self.c.post("/kanban/answer", json={**body, "version": "old"}).status_code, 409)
            self.assertEqual(self.c.post("/kanban/answer", json=body).status_code, 200)
            self.assertEqual(self.answers, ["11", "old", None])
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_hide_rejects_bad_id_with_400(self):
        self.assertEqual(self.c.post("/hide", json={"id": "x" * 5000, "version": "1"}).status_code, 400)
        self.assertEqual(self.c.post("/hide", json={"id": "", "version": "1"}).status_code, 400)
        self.assertEqual(self.c.post("/hide", json={"id": "chat:p:s", "version": "1"}).json(), {"ok": True})


if __name__ == "__main__":
    unittest.main()
