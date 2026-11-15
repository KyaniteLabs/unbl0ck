"""docs/FEATURES.md names the tests that pin each documented behaviour; every name must exist."""

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "FEATURES.md"
TEST_ID = re.compile(r"`(tests/[\w/]+\.py)::([\w:]+)`")


def defined(tree, parts):
    """Whether Class::method, a top-level function or class, or a module-level name exists."""
    scope = tree.body
    for i, name in enumerate(parts):
        found = None
        for node in scope:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                found = node
            elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            ):
                found = node  # e.g. TestX = Machine.TestCase
        if found is None:
            return False
        if i < len(parts) - 1:
            if not isinstance(found, ast.ClassDef):
                return False
            scope = found.body
    return True


class FeaturesDoc(unittest.TestCase):
    def ids(self):
        return TEST_ID.findall(DOC.read_text(encoding="utf-8"))

    def test_the_table_names_tests(self):
        self.assertGreaterEqual(len(self.ids()), 30)

    def test_every_referenced_test_exists(self):
        missing = []
        trees = {}
        for rel, node in self.ids():
            path = ROOT / rel
            if not path.is_file():
                missing.append(f"{rel} (no such file)")
                continue
            tree = trees.setdefault(rel, ast.parse(path.read_text(encoding="utf-8")))
            parts = node.split("::")
            if not parts[-1].startswith("test") and not parts[-1].startswith("Test"):
                missing.append(f"{rel}::{node} (not a test name)")
            elif not defined(tree, parts):
                missing.append(f"{rel}::{node}")
        self.assertEqual(missing, [])

    def test_the_docs_map_links_it(self):
        self.assertIn("(FEATURES.md)", (ROOT / "docs" / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
