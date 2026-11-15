"""Type-check the shipped code with mypy and fail only on errors not in the baseline.

The baseline (.github/mypy-baseline.txt) holds the errors present when the check was added: each
was read and found to be a narrowing mypy cannot see (a value checked elsewhere), not a defect.
Lines are compared without line numbers, so moving code does not churn it.

Usage: python .github/scripts/typecheck.py [--update]
"""

import os
import re
import sys
from collections import Counter
from pathlib import Path

from mypy import api

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / ".github" / "mypy-baseline.txt"
ARGS = ["--check-untyped-defs", "--ignore-missing-imports", "--no-error-summary", "--exclude", "^(tests|design)/", "."]


def errors() -> list[str]:
    os.chdir(ROOT)  # mypy names files relative to the working folder; the baseline does too
    out, err, status = api.run(ARGS)
    if status not in (0, 1) or err:
        sys.exit(f"mypy did not run: {err or out}")
    return sorted(re.sub(r"^([^:]+):\d+: ", r"\1: ", line) for line in out.splitlines() if ": error: " in line)


def main() -> int:
    found = errors()
    if sys.argv[1:] == ["--update"]:
        BASELINE.write_text("".join(f"{line}\n" for line in found))
        print(f"baseline: {len(found)} errors")
        return 0
    new = Counter(found) - Counter(BASELINE.read_text().splitlines())
    gone = Counter(BASELINE.read_text().splitlines()) - Counter(found)
    for line in sorted(new.elements()):
        print(f"::error::new type error: {line}")
    if gone:
        print(f"note: {sum(gone.values())} baseline errors are fixed; run with --update to drop them")
    print(f"mypy: {len(found)} errors, {sum(new.values())} new")
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
