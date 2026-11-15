"""Round 4 for host acceptance (issue #44).

Real Hermes modules are used wherever the interface is checked against the installed host; the
loader stand-ins only model a host that lacks or fails to resolve a name. Tests that create a
Kanban board isolate HERMES_KANBAN_HOME and HERMES_HOME in a temporary directory.
"""

import ast
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from test_needs import needs  # noqa: E402
from test_host_compat import HOST_ROOTS, full_module, source_files, used_names  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

try:
    kanban_db = importlib.import_module("hermes_cli.kanban_db")
    importlib.import_module("hermes_cli.kanban_db_connect")
except Exception:  # noqa: BLE001
    HERMES = False
else:
    HERMES = True


def loader_without(**drops):
    """A loader whose modules hold every listed name except those dropped per module."""

    def load(module):
        mod = full_module()
        for name in drops.get(module.replace(".", "_"), ()):
            head, _, tail = name.partition(".")
            if tail:
                setattr(mod, head, types.SimpleNamespace())
            else:
                delattr(mod, head)
        return mod

    return load


class InventoryIsComplete(unittest.TestCase):
    def test_every_shipped_file_that_imports_hermes_is_scanned(self):
        scanned = {p.resolve() for p in source_files()}
        shipped = [
            p
            for p in ROOT.rglob("*.py")
            if not {"tests", "node_modules", "worktrees", ".git"} & set(p.relative_to(ROOT).parts)
            and p.name != "host_compat.py"
        ]
        missed = []
        for p in shipped:
            for node in ast.walk(ast.parse(p.read_text())):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in HOST_ROOTS:
                    if p.resolve() not in scanned:
                        missed.append(p.name)
        self.assertEqual(sorted(set(missed)), [])

    def test_the_pairing_store_and_the_installer_helpers_are_listed(self):
        self.assertIn("PairingStore", needs.HOST_API["gateway.pairing"])
        self.assertIn("PairingStore.list_pending", needs.HOST_API["gateway.pairing"])
        self.assertEqual(
            set(needs.HOST_API["hermes_cli.webhook"]), {"_mutate_subscriptions", "_subscriptions_path"}
        )
        self.assertEqual(set(needs.HOST_API["hermes_cli.profiles"]), {"profile_exists"})

    def test_a_missing_pairing_store_makes_the_host_incompatible(self):
        r = needs.host_compat(loader_without(gateway_pairing=["PairingStore"]))
        self.assertFalse(r["ok"])
        self.assertIn("gateway.pairing.PairingStore", r["missing"])

    def test_a_pairing_store_without_list_pending_makes_the_host_incompatible(self):
        r = needs.host_compat(loader_without(gateway_pairing=["PairingStore.list_pending"]))
        self.assertFalse(r["ok"])
        self.assertEqual(r["missing"], ["gateway.pairing.PairingStore.list_pending"])

    def test_a_removed_webhook_helper_is_named(self):
        r = needs.host_compat(loader_without(hermes_cli_webhook=["_mutate_subscriptions"]))
        self.assertEqual(r["missing"], ["hermes_cli.webhook._mutate_subscriptions"])

    @unittest.skipUnless(HERMES, "Hermes is not importable here")
    def test_the_real_pairing_store_and_webhook_helpers_are_present(self):
        r = needs.host_compat()
        self.assertEqual(r["missing"], [], r)
        self.assertEqual(r["checked"], sum(len(n) for n in needs.HOST_API.values()))


class ACommentIsNotADependency(unittest.TestCase):
    def test_the_board_slug_comment_is_not_counted_as_usage(self):
        text = (ROOT / "needs.py").read_text()
        self.assertIn("kanban_db._normalize_board_slug", text)  # still named in a comment
        self.assertNotIn("_normalize_board_slug", used_names().get("hermes_cli.kanban_db", set()))
        self.assertNotIn("_normalize_board_slug", needs.HOST_API["hermes_cli.kanban_db"])

    @unittest.skipUnless(HERMES, "Hermes is not importable here")
    def test_a_hermes_without_its_own_slug_helper_is_still_compatible(self):

        def load(module):
            mod = importlib.import_module(module)
            if module == "hermes_cli.kanban_db":
                data = {k: v for k, v in vars(mod).items() if k != "_normalize_board_slug"}
                return types.SimpleNamespace(**data)
            return mod

        r = needs.host_compat(load)
        self.assertTrue(r["ok"], r)
        self.assertEqual(needs.normalize_board_slug(" Team-1 "), "team-1")


