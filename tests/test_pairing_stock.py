"""The plugin against the REAL stock PairingStore (skipped where gateway.pairing is absent):
a request the gateway generated is listed with its admin request id, never its code, and
profile stores are reached the way ``hermes -p <name> pairing`` reaches them."""
import importlib.util, json, os, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("unbl0ck_needs_stock", ROOT / "needs.py")
needs = importlib.util.module_from_spec(spec); spec.loader.exec_module(needs)
needs._settings = lambda: {"principal_name": "Ada"}

try:
    import gateway.pairing as stock
except Exception:   # noqa: BLE001 - outside a Hermes install
    stock = None


@unittest.skipIf(stock is None, "stock Hermes gateway.pairing not importable")
class RealStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.saved = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self.saved
        self.tmp.cleanup()

    def test_lists_request_id_never_code(self):
        code = stock.PairingStore(profile="default").generate_code("telegram", "998877", "Ana")
        self.assertTrue(code)
        it, = needs.pairing_items(self.home)
        rid, = [r["request_id"] for r in stock.PairingStore(profile="default").list_pending()]
        self.assertEqual(it["approve_command"], f"hermes pairing approve telegram {rid}")
        blob = json.dumps(it)
        self.assertNotIn(code, blob)
        self.assertNotIn("998877", blob)
        self.assertIn("Ana", it["title"])

    def test_profile_store(self):
        (self.home / "profiles" / "ops").mkdir(parents=True)
        code = stock.PairingStore(profile="ops").generate_code("discord", "42", "Bo")
        self.assertTrue(code)
        it, = needs.pairing_items(self.home)
        self.assertTrue(it["approve_command"].startswith("hermes -p ops pairing approve discord "))
        self.assertNotIn(code, json.dumps(it))
