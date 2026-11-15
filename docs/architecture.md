# Architecture and contracts

UNBL0CK is a Hermes plugin. The host supplies authentication, plugin routing, native
Kanban, profile discovery and the configured model API. The plugin gathers a queue
and performs explicit human-selected actions; it does not run a separate service.

| Module | Responsibility |
| --- | --- |
| `plugin.yaml` | Plugin manifest, feature settings and host entry points |
| `__init__.py` | Registration, local/messaging slash command, CLI, filing tool and prompt hooks |
| `needs.py` | Settings, five source collectors, detection, versioned mutations, redaction, persisted state and Pocket pack |
| `jobs.py` | Summary and Ask lifecycle tables (in memory), the Ask restart journal (one small file per job), locks, expiry and bounded result retention |
| `dashboard/plugin_api.py` | Validated HTTP inputs, background execution, capacity admission and explicit HTTP failure contracts |
| `desktop/src/plugin.src.js` | Desktop queue, local drafts, actions, recovery controls and host SDK integration |
| `desktop/build.py` | Reproducible font/token inlining into `desktop/plugin.js` and `design/unbl0ck.css` |
| `recovery.py` | Operator CLI: diagnose the shared state files, preserve their bytes, restore a verified backup; counts only, never clears a lock |
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
| `POST /card/ask` | Strictly validated question history (user and assistant turns only, ending with a user question), optional card `version` (409 if the card was blocked again) and optional `request_id` (a repeat returns the same job); returns a job ID immediately or an explicit admission error. Admission (429) is decided before the card is read |
| `GET /card/ask/{job}` | Pending or repeatably readable result; terminal model errors use 502; a job recovered after a restart as `unknown` or `not_submitted` uses 409; a missing `card` is 400 and a mismatched `card` is 404 |
| `GET /card/ask/{job}?poll=true&card=<id>` | Same job, with every outcome encoded for the client: `state` is `pending`, `complete`, `failed`, `unknown` or `not_submitted` (`resubmit_safe` only when nothing was sent) |
| `DELETE /card/ask/{job}?card=<id>` | Deletes the job and its restart marker now; `card` is required and must match; if the marker cannot be removed the call fails and cleanup retries until storage is writable |
| `POST /kanban/answer` | Versioned native comment and optional release; stale version 409 with no comment or release; empty version 400; unversioned comment-only answers still work for older callers (response `versioned:false`, `Warning: 299` header) unless `require_answer_version` is on (428, nothing written, decided on the same settings snapshot the write uses; a release without a version stays 400); the response reports the block `version` applied; settings uncertainty prevents writes |
| `POST /hide`, `POST /unhide` | Hide a specific version; restore by ID (also restores a filed record marked done; `restored:false` when nothing was restorable). Identifiers (and `board`/`task_id` on `/card`, `/kanban/answer`, `/outside/done`) are limited to 300 characters with no control characters, including the C1 range and line/paragraph separators (400); a malformed or non-Kanban `id`, or one that disagrees with `board`/`task_id` (the same board spelled differently, such as `DEFAULT`, agrees), is rejected instead of retargeted |
| `POST /outside/done` | Local, reversible Done: mark a filed record done (kept up to 15 minutes, at most 25) or hide a folder item version; `local_only:true`; nothing is sent or completed externally |
| `GET /pack`, `POST /pack` | Read pack representation, or explicitly write it |

POST Answer/Ask models bound card identifiers to 300 characters, answers/message text
to 20,000, and Ask history to 50 messages. Outside-text checks also bound its ID/version
to 300; GET Card applies the same 300-character limit to its ID and board/task reference (400).
Oversized identifiers in a POST body fail validation with 422. Invalid/stale/missing references fail instead of
targeting a different item. Ask capacity returns 429; unavailable model infrastructure returns 503;
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
| `kanban` | yes | yes | no | yes |
| `outside` | yes | no | yes | yes |
| `chat` | yes | no | yes | yes |

