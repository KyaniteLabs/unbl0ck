"""Public-host API contract; SQLite models the host's non-nestable transactions."""
import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import needs


class PublicUnblockContract(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TABLE state (status TEXT)')
        self.conn.execute("INSERT INTO state VALUES ('blocked')")
        self.version = '1'
        self.calls = 0
        self.host = SimpleNamespace(write_txn=self.txn, get_task=self.task,
                                    unblock_task=self.unblock)

    @contextmanager
    def txn(self, conn, **kwargs):
        if conn.in_transaction:
            raise RuntimeError('nested transaction forbidden')
        conn.execute('BEGIN IMMEDIATE')
        try:
            yield
        except Exception:
            conn.execute('ROLLBACK')
            raise
        else:
            conn.execute('COMMIT')

    def task(self, conn, task_id):
        return SimpleNamespace(status=conn.execute('SELECT status FROM state').fetchone()[0])

    def unblock(self, conn, task_id):
        self.calls += 1
        with self.txn(conn):
            conn.execute("UPDATE state SET status='ready'")
        return True

    def release(self, version):
        with patch.object(needs, '_latest_block', return_value=SimpleNamespace(id=self.version)), \
             patch.object(needs, '_block_since', return_value=0):
            return needs._unblock_task_at_version(self.conn, self.host, 'card', version)

    def test_public_helper_owns_release_transaction(self):
        self.assertTrue(self.release('1'))
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.task(self.conn, 'card').status, 'ready')
        self.assertFalse(self.conn.in_transaction)

    def test_public_transition_failure_does_not_leave_transaction_open(self):
        def fail(conn, task_id):
            with self.txn(conn):
                conn.execute("UPDATE state SET status='ready'")
                raise RuntimeError('host failure')
        self.host.unblock_task = fail
        with self.assertRaisesRegex(RuntimeError, 'host failure'):
            self.release('1')
        self.assertEqual(self.task(self.conn, 'card').status, 'blocked')
        self.assertFalse(self.conn.in_transaction)


class PublicWebhookInstructions(unittest.TestCase):
    def test_private_installer_is_not_shipped(self):
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / 'scripts/install_security_review_route.py').exists())
        instructions = (root / 'docs/security-review-link.md').read_text()
        self.assertIn('hermes webhook subscribe', instructions)
        self.assertIn('--route-profile security', instructions)
        self.assertTrue((root / 'docs/security-review-prompt.txt').is_file())


if __name__ == '__main__':
    unittest.main()
