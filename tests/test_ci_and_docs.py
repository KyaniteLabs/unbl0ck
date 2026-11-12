"""CI gate and docs hygiene: the validate step must assert the validator's `ok`, the skip gate
must show which tests skipped, and the README must run the same pytest command CI runs."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_validate_workflow_asserts_ok():
    wf = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    step = wf[wf.index("Real validator receipt") :]
    assert "validate.json" in step
    assert '.get("ok")' in step or "['ok']" in step or '["ok"]' in step, (
        "the validator receipt is never asserted ok"
    )


def test_skip_gate_prints_the_skipped_tests():
    wf = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    assert 'grep "^SKIPPED"' in wf, "a failing skip gate must list what skipped"


def test_readme_develop_uses_pytest_like_ci():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "python -m pytest -rs tests" in readme
    assert "unittest discover" not in readme
