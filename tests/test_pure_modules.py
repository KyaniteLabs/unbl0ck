"""redact.py, gates.py and mailtext.py are documented as pure: they read no files, settings or
state, so needs.py can hand them text and tests can patch needs without reaching inside them.
This reads each one's syntax tree and holds that claim."""

import ast
import unittest

from _shipped import ROOT, plugin_modules

PURE = ("redact.py", "gates.py", "mailtext.py")
# Standard-library modules that only compute on values they are given.
ALLOWED_IMPORTS = {"__future__", "codecs", "collections", "html", "html.parser", "json", "re", "typing", "unicodedata"}
# redact.py reads the user's home folder path (os.path.expanduser) to scrub it from text.
EXCEPTIONS = {"redact.py": {"os"}}
# Builtins that reach outside the process.
IO_CALLS = {"open", "input", "exec", "eval", "__import__"}


def violations(name: str) -> list[str]:
    tree = ast.parse((ROOT / name).read_text(), name)
    allowed = ALLOWED_IMPORTS | EXCEPTIONS.get(name, set())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name not in allowed]
        elif isinstance(node, ast.ImportFrom):
            mod = "." * node.level + (node.module or "")
            if mod not in allowed:
                found.append(mod)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in IO_CALLS:
            found.append(f"{node.func.id}() at line {node.lineno}")
    return found


class PureModules(unittest.TestCase):
    def test_pure_modules_import_and_call_nothing_that_reads_outside(self):
        for name in PURE:
            with self.subTest(name=name):
                self.assertEqual(violations(name), [])

    def test_the_pure_modules_are_shipped(self):
        shipped = {p.name for p in plugin_modules()}
        self.assertLessEqual(set(PURE), shipped)


if __name__ == "__main__":
    unittest.main()