class LazyAttributeFailure(unittest.TestCase):
    def lazy_loader(self):
        class Lazy:
            def __getattr__(self, name):
                if name == "list_tasks":
                    raise ImportError("a dependency of list_tasks is unavailable")
                return 1

        def load(module):
            return Lazy() if module == "hermes_cli.kanban_db" else full_module()

        return load

    def test_an_attribute_that_raises_on_resolution_is_reported_missing(self):
        r = needs.host_compat(self.lazy_loader())
        self.assertFalse(r["ok"])
        self.assertEqual(r["missing"], ["hermes_cli.kanban_db.list_tasks"])

    def test_the_queue_still_reads_the_healthy_sources_when_the_host_check_hits_one(self):
        real_import = importlib.import_module
        lazy = self.lazy_loader()

        def fake_import(name, *a, **k):
            return lazy(name) if name.startswith("hermes_") else real_import(name, *a, **k)

        calls = []

        def chat_reader(home, **kw):
            calls.append(1)
            return []

        def boom(boards=None, errors=None):
            raise AttributeError("module 'hermes_cli.kanban_db' has no attribute 'list_tasks'")

        with tempfile.TemporaryDirectory() as d, patch.object(needs, "kanban_items", boom), \
                patch.object(importlib, "import_module", fake_import), \
                patch.object(needs, "chat_items", chat_reader), \
                patch.object(needs, "_settings", lambda: {"principal_name": "Ada"}):
            q = needs.build_queue(Path(d))
        self.assertEqual(calls, [1])
        self.assertTrue(any(e.startswith("host: this Hermes lacks 1 of") for e in q["errors"]), q["errors"])

    def test_a_failing_host_report_never_escapes_the_queue(self):
        def boom(boards=None, errors=None):
            raise RuntimeError("kanban down")

        calls = []
        with tempfile.TemporaryDirectory() as d, patch.object(needs, "kanban_items", boom), \
                patch.object(needs, "host_compat", side_effect=RuntimeError("probe failed")), \
                patch.object(needs, "chat_items", lambda *a, **k: calls.append(1) or []), \
                patch.object(needs, "_settings", lambda: {"principal_name": "Ada"}):
            q = needs.build_queue(Path(d))
        self.assertEqual(calls, [1])
        self.assertTrue(any(e.startswith("kanban: RuntimeError") for e in q["errors"]))


