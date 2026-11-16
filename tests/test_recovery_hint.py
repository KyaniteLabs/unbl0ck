"""#48: a store that recovery.py can repair names the tool when it cannot be read; other records do not."""

from r4c_common import HomeCase, needs


class RecoveryHint(HomeCase):
    def test_an_unreadable_recoverable_store_points_to_the_recovery_tool(self):
        for name in ("hidden.json", "pending-release.json", "bluf.json"):
            with self.subTest(name=name):
                p = needs.data_dir(self.home) / name
                p.write_text("{damaged")
                errors: list[str] = []
                self.assertEqual(needs._read_json(p, errors=errors), {})
                self.assertIn("recovery.py diagnose", errors[0])
                with self.assertRaisesRegex(RuntimeError, "recovery.py diagnose"):
                    needs._read_json(p, strict=True)
