# Documentation map

These are the maintained references for UNBL0CK. Change the relevant reference with
the behavior; link to it rather than copying recovery or review rules into another file.

| Reference | Owns |
| --- | --- |
| [Project README](../README.md) | Installation, settings, detection rules and disclosure |
| [Operator guide](operator-guide.md) | Actions, failure recovery, retention and source limits |
| [Architecture](architecture.md) | Module boundaries, API contracts and state ownership |
| [Verification](verification.md) | Reproducible checks, operator coverage and evidence limits |
| [Features](FEATURES.md) | Each documented behaviour and the tests that pin it |
| [Trust audit, 2026-11-15](TRUST-AUDIT-2026-11-15.md) | What became of each finding of the outside trust audit (#71) |
| [Maintenance handoff, 2026-11-15](HANDOFF.md) | State at 0.6.1, open issues by owner, the merge and release routine and the suggested order of work |
| [Release assessment](release-readiness.md) | Dated release recommendation, candidate scope and remaining acceptance |
| [Security review link](security-review-link.md) | Request protocol, activation, authenticated verdicts and merge gate |
| [Visual canon](../design/CANON.md) | Design tokens, layout and generated visual assets |

Operational status is evidence dated in the verification reference, not a permanent
promise. A passing local check, hosted CI, independent review, Security review and a
merged change are separate states.
