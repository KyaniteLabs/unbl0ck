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
| Identity | One opaque account id per connected account. Never an address, display name or token in the queue, logs or payloads. Items are keyed `(provider, account, external_id)`; profiles never share an account unless the operator connects it to each. |
| Permissions | Request the smallest read scope that lists the needed items. Setup states the scope, what leaves the machine and what is stored, and nothing is read until the operator approves it. Write scopes are requested separately, per action kind, and can be refused without losing read access. |
| Provenance | Every card from an adapter says which provider and account it came from and that its text is untrusted content, exactly like a file export. Redaction and the disclosure matrix apply unchanged; an adapter adds a source row to that matrix before shipping. |
| Stable external IDs | Use the provider's immutable id (not a folder path or position). When the provider can renumber (IMAP UIDVALIDITY change, a moved message), the adapter re-keys through the provider's rule or reports the items as unknown; it never guesses a match from text. |
| Deduplication | The same `(key, seq)` twice is a no-op, inside a batch and across batches. An older `seq` never overwrites a newer one. A filed record and an adapter item for the same work are separate cards until a human links them. |
| Incremental reads | Use the provider's change feed or cursor. A cursor only moves after a successful read. A full rescan is bounded like folder scans (items, time) and reports when it stops early. |
| Changed items | A new revision updates the card's version, so a previously dismissed card resurfaces ("changed since you dealt with it"). A change that arrives while the human has an unsent local edit is reported as a conflict; nothing is written to resolve it. |
| Deletion | A deletion signal removes the card and its stored text. Absence from a read is not a deletion signal. |
| Completion and reopen | Completion is read from the provider (sent, completed, archived by the provider's own rule). A reopen at the provider reopens the card and resurfaces it. A provider that still shows an item open contradicts an agent's claim of completion, and the card stays visible with that fact. |
| Revoked access | Items from that account are held and labelled possibly stale. They are never completed, deleted or hidden because the read failed, and nothing is retried with other credentials. |
| Rate limits | A limited read changes nothing and keeps its cursor; the next attempt honours the provider's retry time. Polling never speeds up to catch up. |
| Reading vs writing | Intake never sends, replies, edits or completes anything. A write action, if ever added, goes through the provider's own authorization and recipient confirmation, names the exact effect before it runs, and is recorded as the person's action. |

`tests/test_adapter_contract.py` runs these rules against a fake adapter: duplicate events,
changed drafts, external completion and reopen, revoked access, rate limits, conflicting
edits, and a filed claim that stays unverified.

## Provider matrix

Selection is explicit. Providers not listed are not supported, and nothing below is built.
Selection is a proposal for the maintainers to approve; the provider facts are the
documented behaviour as understood now and must be re-checked against current provider
documentation and a real test account before implementation.

| Provider | Kind | Wave | Minimum read | Stable id | Change feed | Deletion | Completion signal | Limit signal |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Generic IMAP (Drafts folder) | Email | 1 | Read-only login to one named folder | UID with UIDVALIDITY | CONDSTORE/QRESYNC when offered, else UID scan | EXPUNGE or vanished UID | Draft flag cleared and message seen in Sent (a heuristic, shown as "not confirmed") | Server specific; treat a refusal as rate limited |
| Gmail | Email | 1 | `gmail.readonly` | Message and draft id | `history.list` from a history id | History deletion records | Draft gone and message in Sent | HTTP 429 with retry hint |
| Microsoft 365 / Outlook (Graph) | Email | 1 | `Mail.Read` | Immutable id (requested explicitly) | Delta query per folder | `@removed` in delta | Draft gone and message in Sent Items | HTTP 429 with `Retry-After` |
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