@unittest.skipUnless(HERMES, "Hermes is not importable here")
class ReaderSwallowsTheFailure(unittest.TestCase):
    """The real kanban_items records ``kanban[board]: ...`` itself and returns normally."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = patch.dict(os.environ, {"HERMES_KANBAN_HOME": self._tmp.name, "HERMES_HOME": self._tmp.name})
        p.start()
        self.addCleanup(p.stop)
        p2 = patch.object(needs, "_settings", lambda: {"principal_name": "Ada"})
        p2.start()
        self.addCleanup(p2.stop)

    def queue_without(self, attr, module):
        mod = importlib.import_module(module)
        saved = getattr(mod, attr)
        delattr(mod, attr)
        try:
            return needs.build_queue(Path(self._tmp.name))
        finally:
            setattr(mod, attr, saved)

    def test_a_removed_list_tasks_adds_the_host_line_next_to_the_board_error(self):
        q = self.queue_without("list_tasks", "hermes_cli.kanban_db")
        self.assertTrue(any(e.startswith("kanban[default]:") for e in q["errors"]), q["errors"])
        self.assertTrue(
            any(e.startswith("host: this Hermes lacks 1 of") and "list_tasks" in e for e in q["errors"]),
            q["errors"],
        )

    def test_a_removed_connect_adds_the_host_line_too(self):
        q = self.queue_without("connect", "hermes_cli.kanban_db_connect")
        self.assertTrue(any(e.startswith("kanban[default]:") for e in q["errors"]), q["errors"])
        self.assertTrue(any(e.startswith("host:") and "connect" in e for e in q["errors"]), q["errors"])

    def test_a_healthy_host_with_a_clean_board_has_no_host_line(self):
        q = needs.build_queue(Path(self._tmp.name))
        self.assertFalse(any(e.startswith("host:") for e in q["errors"]), q["errors"])

    def test_a_board_failure_on_a_healthy_host_adds_no_host_line(self):
        def bad_connect(board=None, **kw):
            raise OSError("database is locked")

        with patch("hermes_cli.kanban_db_connect.connect", bad_connect):
            q = needs.build_queue(Path(self._tmp.name))
        self.assertTrue(any(e.startswith("kanban[default]:") for e in q["errors"]), q["errors"])
        self.assertFalse(any(e.startswith("host:") for e in q["errors"]), q["errors"])


def load_script():
    spec = importlib.util.spec_from_file_location("host_compat_script", ROOT / "scripts" / "host_compat.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RuntimeIdentity(unittest.TestCase):
    def test_the_version_comes_from_the_hermes_runtime_not_the_metadata_placeholder(self):
        script = load_script()
        stub = types.SimpleNamespace(__version__="9.9.9", __release_date__="2030.1.2")
        with patch.dict(sys.modules, {"hermes_cli": stub}), \
                patch.object(script.metadata, "version", lambda dist: "0.0.0"):
            self.assertEqual(script._hermes_identity(), {"hermes_version": "9.9.9", "hermes_release_date": "2030.1.2"})

    def test_two_revisions_with_the_same_placeholder_metadata_differ(self):
        script = load_script()
        out = []
        for ver, date in (("1.0.0", "2030.1.1"), ("1.0.1", "2030.2.1")):
            stub = types.SimpleNamespace(__version__=ver, __release_date__=date)
            with patch.dict(sys.modules, {"hermes_cli": stub}), \
                    patch.object(script.metadata, "version", lambda dist: "0.0.0"):
                out.append(script._hermes_identity())
        self.assertNotEqual(out[0], out[1])

    def test_a_placeholder_is_never_recorded_as_a_version(self):
        script = load_script()
        stub = types.SimpleNamespace()  # no runtime identity
        with patch.dict(sys.modules, {"hermes_cli": stub}), \
                patch.object(script.metadata, "version", lambda dist: "0.0.0"):
            self.assertEqual(
                script._hermes_identity(), {"hermes_version": "unknown", "hermes_release_date": "unknown"}
            )

    def test_real_metadata_is_the_fallback_when_it_is_not_a_placeholder(self):
        script = load_script()
        with patch.dict(sys.modules, {"hermes_cli": types.SimpleNamespace()}), \
                patch.object(script.metadata, "version", lambda dist: "2.3.4"):
            self.assertEqual(script._hermes_identity()["hermes_version"], "2.3.4")

    @unittest.skipUnless(HERMES, "Hermes is not importable here")
    def test_the_script_records_the_installed_hermes_runtime_version(self):
        out = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "host_compat.py")], capture_output=True, text=True, timeout=60
        )
        rec = json.loads(out.stdout)
        hermes_cli = importlib.import_module("hermes_cli")
        # A runtime whose own version is the 0.0.0 placeholder (the pinned CI checkout is one)
        # is recorded as unknown, never as 0.0.0; any other version is recorded as it is.
        version = str(hermes_cli.__version__)
        expected = "unknown" if version in {"", "0.0.0", "0", "unknown", "none"} else version
        self.assertEqual(rec["hermes_version"], expected)
        self.assertEqual(rec["hermes_release_date"], str(hermes_cli.__release_date__ or "unknown"))
        self.assertNotEqual(rec["hermes_version"], "0.0.0")


class MatrixRowsAreIdentifiable(unittest.TestCase):
    def rows(self):
        text = (ROOT / "docs" / "verification.md").read_text()
        section = text.split("## Acceptance matrix", 1)[1].split("\n## ", 1)[0]
        out = []
        for line in section.splitlines():
            if line.startswith("|") and not line.startswith("| ---") and not line.startswith("| Receipt"):
                out.append([c.strip() for c in line.strip().strip("|").split("|")])
        return out

    def test_the_source_row_is_not_claimed_without_a_run_record(self):
        rows = {r[0]: r for r in self.rows()}
        source = next(r for k, r in rows.items() if k.startswith("Source tests"))
        self.assertEqual(source[4], "not run")
        self.assertEqual(source[3], "not recorded")

    def test_the_docs_say_how_to_record_a_row(self):
        text = (ROOT / "docs" / "verification.md").read_text()
        self.assertIn("release date", text)
        self.assertIn("source commit", text)


class DashboardHostProbesSurviveLazyFailures(unittest.TestCase):
    """Same class as the host check: reading an attribute of a host object can raise more than
    AttributeError (a lazily resolved name whose dependency is missing)."""

    def api(self):
        spec = importlib.util.spec_from_file_location("ub_api_r4_stack17", ROOT / "dashboard" / "plugin_api.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["ub_api_r4_stack17"] = mod
        self.addCleanup(sys.modules.pop, "ub_api_r4_stack17", None)
        spec.loader.exec_module(mod)
        return mod

    def test_a_lazy_module_that_raises_does_not_hide_the_plugin_context(self):
        api = self.api()

        class Lazy(types.ModuleType):
            def __getattr__(self, name):
                raise ImportError("lazy dependency missing")

        ctx = object()
        target = types.ModuleType("ub_r4_stack17_pkg")
        target.__file__ = str(api.PLUGIN_DIR / "__init__.py")
        target._PLUGIN_CTX = ctx
        with patch.dict(sys.modules, {"ub_r4_stack17_lazy": Lazy("ub_r4_stack17_lazy"), "ub_r4_stack17_pkg": target}):
            self.assertIs(api._loaded_ctx(), ctx)

    def test_a_context_whose_llm_raises_is_treated_as_unavailable(self):
        api = self.api()

        class Ctx:
            @property
            def llm(self):
                raise ImportError("provider library missing")

        self.assertFalse(api._has_llm(Ctx()))
        self.assertFalse(api._has_llm(None))
        self.assertTrue(api._has_llm(types.SimpleNamespace(llm=object())))


if __name__ == "__main__":
    unittest.main()