Notes. `outside` covers agent-filed items and watched-folder files. Filed items live in one shared folder, so every profile reads them (the filing profile is recorded as provenance and scopes `dedupe_key`); that is what the `outside` row's yes means. Watched folders are not shared that way: each profile's process reads only the `outside_dirs` its own settings configure, so two profiles read different folders unless they configure the same ones. `kanban` is read across profiles because Hermes keeps one Kanban store shared by every profile; a blocked card is visible from all of them. Full-text search reads
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
| `hidden.json` | Version-specific hides, capped at 1,000 (the oldest dismissal goes first), plus Not for me and Done on watched-folder files, capped separately at 5,000 per watched folder (the per-folder scan bound; up to 8 folders) so paging progress is not evicted, and always kept under the 16 MiB state limit by dropping the oldest; a hide for a version older than the card's current one is refused; shared by local operator surfaces |
| `chat-backfill.json` | The oldest time a queue read reached and the ids (at most 1,000) of chats only a widened `chat_window_days` found, so they are never summarized automatically; no chat text |
| `.state.lock` | Empty owner-only (0600) lock file that serializes writes to the files above across threads and processes; holds no data |
| `pending-release.json` | Recovery state for comments whose release remains unfinished, capped at 1,000; a new marker is refused and reported when full, and an older version never replaces a newer one; a release reserves its marker before the answer is written, so a failed or raised release is always recoverable |
| `outside/*.json` | Owner-only agent-filed records, capped at 200; visible to every profile, with `dedupe_key` private to the filing profile unless `dedupe_scope` is `global` |
| `waits/*.json` | Owner-only live prompt records, removed on resolution/expiry/dead process |
| `recovery/*.bak` | Byte-for-byte copies of a shared state file, written only when an operator runs `recovery.py preserve` or `restore --apply` (directory 0700, files 0600). They hold the same private content as the file they copy and are never deleted by the plugin; remove them yourself when no longer needed |
| `bluf.json` | Summary cache capped at 2,000, bound to the item version and to a hash of its title, text, owner and comment count (an edit or a new comment is summarized again); failed-source entries retained subject to cap |
| `pocket-pack.json` | Written only on explicit pack request |
| Summary jobs | Process memory; restart loses pending work and results |
| `ask/<profile>/<job>.json` | Owner-only (0600/0700) Ask restart markers, one directory per profile, capped at 256 per profile, 15-minute retention, with a `.lock` file and short-lived `.gone` delete markers; no card or question text; answer text only with `ask_persist_replies` (default off) |
| Ask jobs | Process memory plus the marker above; a restart recovers `not_submitted`, `unknown`, `failed` or (opt-in) `complete`, and never resends |
| Answer drafts | Desktop session memory, 100 entries, 30 minutes after last edit; original question version retained |
| Ask conversations | Desktop session memory, 100 entries, 30 minutes after last activity; question version and outstanding job retained; never written to disk |
| Display preferences | Effects and sound only, in Desktop localStorage |

An answer with release is one Kanban write transaction: a stale version is refused (409) with no comment
written, and a release that fails after the answer is written (false or an error) leaves the comment and a
recovery marker for the retry. Duplicate detection looks only at comments made since the current block.

Shared read-modify-write stores for hidden, summary, pending-release and filed state
use thread locks and atomic replacement. macOS/Linux additionally use `fcntl`
cross-process locking with a five-second timeout. Live prompt files and Pocket pack
use per-file atomic replacement without those shared-store locks. Windows lacks that
cross-process guarantee. Never repair corruption by silently replacing it with defaults.

State reads and writes never follow a link: a symbolic link in place of `plugin-data/unbl0ck`,
its subfolders, the lock file, a state file or a temporary file is refused, and a state file
that is not a regular file or is over 16 MiB is treated as unreadable. A state file whose
entries are not in the shape this version writes is likewise left untouched until repaired.

Summary admission is bounded to two workers; Ask admission to four. Failed submission
must release its slot and settle/remove its pending job. Settled Ask eviction follows
completion order so a slow result cannot immediately evict itself. TTL is measured
from creation, not from the last GET; polling cannot extend retention indefinitely.

The [Security link](security-review-link.md) has a separate request/receipt protocol.
Its HMAC and receiver acknowledgement prove neither a completed review nor merge
readiness. No plugin runtime path enables automerge or supplies GitHub credentials.
