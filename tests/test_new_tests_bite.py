"""The CI check that a fix's new tests fail without the fix, run on small throwaway repositories."""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "new_tests_bite.py"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
OLD_TESTS = "".join(f"def test_old_{i}():\n    assert {i} == {i}\n\n\n" for i in range(12))


class TheSummaryParser(unittest.TestCase):
    """#73 review: whether a run reports SUBFAILED depends on the installed pytest, so the parser is
    also tested on fixed output."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("new_tests_bite", SCRIPT)
        self.bite = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bite)

    def test_a_test_with_a_failed_subtest_is_not_passing(self):
        out = ("SUBFAILED[n=1] tests/test_sub.py::T::test_f - AssertionError\n"
               "PASSED tests/test_sub.py::T::test_f\nPASSED tests/test_sub.py::T::test_g\n")
        ids = ["tests/test_sub.py::T::test_f", "tests/test_sub.py::T::test_g"]
        self.assertEqual(self.bite.still_passing(out, ids), ["tests/test_sub.py::T::test_g"])


class TheBiteCheck(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        self.git("init", "-q", "-b", "main")
        self.commit("base", {"pytest.ini": "[pytest]\npythonpath = .\n", "mod.py": "def f():\n    return 1\n",
                             "legacy.py": "LEGACY = 1\n" * 20, "tests/test_old.py": OLD_TESTS})
        self.base = self.git("rev-parse", "HEAD").stdout.strip()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, env=ENV, check=True, capture_output=True, text=True)

    def commit(self, subject, files, gone=(), moved=()):
        for old, new in moved:
            self.git("mv", old, new)
        for path in gone:
            self.git("rm", "-q", path)
        for path, text in files.items():
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / path).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", subject)

    def check(self):
        done = subprocess.run([sys.executable, str(SCRIPT), "--base", self.base], cwd=self.repo, env=ENV,
                              capture_output=True, text=True)
        return done.returncode, done.stdout + done.stderr

    def test_a_test_that_needs_the_fix_passes_the_check(self):
        self.commit("fix: f returns 2", {"mod.py": "def f():\n    return 2\n",
                                         "tests/test_mod.py": "from mod import f\n\ndef test_f():\n    assert f() == 2\n"})
        self.assertEqual(self.check()[0], 0)

    def test_a_test_that_fails_only_in_a_subtest_on_the_parent_bites(self):
        """#73: pytest reports a test whose only failures are subtests as passed too, so a test that
        bit through a subtest was reported as passing without its fix."""
        test = ("import unittest\nfrom mod import f\n\n\nclass T(unittest.TestCase):\n"
                "    def test_f(self):\n        for n in (1, 2):\n            with self.subTest(n=n):\n"
                "                self.assertEqual(f(), 2)\n")
        self.commit("fix: f returns 2", {"mod.py": "def f():\n    return 2\n", "tests/test_sub.py": test})
        code, out = self.check()
        self.assertEqual(code, 0, out)

    def test_a_toothless_test_beside_a_collection_error_is_caught(self):
        """#81 review: a test importing a module the fix adds fails to collect on the parent, and that
        stopped the run before the toothless test ran, so the check passed."""
        self.commit("fix: add api", {"new_api.py": "X = 1\n",
                                     "tests/test_api.py": "from new_api import X\n\ndef test_x():\n    assert X\n",
                                     "tests/test_noop.py": "def test_noop():\n    assert True\n"})
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("test_noop.py::test_noop passes", out)

    def test_a_toothless_test_in_a_renamed_file_is_caught_and_old_tests_are_not(self):
        """#81 review: a renamed test file was skipped; its old tests must not count as new either."""
        self.commit("fix: rename", {"tests/test_new.py": OLD_TESTS + "def test_noop():\n    assert True\n"},
                    moved=[("tests/test_old.py", "tests/test_new.py")])
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("test_new.py::test_noop passes", out)
        self.assertNotIn("test_old_", out)

    def test_other_pytest_file_names_and_spaces_are_read(self):
        """#81 review: ``*_test.py`` files and paths with spaces were not inspected."""
        for path in ("tests/noop_test.py", "tests/test_new case.py"):
            with self.subTest(path=path):
                self.git("reset", "-q", "--hard", self.base)
                self.commit("fix: noop", {path: "def test_noop():\n    assert True\n"})
                code, out = self.check()
                self.assertEqual(code, 1, out)
                self.assertIn(f"{path}::test_noop passes", out)

    def test_a_production_rename_is_undone_on_the_parent(self):
        """#81 review: the parent snapshot dropped the renamed file's old path, so a test of the
        rename passed there and was wrongly called toothless."""
        self.commit("fix: rename legacy", {"tests/test_gone.py": (
            "from pathlib import Path\n\ndef test_legacy_removed():\n    assert not Path('legacy.py').exists()\n")},
            moved=[("legacy.py", "current.py")])
        code, out = self.check()
        self.assertEqual(code, 0, out)

    def test_a_file_the_fix_turns_into_a_folder_is_restored(self):
        """#81 review: the parent's file was restored before the fix's folder of the same name was
        removed, so the snapshot failed and a correct fix was rejected."""
        self.commit("fix: legacy is a folder", {"legacy.py/value": "1\n", "tests/test_layout.py": (
            "from pathlib import Path\n\ndef test_layout():\n    assert Path('legacy.py/value').is_file()\n")},
            gone=["legacy.py"])
        code, out = self.check()
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
