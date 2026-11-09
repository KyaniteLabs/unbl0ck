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

        def hide(i, v):
            if not i or len(i) > 300:
                raise ValueError("bad id")
        self.needs = types.SimpleNamespace(
            NO_BLUF_SOURCES=real.NO_BLUF_SOURCES, hide=hide,
            build_queue=lambda: {"items": [dict(i) for i in items], "bluf_enabled": True, "count": 4, "errors": []})
        self.api._needs = lambda: self.needs
        self.api._loaded_ctx = lambda: types.SimpleNamespace(llm=object())
        self.api._queue_bluf = lambda n, ctx, it: self.queued.append(it["source"])
        app = fastapi.FastAPI(); app.include_router(self.api.router); self.c = TestClient(app)

    def test_queue_summarises_only_kanban_and_chats(self):
        self.assertEqual(self.c.get("/queue").status_code, 200)
        self.assertEqual(sorted(self.queued), ["chat", "kanban"])

    def test_hide_rejects_bad_id_with_400(self):
        self.assertEqual(self.c.post("/hide", json={"id": "x" * 5000, "version": "1"}).status_code, 400)
        self.assertEqual(self.c.post("/hide", json={"id": "", "version": "1"}).status_code, 400)
        self.assertEqual(self.c.post("/hide", json={"id": "chat:p:s", "version": "1"}).json(), {"ok": True})


if __name__ == "__main__":
    unittest.main()
