"""Round-5 findings: the approve line, the Ask body without messages, and the Pocket prompt budget."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path

from test_outside import needs
from test_r4_stack01d_ask import kanban_item

try:
    import fastapi
    from fastapi.testclient import TestClient
except Exception:  # noqa: BLE001 - optional API test dependencies may fail to import
    fastapi = None

ROOT = Path(__file__).resolve().parents[1]
RID = "a1b2c3d4e5f60718"


def load_pkg(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    pkg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pkg)
    return pkg


class AnApproveCommandIsShownWholeOrNotAtAll(unittest.TestCase):
    def summary(self, command):
        pkg = load_pkg("unbl0ck_pkg_r5_g4")
        q = {
            "count": 1,
            "errors": [],
            "items": [
                {
                    "source": "pairing",
                    "platform": "telegram",
                    "title": "Ana wants to talk",
                    "ask": "x",
                    "approve_command": command,
                }
            ],
        }
        pkg._needs = lambda: types.SimpleNamespace(build_queue=lambda: q, _clean=needs._clean)
        return pkg._summary()

    def test_the_longest_valid_command_is_not_cut(self):
        """R5-jobs-024 (class K): a command cut at 200 characters is a different command."""
        command = f"hermes -p {'p' * 250} pairing approve telegram {RID}"
        self.assertGreater(len(command), 300)
        self.assertIn(f"  approve: {command}", self.summary(command).split("\n"))

    def test_a_command_too_long_to_show_is_replaced_by_a_notice(self):
        command = "hermes pairing approve telegram " + "x" * 500
        out = self.summary(command)
        self.assertNotIn("xxxxxxxxxx", out)
        self.assertIn("use the Desktop card", out)


@unittest.skipIf(fastapi is None, "fastapi not installed")
class AnAskBodyWithoutMessagesIsRefused(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("ub_api_r5_g4", ROOT / "dashboard/plugin_api.py")
        self.api = importlib.util.module_from_spec(spec)
        sys.modules["ub_api_r5_g4"] = self.api
        spec.loader.exec_module(self.api)
        self.reads = []

        def card_context(board, task_id):
            self.reads.append((board, task_id))
            raise LookupError("no such card")

        self.api._needs = lambda: types.SimpleNamespace(
            ask_enabled=lambda: True,
            normalize_board_slug=needs.normalize_board_slug,
            card_context=card_context,
        )
        self.api._loaded_ctx = lambda: types.SimpleNamespace(llm=object())
        app = fastapi.FastAPI()
        app.include_router(self.api.router)
        self.client = TestClient(app)

    def test_leaving_messages_out_is_refused_like_an_empty_list(self):
        """R5-api-006 (class M): a default skips validation, so the body reached the card read."""
        empty = self.client.post("/card/ask", json={"board": "default", "task_id": "t", "messages": []})
        self.assertEqual(empty.status_code, 422)
        missing = self.client.post("/card/ask", json={"board": "default", "task_id": "t"})
        self.assertEqual(missing.status_code, 422)
        self.assertEqual(self.reads, [])


class ThePocketPackKeepsItsPromptBudget(unittest.TestCase):
    def test_a_cut_prompt_plus_its_label_stays_within_the_total(self):
        """R5-needs-045 (class D): the label was added after the budgeted cut."""
        items = [kanban_item(id=f"kanban:ops:t_{i}", task_id=f"t_{i}", ask="Q" * 5000) for i in range(6)]
        pack = needs.pocket_pack({"items": items}, limit=6)
        total = sum(len(c["prompt"]) for c in pack["cards"])
        self.assertLessEqual(total, needs.POCKET_TOTAL_PROMPT_CHARS)
        for card in pack["cards"]:
            if len(card["prompt"]) < 5000:
                self.assertTrue(card["truncated"] and card["warning"], card)


if __name__ == "__main__":
    unittest.main()
