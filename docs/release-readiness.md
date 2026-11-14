# Release assessment: 2026-11-13

**Recommendation: prepare the release candidate, and hold publication until actual
Security review and a supported Desktop host smoke test pass.** This is a dated
engineering opinion, not release approval. No version, tag or publication is assigned
by this assessment. The Releases API returned no draft visible to the reviewing
connection at this observation; another release owner may be preparing it elsewhere.

The release should lead with reliable recovery: failed Undo stays retryable, typed
answers survive temporary source faults, interrupted model responses do not repeat
model work, malformed settings cannot widen disclosure, and mail previews select the
correct body. These changes address reproduced failures in the operator's work.

## Candidate scope and evidence

Include the corrected stack through [PR29](https://github.com/KyaniteLabs/unbl0ck/pull/29)
and the canonical documentation in [PR30](https://github.com/KyaniteLabs/unbl0ck/pull/30),
plus the final settings-race correction in [PR31](https://github.com/KyaniteLabs/unbl0ck/pull/31)
at `fac9710e18d37b56aefcb9399e15057ca3659d46`.
Earlier layers have corrections above them; assess the complete integration rather
than treating PR23 alone as the release candidate. Freeze additional feature work
for this candidate, while continuing to fix any acceptance defect that is reproduced.

The corrected cumulative application source `fac9710` passed 401 tests without skips.
Independent review reproduced a concurrent settings fault in the preceding stack and
verified that the correction keeps each caller bound to its own settings/fault snapshot.
The earlier source `b43b0ed` passed all 16 native plugin-validator checks; the final
candidate still requires its own hosted gate. Twelve rendered recovery scenarios passed through
the actual plugin API and native stores. Independent review reproduced important
faults and verified their corrections. These results support preparing a candidate;
they do not prove the packaged Desktop, production providers or live Security relay.
The [verification reference](verification.md) owns the exact execution boundaries.

## Acceptance before publication

1. Obtain the actual Security Hermes verdict and independent code review for each
   current integration delta. Recheck head/base pairs after stack landing changes
   them, and require the project CI on the final integrated commit. A webhook ACK,
   generic comment or old pair's approval cannot satisfy this gate. Follow the
   [review protocol and bootstrap](security-review-link.md).
2. On the supported Desktop host, install the exact candidate and exercise plugin
   enable/disable, settings repair, all five queue sources, answer/keep blocked/release,
   Undo, TL;DR retry, full text/Context and Discuss-in-chat. Record the artifact,
   Hermes/Desktop versions and results. The SDK harness is useful evidence but
   leaves real host integration unverified.
3. Check the release artifact, plugin version and changelog agree with the selected
   commit. Keep rollback to the previous artifact available and preserve user state.
   The release owner should confirm the package installation and rollback paths.

The Security relay is operational tooling. Include it only with its owner-activation
instructions; describe automated review as active only after a real external PR
receives and verifies a current-pair Security verdict. A manually obtained genuine
Security review can satisfy review acceptance without claiming the relay is live.

## Suggested release wording

> This release improves queue search, reading and recovery. Typed answers can survive
> temporary source failures, failed Undo remains retryable, and interrupted model
> polling can recover its existing result. Settings failures stay visible and fail
> closed, while mail previews exclude named attachments. Canonical operator and
> verification guides explain recovery steps and limits.

Retain the documented limits: drafts are session-only; watched folders select the
newest 200 eligible files; Windows has no cross-process state lock; and private Hermes
imports require revalidation on upgrades. Do not describe the finite audit as proof
that every environment or quality-of-life possibility has been exhausted.
