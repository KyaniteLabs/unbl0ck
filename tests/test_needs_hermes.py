"""Integration checks against the real Hermes Kanban implementation."""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERMES_SOURCE = os.environ.get("UNBL0CK_HERMES_SOURCE")
if HERMES_SOURCE:
    sys.path.insert(0, HERMES_SOURCE)
sys.path.insert(0, str(ROOT))

import needs

needs._settings = lambda: {"principal_name": "CEO", "comment_author": "operator"}


try:
    kanban_db = importlib.import_module("hermes_cli.kanban_db")
    kanban_db_connect = importlib.import_module("hermes_cli.kanban_db_connect")
except ImportError as exc:
    _HERMES_IMPORT_ERROR = str(exc)
else:
    _HERMES_IMPORT_ERROR = None


@unittest.skipIf(_HERMES_IMPORT_ERROR, f"Hermes dependency unavailable: {_HERMES_IMPORT_ERROR}")
class RealHermesRelease(unittest.TestCase):
    def test_answer_release_uses_native_transaction_boundary_and_posts_once(self):
        previous_home = os.environ.get("HERMES_HOME")
        with tempfile.TemporaryDirectory() as directory:
            os.environ["HERMES_HOME"] = directory
            try:
                with kanban_db_connect.connect(board="default") as conn:
                    task_id = kanban_db.create_task(
                        conn, title="Choose a database", body="", assignee="worker"
                    )
                    task_id = getattr(task_id, "id", task_id)
                    kanban_db.block_task(
                        conn, task_id, reason="Needs CEO approval", kind="needs_input"
                    )
                    block = needs._latest_block(conn, kanban_db, task_id)
                    version = str(block.id)

                result = needs.answer_card(
                    "default", task_id, "Use the first option", release=True,
                    version=version, home=Path(directory),
                )
                self.assertTrue(result["released"])
                with kanban_db_connect.connect(board="default") as conn:
                    self.assertNotEqual(kanban_db.get_task(conn, task_id).status, "blocked")
                    comments = kanban_db.list_comments(conn, task_id)
                    self.assertEqual(
                        [(comment.author, comment.body) for comment in comments],
                        [("operator", "Use the first option")],
                    )

                    retry_id = kanban_db.create_task(
                        conn, title="Choose a retry path", body="", assignee="worker"
                    )
                    retry_id = getattr(retry_id, "id", retry_id)
                    kanban_db.block_task(
                        conn, retry_id, reason="Needs CEO approval", kind="needs_input"
                    )
                    retry_version = str(needs._latest_block(conn, kanban_db, retry_id).id)

                needs.answer_card(
                    "default", retry_id, "Keep the current path", release=False,
                    version=retry_version, home=Path(directory),
                )
                retry_result = needs.answer_card(
                    "default", retry_id, "Keep the current path", release=True,
                    version=retry_version, home=Path(directory),
                )
                self.assertTrue(retry_result["released"])
                with kanban_db_connect.connect(board="default") as conn:
                    comments = kanban_db.list_comments(conn, retry_id)
                    self.assertEqual(
                        [(comment.author, comment.body) for comment in comments],
                        [("operator", "Keep the current path")],
                    )
            finally:
                if previous_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = previous_home


if __name__ == "__main__":
    unittest.main()
