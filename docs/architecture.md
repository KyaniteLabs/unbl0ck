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
| `POST /card/ask` | Validated question history; returns a job ID immediately or an explicit admission error |
| `GET /card/ask/{job}` | Pending or repeatably readable result; terminal model errors use 502 |
| `GET /card/ask/{job}?poll=true` | Same job, with terminal model failures encoded as `{ok:false,detail}` for the client |
| `POST /kanban/answer` | Versioned native comment and optional release; settings uncertainty prevents writes |
| `POST /hide`, `POST /unhide` | Hide a specific version; restore by ID |
| `POST /outside/done` | Delete a filed record or hide a folder item version |
| `GET /pack`, `POST /pack` | Read pack representation, or explicitly write it |

POST Answer/Ask models bound card identifiers to 300 characters, answers/message text
to 20,000, and Ask history to 50 messages. Outside-text checks also bound its ID/version
to 300; GET Card does not apply the same length limit. Invalid/stale/missing references fail instead of targeting a
different item. Ask capacity returns 429; unavailable model infrastructure returns 503;
unknown/expired jobs return 404. Not every error is retryable without refreshing.

## State lifecycle

Plugin-owned files live under `<HERMES_HOME>/plugin-data/unbl0ck/`. Treat their contents
as private. Settings and source files remain owned by their original applications.

| State | Lifetime and ownership |
| --- | --- |
| `hidden.json` | Version-specific hides, capped at 1,000; shared by local operator surfaces |
| `pending-release.json` | Recovery state for comments whose release remains unfinished |
| `outside/*.json` | Owner-only agent-filed records, capped at 200; shared across profiles |
| `waits/*.json` | Owner-only live prompt records, removed on resolution/expiry/dead process |
| `bluf.json` | Summary cache capped at 2,000, version-bound; failed-source entries retained subject to cap |
| `pocket-pack.json` | Written only on explicit pack request |
| Summary/Ask jobs | Process memory; restart loses pending work and results |
| Answer drafts | Desktop session memory, 100 entries, 30 minutes after last edit; original question version retained |
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
