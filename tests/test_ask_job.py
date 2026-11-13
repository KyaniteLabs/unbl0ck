"""Ask runs as a background job so the desktop's 30 s request limit never cuts it off."""

import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path

try:
    import fastapi
    from fastapi.testclient import TestClient
except Exception:  # noqa: BLE001 - optional API test dependencies may fail to import
    fastapi = None


@unittest.skipIf(fastapi is None, "fastapi not installed")
class AskJobTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "ub_api", Path(__file__).parents[1] / "dashboard/plugin_api.py"
        )
        self.api = importlib.util.module_from_spec(spec)
        sys.modules["ub_api"] = self.api
        spec.loader.exec_module(self.api)
        needs = types.SimpleNamespace(
            ask_enabled=lambda: True,
            card_context=lambda b, t: {"id": t},
            build_ask_messages=lambda c, h: list(h),
        )
        self.api._needs = lambda: needs
        self.api._checked_card_ref = lambda i, b, t: ("factory", "t_1")

        def complete(messages, **kw):
            time.sleep(0.3)
            return types.SimpleNamespace(text="Pick A", provider="p", model="m")

        self.api._loaded_ctx = lambda: types.SimpleNamespace(
            llm=types.SimpleNamespace(complete=complete)
        )
        app = fastapi.FastAPI()
        app.include_router(self.api.router)
        self.c = TestClient(app)

    def test_post_returns_job_then_poll_gets_reply(self):
        r = self.c.post(
            "/card/ask",
            json={"id": "x", "messages": [{"role": "user", "content": "which?"}]},
        ).json()
        self.assertTrue(r["pending"])
        job = r["job"]
        for _ in range(40):
            p = self.c.get(f"/card/ask/{job}").json()
            if not p.get("pending"):
                break
            time.sleep(0.05)
        self.assertEqual(p["reply"], "Pick A")
        self.assertEqual(self.c.get(f"/card/ask/{job}").json(), p)  # safe to retry a lost response

    def _ask_with(self, text):
        seen = {}

        def complete(messages, **kw):
            seen.update(kw)
            return types.SimpleNamespace(
                text=text, provider="p", model="MiniMax-M3.1-Flash"
            )

        self.api._loaded_ctx = lambda: types.SimpleNamespace(
            llm=types.SimpleNamespace(complete=complete)
        )
        job = self.c.post(
            "/card/ask",
            json={"id": "x", "messages": [{"role": "user", "content": "TL;DR"}]},
        ).json()["job"]
        for _ in range(40):
            r = self.c.get(f"/card/ask/{job}")
            if not r.json().get("pending"):
                break
            time.sleep(0.05)
        return r, seen

    def test_reasoning_models_get_room_to_answer(self):
        _, seen = self._ask_with("Unblock it.")
        self.assertGreaterEqual(seen["max_tokens"], 4000)

    def test_inline_thinking_is_not_shown(self):
        r, _ = self._ask_with(
            "<think>long musing</think>\nUnblock it: the fix is merged."
        )
        self.assertEqual(r.json()["reply"], "Unblock it: the fix is merged.")

    def test_no_answer_text_is_an_honest_error(self):
        for text in ("", "   ", "<think>ran out of budget mid-thought"):
            r, _ = self._ask_with(text)
            self.assertEqual(r.status_code, 502)
            self.assertIn("returned no answer text", r.json()["detail"])

    def test_poll_envelope_distinguishes_model_failure_from_transport_failure(self):
        self.api._ASK_JOBS.create("failed")
        self.api._ASK_JOBS.fail("failed", "model call failed: ValueError")
        self.assertEqual(self.c.get("/card/ask/failed").status_code, 502)
        first = self.c.get("/card/ask/failed?poll=true")
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["ok"])
        self.assertEqual(self.c.get("/card/ask/failed?poll=true").json(), first.json())

    def test_retention_is_bounded_without_evicting_pending_work(self):
        table = self.api._ASK_JOBS
        table.create("pending")
        for i in range(table.MAX_SETTLED + 5):
            table.create(str(i))
            table.finish(str(i), "A")
        self.assertEqual(len(table), table.MAX_SETTLED + 1)
        self.assertIsNotNone(table.get("pending"))
        self.assertIsNone(table.get("0"))
        self.assertEqual(table.get(str(table.MAX_SETTLED + 4))["reply"], "A")


if __name__ == "__main__":
    unittest.main()
