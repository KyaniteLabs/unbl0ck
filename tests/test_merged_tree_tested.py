"""A main push may skip re-testing only a tree its pull request head already passed."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "merged_tree_tested.py"
SPEC = importlib.util.spec_from_file_location("merged_tree_tested", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

REPO = "KyaniteLabs/unbl0ck"
PUSHED = "a" * 40
HEAD = "b" * 40


def _fake(*, pulls=None, trees=None, runs=None):
    pulls = (
        [
            {
                "number": 7,
                "merged_at": "2026-11-22T00:00:00Z",
                "merge_commit_sha": PUSHED,
                "base": {"ref": "main"},
                "head": {"sha": HEAD},
            }
        ]
        if pulls is None
        else pulls
    )
    trees = {PUSHED: "t1", HEAD: "t1"} if trees is None else trees
    runs = (
        [
            {
                "id": 1,
                "run_number": 5,
                "run_attempt": 1,
                "head_sha": HEAD,
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
            }
        ]
        if runs is None
        else runs
    )

    def fetch(path):
        if path.endswith("/pulls"):
            return pulls
        if "/git/commits/" in path:
            return {"tree": {"sha": trees[path.rsplit("/", 1)[1]]}}
        if "/actions/workflows/validate.yml/runs?" in path:
            assert "event=pull_request" in path and f"head_sha={HEAD}" in path
            return {"workflow_runs": runs}
        raise AssertionError(path)

    return fetch


def _decide(**kwargs):
    return MODULE.decide(_fake(**kwargs), REPO, PUSHED, "validate.yml")[0]


def test_identical_tree_with_green_pull_request_run_is_tested():
    assert _decide() is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pulls": []},
        {
            "pulls": [
                {
                    "number": 7,
                    "merged_at": None,
                    "merge_commit_sha": PUSHED,
                    "base": {"ref": "main"},
                    "head": {"sha": HEAD},
                }
            ]
        },
        {
            "pulls": [
                {
                    "number": 7,
                    "merged_at": "x",
                    "merge_commit_sha": "c" * 40,
                    "base": {"ref": "main"},
                    "head": {"sha": HEAD},
                }
            ]
        },
        {
            "pulls": [
                {
                    "number": 7,
                    "merged_at": "x",
                    "merge_commit_sha": PUSHED,
                    "base": {"ref": "release"},
                    "head": {"sha": HEAD},
                }
            ]
        },
        {"trees": {PUSHED: "t1", HEAD: "t2"}},
        {"runs": []},
        {
            "runs": [
                {
                    "id": 1,
                    "run_number": 5,
                    "run_attempt": 1,
                    "head_sha": HEAD,
                    "event": "pull_request",
                    "status": "completed",
                    "conclusion": "failure",
                }
            ]
        },
        {
            "runs": [
                {
                    "id": 1,
                    "run_number": 5,
                    "run_attempt": 1,
                    "head_sha": HEAD,
                    "event": "pull_request",
                    "status": "in_progress",
                    "conclusion": None,
                }
            ]
        },
        {
            "runs": [
                {
                    "id": 1,
                    "run_number": 5,
                    "run_attempt": 1,
                    "head_sha": HEAD,
                    "event": "pull_request",
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "id": 2,
                    "run_number": 6,
                    "run_attempt": 1,
                    "head_sha": HEAD,
                    "event": "pull_request",
                    "status": "completed",
                    "conclusion": "cancelled",
                },
            ]
        },
    ],
)
def test_anything_short_of_proof_runs_the_suite(kwargs):
    assert _decide(**kwargs) is False


def test_api_failure_prints_tested_false(capsys):
    def broken(path):
        raise OSError("network down")

    assert MODULE.main(["--repository", REPO, "--sha", PUSHED, "--workflow", "validate.yml"], fetch=broken) == 0
    assert capsys.readouterr().out == "tested=false\n"


def test_main_pushes_skip_only_through_the_helper_and_pr_runs_supersede():
    text = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    assert (
        'merged_tree_tested.py --repository "$GITHUB_REPOSITORY" --sha "$GITHUB_SHA" --workflow validate.yml' in text
    )
    assert "if: github.event_name == 'push'" in text
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in text
    # Every gate job runs unless the helper proved the tree was already tested.
    assert text.count("needs.merged-tree.outputs.tested != 'true'") == 4
