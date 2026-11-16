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
# redact.py reads the user's home folder path (os.path.expanduser) to scrub it from text.
EXCEPTIONS = {"redact.py": {"os"}}
# Builtins that reach outside the process or around this scan; named at all, not only called,
# so ``reader = open`` is caught too.
IO_NAMES = {"__builtins__", "open", "input", "exec", "eval", "compile", "__import__", "globals", "getattr", "breakpoint"}
# Attributes of an imported module that read or write a file, the process or the network
# (json.load, os.system); json.loads and json.dumps work on strings and stay allowed.
IO_ATTRS = {"open", "load", "dump", "read_text", "read_bytes", "write_text", "write_bytes", "urlopen", "system"}


def violations(source: str, name: str = "") -> list[str]:
    tree = ast.parse(source, name)
    allowed = ALLOWED_IMPORTS | EXCEPTIONS.get(name, set())
    modules = {(a.asname or a.name).split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name not in allowed]
        elif isinstance(node, ast.ImportFrom):
            mod = "." * node.level + (node.module or "")
            if mod not in allowed:
                found.append(mod)
        elif isinstance(node, ast.Name) and node.id in IO_NAMES:
            found.append(f"{node.id} at line {node.lineno}")
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


def patched_on_needs() -> set[str]:
    """Every name a test patches on ``needs`` (monkeypatch.setattr, patch.object, patch("...needs.x"))."""
    names = set()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), path.name)):
            if not isinstance(node, ast.Call) or len(node.args) < 1:
                continue
            func = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            first = node.args[0]
            if func in ("setattr", "object") and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                if isinstance(first, ast.Name) and first.id == "needs":
                    names.add(node.args[1].value)
            elif func == "patch" and isinstance(first, ast.Constant) and isinstance(first.value, str):
                module, _, attr = first.value.rpartition(".")
                if module.endswith("needs"):
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
            "import codecs\ncodecs.open('f')\n",
            "f = __builtins__['open']\n",
        ):
            with self.subTest(source=source):
                self.assertNotEqual(violations(source, "gates.py"), [])

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
