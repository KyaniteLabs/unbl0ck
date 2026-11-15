"""The README says: "No shell commands ... The only subprocess is `ps -o lstart=` on hosts without
`/proc`." This reads the syntax tree of every Python file outside tests/ and holds that claim.

Hermes loads only the plugin modules, but the scan also covers design/, desktop/build.py and
scripts/: they are in the same checkout a user installs, so the claim is held for all of them.
A name in a comment or a string is not a use; an import, an attribute or a call is.
"""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {"tests", "node_modules", "__pycache__"}

# Any of these starts another program.
SPAWN_MODULES = {"subprocess", "pty", "multiprocessing", "concurrent.futures.process"}
SPAWN_OS = {"system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp", "startfile"}
SPAWN_OS_PREFIXES = ("exec", "spawn")
SPAWN_ASYNCIO = {"create_subprocess_exec", "create_subprocess_shell"}

# The one disclosed site: (file, enclosing function).
ALLOWED = {("needs.py", "_proc_start")}


def python_files():
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        yield rel


def spawn_sites(tree):
    """(line, enclosing function, what) for every use of a process-starting API."""
    found = []

    def visit(node, func):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        what = None
        if isinstance(node, ast.Import):
            hit = [a.name for a in node.names if a.name.split(".")[0] in SPAWN_MODULES
                   or a.name in SPAWN_MODULES]
            what = hit and f"import {', '.join(hit)}"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.split(".")[0] in SPAWN_MODULES or mod in SPAWN_MODULES:
                what = f"from {mod} import"
            elif mod == "os" and any(
                a.name in SPAWN_OS or a.name.startswith(SPAWN_OS_PREFIXES) for a in node.names
            ):
                what = "from os import <spawn>"
            elif mod == "asyncio" and any(a.name in SPAWN_ASYNCIO for a in node.names):
                what = "from asyncio import <subprocess>"
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            base, attr = node.value.id, node.attr
            if base == "os" and (attr in SPAWN_OS or attr.startswith(SPAWN_OS_PREFIXES)):
                what = f"os.{attr}"
            elif base == "asyncio" and attr in SPAWN_ASYNCIO:
                what = f"asyncio.{attr}"
            elif base in SPAWN_MODULES:
                what = f"{base}.{attr}"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "__import__":
            what = "__import__(...)"  # a dynamic import could hide any of the above
        if what:
            found.append((node.lineno, func, what))
        for child in ast.iter_child_nodes(node):
            visit(child, func)

    visit(tree, None)
    return found


class SubprocessSites(unittest.TestCase):
    def test_the_scan_sees_the_shipped_modules(self):
        files = {str(p) for p in python_files()}
        for rel in ("needs.py", "jobs.py", "__init__.py", "dashboard/plugin_api.py"):
            self.assertIn(rel, files)
        self.assertFalse(any(f.startswith("tests/") for f in files))

    def test_subprocess_is_used_at_exactly_the_disclosed_site(self):
        sites = {}
        for rel in python_files():
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=str(rel))
            for line, func, what in spawn_sites(tree):
                sites.setdefault((str(rel), func), []).append((line, what))
        self.assertEqual(set(sites), ALLOWED, sites)
        # Inside _proc_start: the import, one subprocess.run and the SubprocessError it catches.
        uses = sorted(what for _, what in sites[("needs.py", "_proc_start")])
        self.assertEqual(
            uses, ["import subprocess", "subprocess.SubprocessError", "subprocess.run"]
        )

    def test_the_one_call_is_ps_lstart_without_a_shell(self):
        tree = ast.parse((ROOT / "needs.py").read_text(encoding="utf-8"))
        (fn,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_proc_start"]
        (call,) = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "run" and getattr(n.func.value, "id", None) == "subprocess"
        ]
        argv = call.args[0]
        self.assertIsInstance(argv, ast.List)  # a fixed argv, never a command string
        self.assertEqual(
            [ast.literal_eval(e) for e in argv.elts[:4]], ["ps", "-o", "lstart=", "-p"]
        )
        kw = {k.arg: k.value for k in call.keywords}
        self.assertNotIn("shell", kw)
        self.assertNotIn("executable", kw)
        self.assertEqual(
            ast.literal_eval(kw["env"]), {"PATH": "/bin:/usr/bin", "LC_ALL": "C"}
        )
        self.assertEqual(ast.literal_eval(kw["timeout"]), 2)


if __name__ == "__main__":
    unittest.main()
