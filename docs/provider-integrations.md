# Provider integrations: plan and adapter contract

Status: plan only. UNBL0CK calls no mailbox, notes or task service today. Intake is
agent-filed records and read-only exports in watched folders (see the
[operator guide](operator-guide.md#supported-exported-formats)). This page fixes what an
integration must promise before one is built or any account access is requested.
`adapter_contract.py` holds the vocabulary and the reconciliation rules as pure code; it is
not wired into the queue and reads nothing. Nothing here authorizes connector access,
spending, or sending content anywhere.

## Today: what "done" means

A filed item's `resolve` is a claim by the agent that filed it. UNBL0CK stores the claim and
removes the card; it does not check the original app. **Done** in Desktop is local and
reversible and says nothing about the source. Neither is verified completion, and any
integration must keep that difference visible: a card finished by a claim reads
"Reported done by an agent (not confirmed)", and only a read back from the provider can
make it "Done (confirmed at the source)" (`adapter_contract.completion_label`).

## Adapter contract

An adapter is a read-only reader unless it also lists `write`, and `write` is a separate,
explicitly approved capability, never a default and never implied by reading.

| Topic | Rule |
| --- | --- |
| Identity | One opaque account id per connected account. Never an address, display name or token in the queue, logs or payloads. Items are keyed `(provider, account, external_id)`; profiles never share an account unless the operator connects it to each. Every read is bound to an account scope (the `(provider, account)` pairs it was made for): the contract refuses a read without one, and an item outside it is rejected, never applied. |
| Permissions | Request the smallest read scope that lists the needed items. Setup states the scope, what leaves the machine and what is stored, and nothing is read until the operator approves it. Write scopes are requested separately, per action kind, and can be refused without losing read access. |
| Provenance | Every card from an adapter says which provider and account it came from and that its text is untrusted content, exactly like a file export. Redaction and the disclosure matrix apply unchanged; an adapter adds a source row to that matrix before shipping. |
| Stable external IDs | Use the provider's immutable id (not a folder path or position). When the provider can renumber (IMAP UIDVALIDITY change, a moved message), the adapter re-keys through the provider's rule or reports the items as unknown; it never guesses a match from text. An external id must be unique inside its account: an IMAP id is mailbox plus UIDVALIDITY plus UID (`imap_external_id`), because a UID alone repeats across mailboxes. |
| Deduplication | The same `(key, seq)` twice is a no-op, inside a batch and across batches, and a batch folds to one event per item (the newest `seq` wins), so a deletion after an addition in one batch is not an addition. `seq` is a non-negative integer; anything else is rejected and the cursor does not move. The same `seq` with a different revision keeps the first one seen and is reported as `seq_collision`, identically inside and across batches. An older `seq` never overwrites a newer one, including an older deletion or completion; items with no card keep their `seq` too (`tombstone`, `track_done`, `advance`), so a late older event stays stale. A filed record and an adapter item for the same work are separate cards until a human links them. |
| Incremental reads | Use the provider's change feed or cursor. A cursor only moves after a successful read. A full rescan is bounded like folder scans (items, time) and reports when it stops early: the plan carries `complete`, `omitted` (or unknown) and `stop_reason`, so a partial read is never the same result as a complete one. Only a status of `ok` moves the cursor and applies items; any other status is a failure and changes nothing. |
| Changed items | A new revision updates the card's version, so a previously dismissed card resurfaces ("changed since you dealt with it"). A change that arrives while the human has an unsent local edit is reported as a conflict; nothing is written to resolve it. |
| Deletion | A confirmed deletion signal removes the card and its stored text. Absence from a read is not a deletion signal, and neither is an item leaving the watched folder or view (`left_view`: a move, a filter): that holds the card until the adapter confirms by the immutable id that it is gone. A deleted item that returns with a newer `seq` is restored. |
| Completion and reopen | Completion is read from the provider (sent, completed, archived by the provider's own rule). A reopen at the provider reopens the card and resurfaces it. A provider that still shows an item open contradicts an agent's claim or a heuristic completion, and the card stays visible with that fact, held or not. A completion the adapter only infers is flagged `heuristic` and shown as "Probably done (inferred, not confirmed at the source)"; only a provider-stated completion is "Done (confirmed at the source)". A provider state the contract does not know changes nothing. |
| Revoked access | Items from that account are held and labelled possibly stale; an authentication failure (`auth_failed`) is held the same way, and neither is treated as a rate limit. They are never completed, deleted or hidden because the read failed, and nothing is retried with other credentials. A complete successful read for the account releases the holds, including items a change feed did not repeat. |
| Rate limits | A limited read changes nothing and keeps its cursor; the next attempt honours the provider's retry time. Polling never speeds up to catch up. |
| Reading vs writing | Intake never sends, replies, edits or completes anything. A write action, if ever added, goes through the provider's own authorization and recipient confirmation, names the exact effect before it runs, and is recorded as the person's action. |

`tests/test_adapter_contract.py` runs these rules against a fake adapter: duplicate events,
changed drafts, external completion and reopen, revoked access, rate limits, conflicting
edits, and a filed claim that stays unverified. `tests/test_r4_stack15.py` replays batches
through `reconcile` and `apply` (a reference model of the store): the same item in two
batches, several revisions in one batch, tombstones, restores, holds and recovery.

## Provider matrix

Selection is explicit. Providers not listed are not supported, and nothing below is built.
Selection is a proposal for the maintainers to approve; the provider facts are the
documented behaviour as understood now and must be re-checked against current provider
documentation and a real test account before implementation.

| Provider | Kind | Wave | Minimum read | Stable id | Change feed | Deletion | Completion signal | Limit signal |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Generic IMAP (Drafts folder) | Email | 1 | Read-only login to one named folder; Sent is a separate read, approved on its own | Mailbox, UIDVALIDITY and UID (a UID alone repeats across mailboxes) | CONDSTORE/QRESYNC when offered, else UID scan | `EXPUNGE` or a vanished UID is `left_view` (a move looks the same); deletion only once the message is confirmed absent from every tracked mailbox | Draft flag cleared and message seen in Sent (a heuristic, shown as "not confirmed"); without the approved Sent read, completion stays unknown | `NO [LIMIT]` or `[UNAVAILABLE]` or a server-stated retry time is rate limited; `NO [AUTHENTICATIONFAILED]`, `[AUTHORIZATIONFAILED]` and `[EXPIRED]` are access failures (held); any other refusal is an error that changes nothing |
| Gmail | Email | 1 | `gmail.readonly` | Message and draft id | `history.list` from a history id | History deletion records | Draft gone and message in Sent | HTTP 429 with retry hint |
| Microsoft 365 / Outlook (Graph) | Email | 1 | `Mail.Read` | Immutable id (requested explicitly) | Delta query per folder | `@removed` in delta is `left_view` (the reason can be `deleted` for a move); deletion only after the immutable id is confirmed gone | Draft gone and message in Sent Items (Sent Items named in setup, since `Mail.Read` spans folders) | HTTP 429 with `Retry-After` |
| CalDAV tasks (Apple Reminders, Nextcloud) | Tasks | 2 | Read one task list | `UID` | Sync token (RFC 6578) | Removed in sync report | `STATUS:COMPLETED` | Server specific |
| Notion | Notes and tasks | 2 | Read access to one shared database | Page id | Poll by last-edited time | Archived or trashed | A status or checkbox property the operator maps | HTTP 429 with `Retry-After` |
| Apple Mail, Apple Notes (local apps) | Email, notes | none | None; local exports only | n/a | n/a | n/a | n/a | n/a |
| Any other service | | not supported | | | | | | |

Notes. Email has no completion in the task sense: a draft being sent is the closest signal
and is shown with its own wording, never as a verified decision. Wave 1 is chosen because
it covers the provider-neutral protocol and the two largest hosted services with the
smallest read scopes; wave 2 covers tasks and notes. Outlook is one row, not the design.

## Not decided here

Which wave starts first, any account setup flow, where a token would be stored (a new
persistent private store needs its own disclosure and review), and every write action.
