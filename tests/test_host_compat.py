"""Hermes interfaces UNBL0CK depends on: listed, checked, and failing clearly (issue #44)."""

import importlib
import json
import re
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from test_needs import needs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

try:
    importlib.import_module("hermes_cli.kanban_db")
except Exception:  # noqa: BLE001
    HERMES = False
else:
    HERMES = True


HOST_ROOTS = ("hermes_cli", "hermes_constants", "gateway")


def source_files():
    """Every shipped Python file that can reach into Hermes (tests are not shipped)."""
    files = [ROOT / n for n in ("needs.py", "jobs.py", "recovery.py", "adapter_contract.py", "__init__.py")]
    files += [ROOT / "dashboard" / "plugin_api.py"]
    files += sorted((ROOT / "scripts").glob("*.py"))
    return [f for f in files if f.exists() and f.name != "host_compat.py"]


def used_names():
    """Every Hermes name the shipped source reaches for, read from the syntax tree.

    Only executable imports and attribute accesses count: a name in a comment or docstring is not
    a dependency. ``from hermes_cli import kanban_db`` makes ``kanban_db.x`` an access to
    ``hermes_cli.kanban_db.x``; ``from gateway.pairing import PairingStore`` is the name
    ``PairingStore``. The one method taken from a returned object, ``list_pending``, is recorded
    as ``PairingStore.list_pending``.
    """
    import ast

    found: dict[str, set[str]] = {}
    for path in source_files():
        tree = ast.parse(path.read_text())
        alias: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in HOST_ROOTS:
                for a in node.names:
                    if node.module == "hermes_cli":
                        alias[a.asname or a.name] = f"hermes_cli.{a.name}"
                    else:
                        found.setdefault(node.module, set()).add(a.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in alias:
                found.setdefault(alias[node.value.id], set()).add(node.attr)
            if isinstance(node, ast.Attribute) and node.attr == "list_pending":
                found.setdefault("gateway.pairing", set()).add("PairingStore.list_pending")
    return found


def full_module(drop=()):
    """A stand-in module holding every listed name (dotted names become nested attributes)."""
    ns = types.SimpleNamespace()
    for names in needs.HOST_API.values():
        for name in names:
            if name in drop:
                continue
            head, _, tail = name.partition(".")
            if tail:
                setattr(ns, head, types.SimpleNamespace(**{tail: 1}))
            else:
                setattr(ns, head, 1)
    return ns


class TheTableIsTheCode(unittest.TestCase):
    def test_every_name_the_source_uses_is_listed_and_nothing_extra(self):
        used = used_names()
        listed = {m: set(n) for m, n in needs.HOST_API.items()}
        self.assertEqual(used, listed)

    def test_private_names_are_counted(self):
        privates = {n for names in needs.HOST_API.values() for n in names if n.startswith("_")}
        self.assertEqual(
            privates,
            {"_append_event", "_landing_status_after_parents", "_reclaim_dangling_run",
             "_resume_status_from_events", "_mutate_subscriptions", "_subscriptions_path"},
        )
        self.assertEqual(needs.host_compat(lambda m: full_module())["private_checked"], len(privates))


class Report(unittest.TestCase):
    def full(self):
        return full_module()

    def test_a_complete_host_is_ok(self):
        r = needs.host_compat(lambda m: self.full())
        self.assertEqual((r["ok"], r["missing"], r["modules_missing"]), (True, [], []))
        self.assertEqual(needs.host_compat_note(r), "")

    def test_a_renamed_private_function_is_named(self):
        def load(module):
            mod = self.full()
            if module == "hermes_cli.kanban_db":
                del mod.__dict__["_reclaim_dangling_run"]
            return mod

        r = needs.host_compat(load)
        self.assertEqual(r["missing"], ["hermes_cli.kanban_db._reclaim_dangling_run"])
        self.assertFalse(r["ok"])
        note = needs.host_compat_note(r)
        self.assertIn("lacks 1 of the", note)
        self.assertIn("_reclaim_dangling_run", note)
        self.assertIn("update UNBL0CK", note)

    def test_a_missing_module_marks_all_its_names(self):
        def load(module):
            if module == "hermes_cli.kanban_db_connect":
                raise ImportError("gone")
            return self.full()

        r = needs.host_compat(load)
        self.assertEqual(r["modules_missing"], ["hermes_cli.kanban_db_connect"])
        self.assertEqual(r["missing"], ["hermes_cli.kanban_db_connect.connect"])

    def test_the_note_is_bounded_and_carries_names_only(self):
        r = needs.host_compat(lambda m: (_ for _ in ()).throw(ImportError("/secret/path/x")))
        note = needs.host_compat_note(r)
        self.assertNotIn("/secret", note)
        self.assertIn("...", note)
        self.assertLess(len(note), 600)

    @unittest.skipUnless(HERMES, "Hermes is not importable here")
    def test_the_hermes_in_this_environment_has_every_interface(self):
        r = needs.host_compat()
        self.assertEqual(r["missing"], [], r)


class FailsClearly(unittest.TestCase):
    def test_a_host_without_the_interface_adds_one_plain_line_to_the_queue_errors(self):
        broken = {"ok": False, "checked": 18, "missing": ["hermes_cli.kanban_db.list_tasks"], "modules_missing": [], "private_checked": 5}

        def boom(boards=None, errors=None):
            raise AttributeError("module 'hermes_cli.kanban_db' has no attribute 'list_tasks'")

        import tempfile

        with tempfile.TemporaryDirectory() as d, patch.object(needs, "kanban_items", boom), \
                patch.object(needs, "host_compat", lambda *a: broken), patch.object(needs, "_settings", lambda: {"principal_name": "Ada"}):
            q = needs.build_queue(Path(d))
        self.assertTrue(any(e.startswith("kanban: AttributeError") for e in q["errors"]))
        self.assertTrue(any(e.startswith("host: this Hermes lacks 1 of the 18") for e in q["errors"]))

    def test_a_healthy_host_adds_no_host_line(self):
        def boom(boards=None, errors=None):
            raise RuntimeError("one board failed")

        import tempfile

        with tempfile.TemporaryDirectory() as d, patch.object(needs, "kanban_items", boom), \
                patch.object(needs, "host_compat", lambda *a: {"ok": True, "checked": 18, "missing": [], "modules_missing": [], "private_checked": 5}), \
                patch.object(needs, "_settings", lambda: {"principal_name": "Ada"}):
            q = needs.build_queue(Path(d))
        self.assertFalse(any(e.startswith("host:") for e in q["errors"]))


class Script(unittest.TestCase):
    def test_the_script_prints_a_record_without_card_or_settings_text(self):
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "host_compat.py")], capture_output=True, text=True, timeout=60)
        self.assertIn(out.returncode, (0, 1), out.stderr[-300:])
        rec = json.loads(out.stdout)
        self.assertEqual(
            set(rec),
            {"recorded_at", "plugin_version", "hermes_version", "hermes_release_date", "python", "os", "state_lock", "interfaces"},
        )
        self.assertRegex(rec["recorded_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(rec["plugin_version"], re.search(r'^version:\s*"([^"]+)"', (ROOT / "plugin.yaml").read_text(), re.M).group(1))
        self.assertIn(rec["state_lock"], {"flock", "msvcrt", "thread-only"})
        self.assertEqual(set(rec["interfaces"]), {"ok", "checked", "missing", "modules_missing", "private_checked"})


if __name__ == "__main__":
    unittest.main()
