# Verification and acceptance

Verification must name the source revision and the behavior it actually exercised.
Review, runtime execution, hosted CI, Security acceptance, merge and deployment are
separate gates. A skipped test, queued request or receiver acknowledgement is not a pass.

## Reproduce the project gate

Use Python 3.14 and the Hermes revision pinned in
[`validate.yml`](../.github/workflows/validate.yml). Install Hermes from its own frozen
lockfile and run the plugin tests in that environment, with pytest 8.4.2. Use a disposable
Hermes home containing no credentials or operator data.

```sh
python desktop/build.py
git diff --exit-code -- desktop design
python -m pytest -q -rs -p no:cacheprovider tests
hermes plugins validate . --json
```

All collected tests must run; any skip fails the hosted gate. Inspect the validator's
JSON `ok` value and warnings, rather than treating an exited command as proof. Run
`actionlint .github/workflows/*.yml` for workflow changes. Audit the exact outgoing diff
for secrets and private environment details before pushing or posting evidence.

The Desktop tests execute both source and generated JavaScript with Node. Rendered
inspection is additionally required for UI behavior changes: passing those tests alone
does not prove focus, accessible controls or the operator journey in a browser.

## Operator coverage

| Path | Execution and relevant tests | Required failure/recovery observation |
| --- | --- | --- |
| Five-source collection | `test_needs*`, `test_cover`, `test_waits`, `test_pairing_stock`, `test_outside` | One failed source cannot claim all-clear or discard healthy sources; settings uncertainty suppresses disclosure |
| Local CLI/slash and pack | `test_inbox_quality`, `test_outside`, `test_qa` | Messaging sees counts only; JSON stdout remains parseable while writing a pack |
| Answer / keep blocked / release / triage | `test_needs_hermes`, `test_api_qa`, `test_hardening`, rendered operator checks | One comment; partial release retries without another comment; stale versions never submit |
| Hide / folder Done / Undo | `test_outside`, `test_failure_quality`, `test_operator_recovery`, rendered operator checks | Failed Undo retains retry; corrupt state is preserved; folder source is untouched |
| Full text / Context / search / keys | `test_desktop_quality`, `test_desktop_contract`, rendered operator checks | Retry controls work; filters preserve mounted state; Escape and focus follow the documented order |
| Typed answer recovery | `test_operator_recovery`, rendered partial-source checks | Card disappears and returns with draft intact; changed question keeps recovered draft disabled |
| Summary / Ask | `test_bluf*`, `test_ask_job`, `test_ask_privacy`, `test_state_machines`, rendered operator checks | Bounded admission; reasoning stripped; lost terminal response can be collected with no second model POST |
| Config read failure and repair | `test_failure_quality`, independent actual Hermes parser probes | Raised, marked-dictionary and import failures fail closed, both cold and after a good config; concurrent refresh shares one settled read; another caller's repair cannot erase an in-flight fault |
| Security relay / installer | `test_security_review`, workflow lint | Native signature/limiter compatibility; pending stays non-green; exact authenticated current review required; foreign routes preserved |

## Observed evidence: 2026-11-13

The earlier cumulative application source at `b43b0ed58d89f4060387290dffddc77b6b80f165`
passed 397 tests with no skips in a disposable runtime using the pinned Hermes source.
There was one upstream Starlette/httpx deprecation warning. The generated Desktop
artifact was rebuilt and exercised. Workflow lint and the outgoing staged leak scans
passed; that is not a claim that all historical repository content was scanned clean.
The real plugin validator passed all 16 checks and reported a warning about private
Hermes imports, including Kanban, pairing and subscription installation seams. Those
internal dependencies remain version-sensitive and require revalidation on upgrades.

Twelve rendered recovery scenarios passed against the real plugin API and native
Kanban storage with synthetic source records and deterministic model responses:
failed Undo/retry; successful Ask→answer→release; keep-blocked→release; partial release
recovery; reblocked stale answer; Discuss-in-chat composer contract; model error/retry;
26 follow-ups; partial-source draft recovery; changed question during source failure;
late completion of an old send while a newer remounted draft exists; and lost terminal
Ask response with one POST and two polls. No browser execution errors were observed.
The original eight operator scenarios were rerun at `6aa5a2b`; the four focused
recovery scenarios were rerun against the final `59d854b` Desktop source included in
`b43b0ed`. The separate mail change passed its real EmailMessage regressions.

Independent code review found and drove corrections for the real configuration-error
contract, Ask result consumption and completion ordering, Undo, answer draft loss,
MIME selection, and webhook acknowledgement/rate-limit ambiguity. It is a separate
review lane from Security Hermes. Current per-PR review and CI state must be checked
live before landing; this document is not an evergreen approval of later commits.

A final independent review reproduced a further settings race in the documentation
stack at `8fbd0a0`: concurrent failed refreshes could clear the shared error before
another caller checked it, exposing unrelated chats. A subsequent repair could also
erase an in-flight caller's fault. The correction at
`fac9710e18d37b56aefcb9399e15057ca3659d46` serializes refresh and binds settings to
their fault for each chat/answer call. The cold-cache and last-good concurrent-fault
regressions both failed on the preceding source. The corrected cumulative suite
passed 401 tests with no skips in the same pinned runtime, with the same upstream
deprecation warning.

Independent review found no remaining material source findings in
`89597e3..fac9710`, including the immediate correction over `8fbd0a0`. Six bounded
concurrency/repair probes and 11 settings tests passed independently without skips.
Desktop, jobs/API, MIME and relay source files were verified unchanged, so their
earlier execution evidence is reused, not represented as a new rendered run. The
397-test run above is historical evidence and does not cover this later correction.

## Acceptance still requiring live evidence

The rendered harness exercises host SDK contracts, not a complete packaged Electron
application. Live external chat providers, production mail applications, real model
providers, Windows cross-process concurrency, and every Hermes version are not covered
by these results. Use the actual supported host for release acceptance.

The Security bridge is built and locally tested but not activated. At this observation
the owning gateway was unavailable. Activation requires the checks in the
[Security guide](security-review-link.md), including an actual submitted verdict from
the verified Security identity for the current head/base pair. No Security verdict,
automerge, deployment or production end-to-end success is claimed here.

## Design alternatives and remaining limits

The review compared in-memory draft recovery with disk/localStorage persistence;
session memory preserves short fault recovery without adding private draft storage.
Losing drafts on full reload remains explicit. Ask conversation restoration after
unmount remains a separate possible enhancement; transport retry within a mounted card
is implemented and verified.

Bounded folder selection discloses older omissions instead of silently scanning an
unbounded directory. Folder pagination and automatic export archiving were not adopted;
the latter would change the read-only source contract. Filed dedupe keys remain shared
across profiles; callers can namespace them without migrating existing records.

The review link reuses native signed ingress and verifies GitHub review identity rather
than adding a listener, executing PR code with secrets, or treating duplicate responses
as admission proof. Independent code review and Security review remain separate gates.
This is a documented finite review of the present operator paths, not proof that every
possible quality-of-life improvement or failure mode has been exhausted.
