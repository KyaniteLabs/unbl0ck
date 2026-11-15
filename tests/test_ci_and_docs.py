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


def test_docs_match_what_done_ask_markers_and_card_limits_actually_do():
    # Class sweep (docs contradicting behavior). Each assertion names the behavior it mirrors.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "operator-guide.md").read_text(encoding="utf-8")
    api = (ROOT / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
    # Done on a filed record keeps it 15 minutes for Undo; it is not a deletion.
    assert "**Done** deletes an agent-filed record" not in readme
    assert "deleting an agent-filed record does not" not in readme
    # Restart markers sit in one directory per profile (needs.ask_journal_dir).
    for text in (readme, guide):
        assert "ask/<job>.json" not in text
        assert "ask/<profile>/<job>.json" in text
    # GET /card does bound its identifiers (plugin_api._bounded -> 400).
    assert "GET Card does not apply the same length limit" not in arch
    # The summary of a chat is the 480-character queue excerpt, not 6000 characters.
    assert "480 characters the queue carries" in readme
    # The queue is grouped by source first (needs.SOURCE_ORDER); the README must not say "no ranking".
    assert "adds no ranking of its own" not in readme
    # jobs.py also holds the Ask journal; the stale "last 20" comment is gone.
    assert "In-memory Summary and Ask lifecycle tables" not in arch
    assert "Ask only uses the last 20" not in api
