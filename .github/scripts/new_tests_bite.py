"""A regression test must fail without its fix.

For each commit in BASE..HEAD whose subject starts with "fix", find the test functions that
commit adds under tests/, put the parent's production files under the commit's tests, and run
those tests. Any that pass did not need the fix, so they guard nothing: the script exits 1.

Usage: python .github/scripts/new_tests_bite.py --base origin/main
Runs pytest with this interpreter, so call it with the Python that runs the test suite.
"""

import argparse
import ast
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def git(*args: str, cwd: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def test_names(source: str) -> set[str]:
    """``name`` or ``Class::name`` for every test function in a module."""
    names = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.update(
                f"{node.name}::{n.name}" for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")
            )
    return names


def changed(commit: str, *spec: str) -> list[str]:
    """Paths the commit changes; a rename counts as a deletion plus an addition."""
    out = git("diff", "--name-only", "--no-renames", "-z", f"{commit}^", commit, "--", *spec)
    return [p for p in out.split("\0") if p]


def added_tests(commit: str) -> list[str]:
    ids = []
    fields = git("diff", "--name-status", "-z", "-M", "--diff-filter=AMR", f"{commit}^", commit, "--", "tests").split("\0")
    while len(fields) > 1:
        status, src = fields.pop(0), fields.pop(0)
        path = fields.pop(0) if status.startswith("R") else src  # a renamed file keeps its old tests
        if not re.fullmatch(r"tests/(.+/)?(test_[^/]*|[^/]*_test)\.py", path):
            continue
        before = git("show", f"{commit}^:{src}") if status != "A" else ""
        new = test_names(git("show", f"{commit}:{path}")) - test_names(before)
        ids += sorted(f"{path}::{name}" for name in new)
    return ids


def passing_on_parent(commit: str, ids: list[str]) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        wt = str(Path(tmp) / "wt")
        git("worktree", "add", "--detach", wt, commit)
        try:
            paths = changed(commit, ".", ":!tests")
            on_parent = {p for p in paths if git("ls-tree", f"{commit}^", "--", p, cwd=wt)}
            for path in paths:  # remove first: a file the fix turned into a folder needs the folder gone
                if path not in on_parent:
                    git("rm", "-q", "--", path, cwd=wt)
            for path in on_parent:
                git("checkout", f"{commit}^", "--", path, cwd=wt)
            out = ""
            for path in dict.fromkeys(i.split("::", 1)[0] for i in ids):  # one run per file: a file that
                out += subprocess.run(  # fails to import on the parent must not stop the others
                    [sys.executable, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider",
                     *(i for i in ids if i.startswith(f"{path}::"))],
                    cwd=wt, capture_output=True, text=True,
                ).stdout
            # A test whose only failures are subtests is reported PASSED as well: a failed subtest bites.
            return [i for i in ids if re.search(rf"^PASSED {re.escape(i)}\b", out, re.M)
                    and not re.search(rf"^SUBFAILED.* {re.escape(i)}\b", out, re.M)]
        finally:
            git("worktree", "remove", "--force", wt)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True)
    base = parser.parse_args().base
    failed = False
    for line in git("log", "--reverse", "--no-merges", "--format=%H %s", f"{base}..HEAD").splitlines():
        commit, subject = line.split(" ", 1)
        if not subject.startswith("fix"):
            continue
        ids = added_tests(commit)
        if not ids:
            print(f"note {commit[:9]} adds no test: {subject}")
            continue
        passed = passing_on_parent(commit, ids)
        for i in passed:
            print(f"::error::{commit[:9]} {i} passes without the fix, so it does not guard it")
        print(f"{'FAIL' if passed else 'ok  '} {commit[:9]} {len(ids) - len(passed)}/{len(ids)} new tests fail on the parent")
        failed |= bool(passed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
