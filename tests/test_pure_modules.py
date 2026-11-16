"""redact.py, gates.py and mailtext.py are documented as pure: they read no files, settings or
state, so needs.py can hand them text and tests can patch needs without reaching inside them.
This reads each one's syntax tree and holds that claim."""

import ast
import unittest
from pathlib import Path

from _shipped import ROOT, plugin_modules

PURE = ("redact.py", "gates.py", "mailtext.py")
# Standard-library modules that only compute on values they are given.
ALLOWED_IMPORTS = {"__future__", "collections", "html", "html.parser", "json", "re", "typing", "unicodedata"}
# redact.py reads the user's home folder path to scrub it from text: it may import os and use
# exactly os.path.expanduser, nothing else of os.
EXCEPTIONS = {"redact.py": {"os": {"os.path.expanduser"}}}
# Builtins that reach outside the process or around this scan; named at all, not only called,
# so ``reader = open`` is caught too.
IO_NAMES = {"__builtins__", "open", "input", "exec", "eval", "__import__", "globals", "breakpoint"}
# Attributes of an imported module that read or write a file, the process or the network
# (json.load); json.loads and json.dumps work on strings and stay allowed.
IO_ATTRS = {"open", "load", "dump", "read_text", "read_bytes", "write_text", "write_bytes", "urlopen", "system"}


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([node.id, *reversed(parts)]) if isinstance(node, ast.Name) else ""


def violations(source: str, name: str = "") -> list[str]:
    tree = ast.parse(source, name)
    exceptions = EXCEPTIONS.get(name, {})
    modules = {(a.asname or a.name).split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    # Every node inside an allowed exception use (os.path.expanduser) is covered by it.
    covered = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and any(_dotted(node) in uses for uses in exceptions.values()):
            covered |= {id(n) for n in ast.walk(node)}
    found = []
    for node in ast.walk(tree):
        if id(node) in covered:
            continue
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name not in ALLOWED_IMPORTS and a.name not in exceptions]
            found += [f"{a.name} as {a.asname}" for a in node.names if a.name in exceptions and a.asname]
        elif isinstance(node, ast.ImportFrom):
            mod = "." * node.level + (node.module or "")
            if mod not in ALLOWED_IMPORTS:  # an exception module is used by its dotted name only
                found.append(mod)
        elif isinstance(node, ast.Name) and node.id in IO_NAMES:
            found.append(f"{node.id} at line {node.lineno}")
        elif isinstance(node, ast.Name) and node.id in exceptions:
            found.append(f"{node.id} beyond {sorted(exceptions[node.id])} at line {node.lineno}")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in IO_ATTRS
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
        ):
            found.append(f".{node.attr} at line {node.lineno}")
    return found


# Patches that wrap the real function to count the calls needs itself makes; the moved code's own
# calls are not what they measure.
SPIES = {"_ask_redact"}


def _needs_aliases(tree: ast.AST) -> set[str]:
    """Names a test file binds to the needs module: ``import needs as n``, ``from x import needs``, ``n = needs``."""
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            aliases |= {a.asname or a.name for a in node.names if a.name == "needs"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name) and node.value.id in aliases:
            aliases |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return aliases


def _needs_target(dotted: str) -> str | None:
    module, _, attr = dotted.rpartition(".")
    return attr if module == "needs" or module.endswith(".needs") else None


def patched_on_needs(sources: dict[str, str] | None = None) -> set[str]:
    """Every name a test patches on ``needs``: setattr or patch.object on the module or an alias
    of it, and setattr or patch with a "needs.<name>" string target."""
    if sources is None:
        sources = {p.name: p.read_text() for p in sorted(Path(__file__).parent.glob("*.py"))}
    names = set()
    for fname, text in sources.items():
        tree = ast.parse(text, fname)
        aliases = _needs_aliases(tree) | {"needs"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            first, rest = node.args[0], node.args[1:]
            if func in ("setattr", "object") and rest and isinstance(rest[0], ast.Constant):
                if isinstance(first, ast.Name) and first.id in aliases:
                    names.add(rest[0].value)
            if func in ("setattr", "patch") and isinstance(first, ast.Constant) and isinstance(first.value, str):
                attr = _needs_target(first.value)
                if attr:
                    names.add(attr)
    return names


class PureModules(unittest.TestCase):
    def test_pure_modules_import_and_reach_nothing_outside(self):
        for name in PURE:
            with self.subTest(name=name):
                self.assertEqual(violations((ROOT / name).read_text(), name), [])

    def test_the_scan_catches_an_aliased_or_attribute_read(self):
        for source in (
            "reader = open\ndata = reader('plugin.yaml').read()\n",
            "import json\njson.load(x)\n",
            "from pathlib import Path\n",
            "import os\nos.system('x')\n",
            "from os import system as run\nrun('x')\n",
            "import os.path\nos.system('x')\n",
            "import os as o\no.system('x')\n",
            "import codecs\ncodecs.open('f')\n",
            "f = __builtins__['open']\n",
        ):
            with self.subTest(source=source):
                self.assertNotEqual(violations(source, "redact.py"), [])

    def test_the_scan_allows_pure_code_that_reuses_a_builtin_name(self):
        self.assertEqual(violations('from re import compile\nP = compile("a+")\n', "gates.py"), [])
        self.assertEqual(violations('import os\nHOME = os.path.expanduser("~")\n', "redact.py"), [])

    def test_the_patch_scan_sees_string_targets_and_aliases_only_of_needs(self):
        sources = {
            "a.py": 'monkeypatch.setattr("needs._a", f)\npatch("pkg.needs._b", f)\n',
            "b.py": "import needs as n\npatch.object(n, '_c', f)\nm = n\nmonkeypatch.setattr(m, '_d', f)\n",
            "c.py": 'patch("otherneeds._x", f)\npatch.object(other, "_y", f)\n',
        }
        self.assertEqual(patched_on_needs(sources), {"_a", "_b", "_c", "_d"})

    def test_the_pure_modules_are_shipped(self):
        shipped = {p.name for p in plugin_modules()}
        self.assertLessEqual(set(PURE), shipped)

    def test_no_test_patches_a_name_that_moved_to_a_pure_module(self):
        """needs re-binds the names it loads from the pure modules; patching one on needs would not
        reach the moved code that calls it, so a test doing that would pass against a stub only."""
        moved = set()
        for name in PURE:
            tree = ast.parse((ROOT / name).read_text(), name)
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    moved.add(node.name)
                elif isinstance(node, ast.Assign):
                    moved |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        self.assertEqual(sorted(patched_on_needs() & moved - SPIES), [])


if __name__ == "__main__":
    unittest.main()
