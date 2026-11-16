# UNBL0CK maintenance handoff

As of 2026-11-15

UNBL0CK 0.6.1 is released, main is green on Linux, Windows and macOS, and no pull requests are open. The audit and fix rounds are over; from here the repo needs routine maintenance, and the eight open issues are product and engineering work.

## Where things stand

Main is at 64a5035 (the #88 merge), and every CI job on it passes.

| Item | State |
| --- | --- |
| Latest release | v0.6.1, published on GitHub Nov 15 (the first published release; v0.6.0 is a tag only) |
| `plugin.yaml` version | 0.6.1 |
| Open pull requests | None |
| CI: `validate` | Desktop build and the full test suite on Linux, pytest 9.1.1 (the version Hermes pins) |
| CI: `platform-state` | State lock, Ask journal and recovery tests on Windows and macOS, Python 3.14 |
| CI: `lint`, `typecheck` | ruff; mypy 1.18.2 against a baseline of 69 known errors, 0 new allowed |
| CI: bite check | Each `fix` commit's new tests must fail on its parent commit |
| Security review workflow | Reconciles the independent Security review on PRs every 30 minutes |
| Last full local run | 1,724 passed; ruff clean; mypy 69 baseline, 0 new |

Last real-runner results: macOS 100 passed, 11 skipped; Windows 20 passed, 91 skipped. Windows skips most of these tests on purpose, because the Ask journal and the recovery tool switch themselves off there. One Windows test is skipped because of a known bug (see #43).

## Open issues

Eight issues are open. Three are Windows engineering work, three need real hosts, exports or accounts, and one needs a product decision. The eighth (#49) tracks the other seven. Each one already has a status comment with evidence from Nov 15.

| Issue | What is left | Kind of work | Next step |
| --- | --- | --- | --- |
| [#43](https://github.com/KyaniteLabs/unbl0ck/issues/43) Windows state safety | When six processes write the same records at once, they time out on the Windows `msvcrt` state lock ("state lock busy"). Windows crash and permission behaviour has not been tested on a real host. | Engineering | Fix the lock under contention (fairness or backoff), then remove the skip on `SameKeyAcrossProcesses` |
| [#35](https://github.com/KyaniteLabs/unbl0ck/issues/35) Ask job recovery | The Ask journal is POSIX-only. On Windows it reports "unavailable", so a restart can lose Ask jobs | Engineering | Design a Windows journal, or document Windows as best-effort |
| [#48](https://github.com/KyaniteLabs/unbl0ck/issues/48) Recovery workflow | The recovery tool refuses to run on Windows because Windows has no directory descriptors | Engineering | Same Windows filesystem layer as #35; do them together |
| [#38](https://github.com/KyaniteLabs/unbl0ck/issues/38) Exported content | The tests use only synthetic messages; there are no real Outlook, Gmail or Thunderbird export fixtures | Needs real exports | Collect redacted real exports, then set format priorities from them |
| [#44](https://github.com/KyaniteLabs/unbl0ck/issues/44) Host and install acceptance | No receipts yet from a packaged host, a provider, or an install, upgrade and rollback | Needs a real host | Run the operator guide's install, upgrade and rollback steps on a packaged host and record the results |
| [#42](https://github.com/KyaniteLabs/unbl0ck/issues/42) Privacy and disclosure | Per-source disclosure controls are not built, and no independent Security review has accepted the exact release head | Product + Security | Decide the controls, then request a Security review of that head |
| [#39](https://github.com/KyaniteLabs/unbl0ck/issues/39) Provider integrations | Provider selection and rollout waves are still an unapproved proposal | Product decision | Approve or cut the proposal in `docs/provider-integrations.md` |
| [#49](https://github.com/KyaniteLabs/unbl0ck/issues/49) 0.5.1 follow-up tracker | Nothing of its own; it closes when its child issues close | Tracking | Update it whenever a child issue closes |

## Maintenance routine

CI already runs every check below on each PR. Run them locally too before you push, because a failure costs less to find on your machine. The exact commands are in `docs/verification.md`.

Before every merge:

1. `python desktop/build.py`, then `python -m pytest -q -rs -p no:cacheprovider tests` in an environment with pytest 9.1.1. The suite must pass with no unexpected skips; `validate` fails on any skip.
2. Run `ruff check .` and `python .github/scripts/typecheck.py`. There must be no new mypy errors beyond `.github/mypy-baseline.txt`.
3. For a `fix` commit, run `python .github/scripts/new_tests_bite.py --base github/main`. Each new test must fail on the commit before the fix. Put guard tests that pass on both in a separate `test:` commit. Always pass the main branch as the base, not an intermediate commit.
4. If a workflow changed, run `actionlint .github/workflows/*.yml`.
5. Read the outgoing diff for anything private (see the next section).
6. Merge with `--no-ff` and fast-forward main to the merge commit. Never force-push.

To cut a release:

1. Bump `plugin.yaml` and add a dated `CHANGELOG.md` section with a "Known gaps" line naming the open issues.
2. Merge it through a PR with green CI.
3. Tag the merge commit with an annotated tag `vX.Y.Z`.
4. Publish the GitHub release with the changelog section as its notes. A tag on its own is not a release; 0.6.0 shipped only as a tag.

Weekly:

- Read new issues and Security review results, and reply on each with evidence: a file path, a test or a CI run.
- Bump pinned tool versions (pytest, hypothesis, mypy, setup-python by commit SHA) only when Hermes moves its own pins.

## Rules that keep the public repo safe

The repo is public, so every push, comment, tag and release can be read by anyone. These rules come out of the audit rounds.

- **Check for leaks before anything goes public.** Diffs, PR text, issue comments and release notes must contain no credentials, local paths, host names, private email addresses or internal tool and model names. Describe a check by what it did ("an acceptance check of main"), not by the tool that ran it.
- **Main only moves forward.** Merges are fast-forward only, with no force-push. Remote branches are never deleted, and tags are never moved or deleted.
- **A merge needs green CI.** If main moved after the last green run, re-run the gates on the merged tree.
- **Every fix comes with a regression test that fails without it.** Fix the whole class of bug, not only the one instance. Review only the fix diff. A new high-severity finding blocks the merge; fix medium findings before the next release.
- **A skip needs a stated reason.** Skip a test only where the code switches the feature off by design, or for a known bug that stays open in an issue. Never weaken an assertion to make a test pass.
- **Nothing goes upstream to Hermes without the owner's approval.**

## Back to product and engineering

Start with the two product decisions. They decide what the engineering work is for, and nothing else unblocks them.

1. **Decide on providers and disclosure (#39, #42).** Approve or cut the provider proposal, and choose the per-source disclosure controls. Product owns both.
2. **Build the Windows filesystem layer (#43, #35, #48).** Fix the lock under contention first, because it can lose writes today. Then build the journal and the recovery tool on one shared Windows layer. CI already runs these tests on Windows, so each fix can be checked by removing a skip.
3. **Collect real-world evidence (#38, #44).** Redacted real exports and one packaged-host install, upgrade and rollback run. Their results close the acceptance gaps on #49.
4. **Ship 0.7.0** when items 1 and 2 have landed, following the release steps above.

## Links

- [Repository](https://github.com/KyaniteLabs/unbl0ck)
- [v0.6.1 release](https://github.com/KyaniteLabs/unbl0ck/releases/tag/v0.6.1)
- [#43 Windows results comment](https://github.com/KyaniteLabs/unbl0ck/issues/43#issuecomment-6906779212)
- [Open issues](https://github.com/KyaniteLabs/unbl0ck/issues)
- In the repo: `docs/verification.md` (checks and acceptance matrix), `docs/operator-guide.md` (hosts, install, rollback, recovery), `docs/provider-integrations.md` (#39 proposal), `CHANGELOG.md`
