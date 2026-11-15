# Architecture and contracts

UNBL0CK is a Hermes plugin. The host supplies authentication, plugin routing, native
Kanban, profile discovery and the configured model API. The plugin gathers a queue
and performs explicit human-selected actions; it does not run a separate service.

| Module | Responsibility |
| --- | --- |
| `plugin.yaml` | Plugin manifest, feature settings and host entry points |
| `__init__.py` | Registration, local/messaging slash command, CLI, filing tool and prompt hooks |
| `needs.py` | Settings, five source collectors, detection, versioned mutations, redaction, persisted state and Pocket pack |
| `jobs.py` | In-memory Summary and Ask lifecycle tables, locks, expiry and bounded result retention |
| `dashboard/plugin_api.py` | Validated HTTP inputs, background execution, capacity admission and explicit HTTP failure contracts |
| `desktop/src/plugin.src.js` | Desktop queue, local drafts, actions, recovery controls and host SDK integration |
| `desktop/build.py` | Reproducible font/token inlining into `desktop/plugin.js` and `design/unbl0ck.css` |
| `scripts/security_review.py` | Trusted CI relay and authenticated GitHub verdict reconciliation |
| `scripts/install_security_review_route.py` | Owner-run installation of one native Hermes subscription |

## Read and write boundaries

Queue reads isolate failures by source. Settings failures are different: an uncertain
identity must not widen chat collection or resume model disclosure. Both raised loader
errors and Hermes' `FailedConfigRead.read_error` marker enter the fail-closed path.
Recovery resumes configured behavior only after a successful settings read.
Settings refresh and cache publication are serialized. Chat collection and answers
capture settings and their error together, so another caller's refresh or repair
cannot erase an in-flight caller's fault or change its identity snapshot.

Chat databases are opened read-only. Watched files are checked for containment,
regular-file type and identity before use; changed files are retried on a later poll.
Pairing reads use native Hermes behavior, including expiry pruning. No mail provider
or notes-service API is called.

Kanban writes go through native Hermes. Desktop sends the source version with answers;
the HTTP answer model also supports callers omitting that optional field. Hide and
folder Done send a source version; Unhide restores by ID without a version. Partial comment/release success is tracked so
release recovery need not duplicate a comment. Model calls never mutate Kanban.

Local CLI output can contain the queue; messaging slash output contains counts only.
`--json --pack` keeps stdout parseable JSON and reports the pack path on stderr.
The same selection/order feeds Desktop, CLI, slash output and Pocket pack.

## HTTP surface

Paths below are relative to the host's plugin API prefix. They depend on host access
control and are not a standalone public API.

| Method and path | Contract |
| --- | --- |
| `GET /queue` | Items, counts, feature flags and source errors; partial failure is visible in the response |
| `GET /card` | Kanban context by validated ID or board/task reference |
| `GET /outside/text` | Current bounded outside text; matching item version required |
| `GET /source/text` | Full text of one current chat or live-prompt record, read on demand; version required (409 if stale, 404 if gone, 503 if the source cannot be read, 403 for password prompts) |
| `POST /card/ask` | Validated question history; returns a job ID immediately or an explicit admission error |
| `GET /card/ask/{job}` | Pending or repeatably readable result; terminal model errors use 502; a job recovered after a restart as `unknown` or `not_submitted` uses 409; a mismatched `card` is 404 |
| `GET /card/ask/{job}?poll=true&card=<id>` | Same job, with every outcome encoded for the client: `state` is `pending`, `complete`, `failed`, `unknown` or `not_submitted` (`resubmit_safe` only when nothing was sent) |
| `DELETE /card/ask/{job}` | Deletes the job and its restart marker now |
| `POST /kanban/answer` | Versioned native comment and optional release; stale version 409 with no comment or release; empty version 400; unversioned comment-only answers still work for older callers (response `versioned:false`, `Warning: 299` header) unless `require_answer_version` is on (428, nothing written); the response reports the block `version` applied; settings uncertainty prevents writes |
| `POST /hide`, `POST /unhide` | Hide a specific version; restore by ID (also restores a filed record marked done; `restored:false` when nothing was restorable). Identifiers (and `board`/`task_id` on `/card`, `/kanban/answer`, `/outside/done`) are limited to 300 characters with no control characters (400); a malformed or non-Kanban `id`, or one that disagrees with `board`/`task_id`, is rejected instead of retargeted |
| `POST /outside/done` | Local, reversible Done: mark a filed record done (kept up to 15 minutes, at most 25) or hide a folder item version; `local_only:true`; nothing is sent or completed externally |
| `GET /pack`, `POST /pack` | Read pack representation, or explicitly write it |

