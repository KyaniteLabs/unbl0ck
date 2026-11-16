# Trust audit, 2026-11-15

An outside audit found 15 gaps in how UNBL0CK checks its own guarantees (#71). This page records what each one became after 0.6.0. The "before" column is `main` at 0.6.0 (0b8d3af). The "after" column is the top of the follow-up stack: #70 fixes, then CI and docs gates, then the property tests.

## Scan results

| Check | Before | After |
|---|---|---|
| CI lint job (`ruff check .`) | none | runs; clean |
| CI type-check job (mypy) | none | runs; fails on any error outside the 69-error baseline |
| CI job timeout | none | 20 min tests, 10 min lint |
| CI dependency cache | none | uv cache on |
| Production `# noqa: BLE001` with no reason | 15 | 0 |
| Property-based (Hypothesis) test files | 0 | 1 |
| Feature list tied to tests | none | `docs/FEATURES.md`, 36 behaviours, checked by `tests/test_features_doc.py` |
| Test that `subprocess` is started in one place | none | `tests/test_subprocess_sites.py` |
| CI check that a fix's new tests fail without the fix | none | `.github/scripts/new_tests_bite.py`, on pull requests |
| Dead image links in `design/CANON.md` | 2 | 0 |
| `.claude/` in `.gitignore` | no | yes |
| Skip directives in tests | 50 | 51 (one new Windows-only skip; CI still fails on any skipped test) |

## Findings

| # | Finding | Status |
|---|---|---|
| 1 | Allowlist card view (the queue's trust boundary) is spread across `needs.py` | Fixed: Ask builds its payload from a fixed list of named fields in `_ask_card_payload`, pinned by `tests/test_r5_stack01_ask.py` |
| 2 | Process-start test is a characterization labelled as a contract; redaction tests assert on substrings | Fixed: the ps test is relabelled and a syntax-tree scan pins the one process start; `assert_redacted` fails on any surviving 4-character piece of a secret. On pull requests, CI runs each `fix` commit's new tests against the parent's code and fails if any pass |
| 3 | CI runs no linter or type checker | Fixed. Lint: ruff. Types: mypy, against a baseline of the 69 errors present when it was added; each was read and is a value checked elsewhere that mypy cannot see, not a defect |
| 4 | CI has no job timeout or dependency cache | Fixed. Making the checks required on `main` is a repository setting for the owner |
| 5 | No feature list or driver | Fixed: `docs/FEATURES.md` |
| 6 | No property-based tests for the state machines | Fixed: Hypothesis state machines for Ask jobs and for outside Done, Not for me and Undo |
| 7 | Skip directives across many test files | Reviewed, rejected as a defect: each skip is a platform or optional-dependency guard, and CI fails on any skipped test |
| 8 | `needs.py` is one large module; the "only one `subprocess` use" README claim is unchecked | Claim now checked (see 2). Partly fixed: the pure sections moved out: secret redaction (about 1,000 lines) to `redact.py`, the gate-phrase grammar to `gates.py` and the HTML and mail text reader to `mailtext.py`, each held pure by `tests/test_pure_modules.py`. The stateful sections stay, because tests patch names such as `_settings` and `hermes_home` on `needs` and a moved caller would not see the patch |
| 9 | Repeated `kanban_db` imports | Unused imports removed in 16 files. The repeated `kanban_db` imports in `dashboard/plugin_api.py` are lazy imports of an optional Hermes module, kept and exempted in `ruff.toml` |
| 10 | CANON rules are prose only | Rejected for now: CANON is a design guide for people, not a machine-checkable rule set |
| 11 | Bare `# noqa: BLE001` | Fixed: every production suppression gives its reason |
| 12 | CHANGELOG headings; dead CANON links; "CI has no Hypothesis" | Already fixed (CHANGELOG); fixed (links and the Hypothesis claim) |
| 13 | Windows lock not run on real Windows; stale skip in `tests/test_wave2_fixes.py` | Open in #43. The skip is kept: that test imports `fcntl`, which Windows lacks |
| 14 | Agent worktrees left in `.claude/` | Fixed: removed locally, and `.claude/` is ignored |
| 15 | No written audit result | Fixed: this page |

## Defects found while closing the audit (#70)

- A file in a subfolder of a watched folder was opened by its full path, so a subfolder swapped for a link after the scan could redirect the read. Nested files now open one folder at a time from the watched folder's descriptor, never through a link.
- Profile, pairing and prompt-wait folders were listed in full. Every state-folder listing now stops at the same bound and reports the cut.
- Three desktop tests passed with their fix reverted. They now fail when it is removed.
