"""Integration checks against the real Hermes Kanban implementation."""

import importlib
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
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


@unittest.skipIf(
    _HERMES_IMPORT_ERROR, f"Hermes dependency unavailable: {_HERMES_IMPORT_ERROR}"
)
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
                    "default",
                    task_id,
                    "Use the first option",
                    release=True,
                    version=version,
                    home=Path(directory),
                )
                self.assertTrue(result["released"])
                with kanban_db_connect.connect(board="default") as conn:
                    self.assertNotEqual(
                        kanban_db.get_task(conn, task_id).status, "blocked"
                    )
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
                    retry_version = str(
                        needs._latest_block(conn, kanban_db, retry_id).id
                    )

                needs.answer_card(
                    "default",
                    retry_id,
                    "Keep the current path",
                    release=False,
                    version=retry_version,
                    home=Path(directory),
                )
                retry_result = needs.answer_card(
                    "default",
                    retry_id,
                    "Keep the current path",
                    release=True,
                    version=retry_version,
                    home=Path(directory),
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

    def test_stale_answer_cannot_release_a_newer_block(self):
        previous_home = os.environ.get("HERMES_HOME")
        with tempfile.TemporaryDirectory() as directory:
            os.environ["HERMES_HOME"] = directory
            try:
                with kanban_db_connect.connect(board="default") as conn:
                    task_id = kanban_db.create_task(
                        conn, title="Choose a release path", body="", assignee="worker"
                    )
                    task_id = getattr(task_id, "id", task_id)
                    kanban_db.block_task(
                        conn,
                        task_id,
                        reason="Needs operator decision",
                        kind="needs_input",
                    )
                    old_version = str(needs._latest_block(conn, kanban_db, task_id).id)

                original_write_txn = kanban_db.write_txn
                top_level_txns = 0
                interleaved = False
                new_version = None

                @contextmanager
                def interleave_after_second_txn(conn, *, allow_nested=False):
                    nonlocal top_level_txns, interleaved, new_version
                    top_level = not conn.in_transaction
                    if top_level:
                        top_level_txns += 1
                    with original_write_txn(conn, allow_nested=allow_nested):
                        yield conn
                    if top_level and top_level_txns == 2 and not interleaved:
                        interleaved = True
                        with kanban_db_connect.connect(board="default") as other:
                            self.assertTrue(
                                kanban_db.block_task(
                                    other,
                                    task_id,
                                    reason="Needs a new operator decision",
                                    kind="capability",
                                )
                            )
                            new_version = str(
                                needs._latest_block(other, kanban_db, task_id).id
                            )

                kanban_db.write_txn = interleave_after_second_txn
                try:
                    result = needs.answer_card(
                        "default",
                        task_id,
                        "Use the earlier option",
                        release=True,
                        version=old_version,
                        home=Path(directory),
                    )
                finally:
                    kanban_db.write_txn = original_write_txn

                self.assertTrue(result["released"])
                self.assertTrue(interleaved)
                self.assertNotEqual(old_version, new_version)
                with kanban_db_connect.connect(board="default") as conn:
                    task = kanban_db.get_task(conn, task_id)
                    self.assertEqual(
                        str(getattr(task.status, "value", task.status)), "blocked"
                    )
                    comments = kanban_db.list_comments(conn, task_id)
                    self.assertEqual(
                        [comment.body for comment in comments],
                        ["Use the earlier option"],
                    )
            finally:
                if previous_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = previous_home

    def test_release_preserves_parent_gating_and_review_resume_phase(self):
        previous_home = os.environ.get("HERMES_HOME")
        with tempfile.TemporaryDirectory() as directory:
            os.environ["HERMES_HOME"] = directory
            try:
                with kanban_db_connect.connect(board="default") as conn:
                    parent_id = kanban_db.create_task(
                        conn, title="Complete upstream work", body="", assignee="worker"
                    )
                    parent_id = getattr(parent_id, "id", parent_id)
                    child_id = kanban_db.create_task(
                        conn, title="Resume dependent work", body="", assignee="worker"
                    )
                    child_id = getattr(child_id, "id", child_id)
                    kanban_db.link_tasks(conn, parent_id, child_id)
                    with kanban_db.write_txn(conn):
                        conn.execute(
                            "UPDATE tasks SET status = 'ready' WHERE id = ?",
                            (child_id,),
                        )
                    self.assertTrue(
                        kanban_db.block_task(
                            conn,
                            child_id,
                            reason="Needs operator decision",
                            kind="needs_input",
                        )
                    )
                    child_version = str(
                        needs._latest_block(conn, kanban_db, child_id).id
                    )

                    review_id = kanban_db.create_task(
                        conn, title="Resume review work", body="", assignee="worker"
                    )
                    review_id = getattr(review_id, "id", review_id)
                    with kanban_db.write_txn(conn):
                        conn.execute(
                            "UPDATE tasks SET status = 'ready' WHERE id = ?",
                            (review_id,),
                        )
                    self.assertTrue(
                        kanban_db.block_task(
                            conn,
                            review_id,
                            reason="Needs operator decision",
                            kind="needs_input",
                        )
                    )
                    review_event = needs._latest_block(conn, kanban_db, review_id)
                    conn.execute(
                        "UPDATE task_events SET payload = ? WHERE id = ?",
                        (
                            '{"kind":"needs_input","resume_status":"review"}',
                            review_event.id,
                        ),
                    )
                    review_version = str(review_event.id)

                needs.answer_card(
                    "default",
                    child_id,
                    "Resume after parent completes",
                    release=True,
                    version=child_version,
                    home=Path(directory),
                )
                needs.answer_card(
                    "default",
                    review_id,
                    "Continue review",
                    release=True,
                    version=review_version,
                    home=Path(directory),
                )
                with kanban_db_connect.connect(board="default") as conn:
                    self.assertEqual(
                        str(
                            getattr(
                                kanban_db.get_task(conn, child_id).status,
                                "value",
                                kanban_db.get_task(conn, child_id).status,
                            )
                        ),
                        "todo",
                    )
                    self.assertEqual(
                        str(
                            getattr(
                                kanban_db.get_task(conn, review_id).status,
                                "value",
                                kanban_db.get_task(conn, review_id).status,
                            )
                        ),
                        "review",
                    )
            finally:
                if previous_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = previous_home


if __name__ == "__main__":
    unittest.main()