POST Answer/Ask models bound card identifiers to 300 characters, answers/message text
to 20,000, and Ask history to 50 messages. Outside-text checks also bound its ID/version
to 300; GET Card does not apply the same length limit. Invalid/stale/missing references fail instead of targeting a
different item. Ask capacity returns 429; unavailable model infrastructure returns 503;
unknown/expired jobs return 404. Not every error is retryable without refreshing.

## Disclosure matrix

What can reach the configured model provider, by source. `needs.SURFACES` and
`needs.disclosure_matrix()` are the code; `GET /queue` returns the live matrix as
`disclosure` (booleans only, no text) and a test keeps this table equal to the code at
default settings. Turning `bluf_enabled` or `ask_enabled` off, or a failed settings read,
turns the matching columns off in the payload and in behavior.

| Source | Summary to model | Ask to model | Full-text search | Read across profiles |
| --- | --- | --- | --- | --- |
| `prompt` | no | no | yes | yes |
| `pairing` | no | no | no | yes |
| `kanban` | yes | yes | no | no |
| `outside` | yes | no | yes | yes |
| `chat` | yes | no | yes | yes |

Notes. `outside` covers agent-filed items and watched-folder files; both are read for every profile (filed items live in one shared folder and record their profile only for dedupe). Full-text search reads
local text and never calls a model; it skips password prompts and returns ids, versions and
counts only. Summaries and Ask run the card text, comments and owner through the same
redaction (secrets, home directory and workspace path); the operator's own typed Ask
question is sent as typed. Password prompts and pairing codes are never summarized, asked
about or returned by search. Cached summaries are keyed by item id (which carries the
profile or board) and version, so one profile's or source's summary is never attached to
another's; a corrupt cache shows no summary. Remote slash output stays counts only.

Missing evidence (issue #42): the exact-head independent Security verdict, and real-host
runs of these controls across several profiles and a live provider. Both need a reviewer or
host this repository cannot supply; the tests above run against synthetic secrets and
temporary homes.

## State lifecycle

Plugin-owned files live under `<HERMES_HOME>/plugin-data/unbl0ck/`. Treat their contents
as private. Settings and source files remain owned by their original applications.

| State | Lifetime and ownership |
| --- | --- |
| `hidden.json` | Version-specific hides, capped at 1,000, plus watched-folder Done records capped separately at 5,000 (the per-folder scan bound) so paging progress is not evicted; shared by local operator surfaces |
| `pending-release.json` | Recovery state for comments whose release remains unfinished |
| `outside/*.json` | Owner-only agent-filed records, capped at 200; visible to every profile, with `dedupe_key` private to the filing profile unless `dedupe_scope` is `global` |
| `waits/*.json` | Owner-only live prompt records, removed on resolution/expiry/dead process |
| `bluf.json` | Summary cache capped at 2,000, version-bound; failed-source entries retained subject to cap |
| `pocket-pack.json` | Written only on explicit pack request |
| Summary jobs | Process memory; restart loses pending work and results |
| `ask/*.json` | Owner-only (0600/0700) Ask restart markers, capped at 256, 15-minute retention; no card or question text; answer text only with `ask_persist_replies` (default off) |
| Ask jobs | Process memory plus the marker above; a restart recovers `not_submitted`, `unknown`, `failed` or (opt-in) `complete`, and never resends |
| Answer drafts | Desktop session memory, 100 entries, 30 minutes after last edit; original question version retained |
| Ask conversations | Desktop session memory, 100 entries, 30 minutes after last activity; question version and outstanding job retained; never written to disk |
| Display preferences | Effects and sound only, in Desktop localStorage |

Shared read-modify-write stores for hidden, summary, pending-release and filed state
use thread locks and atomic replacement. macOS/Linux additionally use `fcntl`
cross-process locking with a five-second timeout. Live prompt files and Pocket pack
use per-file atomic replacement without those shared-store locks. Windows lacks that
cross-process guarantee. Never repair corruption by silently replacing it with defaults.

Summary admission is bounded to two workers; Ask admission to four. Failed submission
must release its slot and settle/remove its pending job. Settled Ask eviction follows
completion order so a slow result cannot immediately evict itself. TTL is measured
from creation, not from the last GET; polling cannot extend retention indefinitely.

The [Security link](security-review-link.md) has a separate request/receipt protocol.
Its HMAC and receiver acknowledgement prove neither a completed review nor merge
readiness. No plugin runtime path enables automerge or supplies GitHub credentials.
