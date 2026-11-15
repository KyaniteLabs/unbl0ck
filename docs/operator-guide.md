# Operator guide

Start with [installation and settings](../README.md#install). The queue combines five
sources; a source failure leaves healthy sources usable and marks the result incomplete.
An empty incomplete queue does not mean everything is clear.

## Act on an item

| Source | Action | Completion and recovery |
| --- | --- | --- |
| Kanban | Type an answer; choose **Answer + unblock** or **Answer, keep blocked** | Posts one native comment. If release fails after posting, use **Release (unbl0ck)** to retry release without posting the answer again. Triage answers stay in triage. |
| Chat | Open a local session, or read where to reply on a gateway platform | Reply in the original chat. UNBL0CK does not send a message on your behalf. |
| Live prompt | Read the platform and prompt | Answer where the agent paused. Resolved, expired and dead-process prompts disappear. |
| Pairing request | Read the admin approval command or open Pairing | Approve through native Hermes. Expired requests disappear. The requester’s pairing code is never exposed. |
| Outside Hermes | Read **Act in**, then finish in the original app | **Done** is local and reversible for both kinds: it clears the card from UNBL0CK and nothing else. It never sends mail or completes the task in the original app. A filed record is kept, marked done, for 15 minutes so Undo works, then deleted. Folder **Done** hides that file version; it does not edit, move or delete the source file. |

**Not for me** hides the current item version. A source change makes it eligible again.
**Undo** history keeps the five most recent local dismissals (Not for me and Done), newest
first, each with its own Undo button. An entry lasts 12 seconds, pauses expiry while
restoration is pending, and renews its window after a failed restoration request, marked
"could not restore, try again". When an entry expires the page announces
"Undo window ended". If the item changed or its record is already gone, Undo says nothing
was restored. A filed record is kept on disk for 15 minutes after Done (at most 25 such
records), then deleted; the agent's own `resolve` action removes a record permanently and
is the only action that means the work is finished. Re-filing identical content keeps a
Done item hidden; changed content brings it back.

Search and source filtering use already loaded text. They preserve mounted answer
boxes and conversations and do not change the total queue count. **Clear filters**
restores the list. **Esc** closes TL;DR, Context, then Full text; in Search it clears
the query. **⌘/Ctrl + Enter** submits an answer; wording that says to keep the card
blocked selects the keep-blocked action.

Typed Kanban answers also survive a card disappearing during a partial source failure
or a route remount. Up to 100 drafts are held in session memory, each expiring 30 minutes
after its last edit. A successful answer clears its draft. A full page reload or app
restart clears this memory. A recovered draft retains its original question version:
if the card changed, review it, clear the old draft, then write a current answer.

## Read context and use the model

**Context**, **TL;DR** and **Discuss in chat** fetch details when requested; ordinary
queue polling does not fetch every card's detail. Failed Context and full-text requests
have their own retry controls. A failed request does not imply that its content is empty.

Queue polling carries only the last 480 characters of a chat message or live prompt,
marked with a note. **Full text** then reads the whole record (up to 20,000 characters)
from its source for that item version, and never sends it to a model. A changed or
vanished record, or an unreadable source, reports an error with Retry and leaves the
excerpt labelled as an excerpt. Password prompts are never expanded.

**TL;DR** starts a read-only model job. **Use as answer** copies a suggestion into the
answer box; the human still submits it. **Discuss in chat** asks the host composer to
create a draft. The original card stays unchanged by these actions.

Two summaries and four Ask calls can run concurrently. A busy Ask returns 429 with a
retry hint rather than joining an unbounded backlog. An Ask poll retries the same job
after a transport failure. A terminal model failure permits a new attempt. Completed
results can be read repeatedly: polling does not consume them. At most 128 settled
results are retained in completion order, and every job expires 15 minutes after creation.
A restart loses in-memory jobs; the UI reports an unknown/expired job and offers a fresh
attempt. The client polls for up to 150 seconds per attempt.

The displayed Ask conversation remains intact while that card is mounted. Requests
send its latest 50 messages; the server then applies `ask_history_turns`. Conversation
history and outstanding job selection are not restored after the card unmounts. Keep
the card open while collecting an answer, or start a fresh question when returning.

## Recover from a fault

| Visible fault | Meaning | Next action |
| --- | --- | --- |
| Incomplete queue / source error | Some sources could not be read; missing items are unknown | Repair the named source and Refresh. Continue using healthy sources. |
| Settings read failure | Identity/disclosure configuration is uncertain | Repair Hermes configuration and Refresh. Last known identity is retained; chat collection, model features and Kanban answers stay suspended until settings are readable. |
| Old-question draft notice /409 | The item changed after the draft or request was created | Read the new question, clear the old draft, then answer again. |
| Answer posted but release failed | The comment succeeded; the transition did not | Use Release. Check the card before attempting another answer. |
| Busy/retry state write | Another process owns the lock | Wait briefly and retry. Never delete a lock to bypass the writer. |
| Corrupt hidden or release state | The stored record cannot be interpreted safely | Preserve the file, repair or restore it through the owning installation, then retry. Mutations refuse to overwrite corrupt state. |
| Summary unavailable | The original text remains available | Read Full text. Automatic retry follows the bounded summary retry schedule. |
| Ask fault | The model, its capacity, or transport failed | Retry. A transport retry resumes the same job; an expired/restarted job requires a new attempt. |
| Security reconciliation pending | A signed request has no authenticated current verdict | Follow the [Security recovery procedure](security-review-link.md). An acknowledgement is not an approval. |

## Outside-source limits

Watched folders read regular `.eml`, `.md` and `.txt` files. They skip symlinks and
dotfiles, show the newest 200 waiting files per folder (ties break on path, so the
order is stable), and read at most 256 KiB per file. By default only the folder's
direct files count. Set `outside_recursive` to true to also read nested folders, up
to 3 levels deep, 5,000 directory entries and 2 seconds per folder per poll; symlinked
folders and files are never followed, and a path that resolves outside the watched
folder is rejected. Hitting any scan bound is reported in the queue errors.
Cards mark truncated exports. Full text remains within the read limit; use the
original app for the complete document.

Files you mark Done or Not for me no longer use up the 200 window, so finishing a
page brings the next older files into view. When more are waiting, the queue says how
many ("N older files are waiting") until you work through them. UNBL0CK never moves,
renames or deletes source files; a hidden file that changes comes back.

Mail uses the plain body selected by MIME structure. Attachments, filename-bearing
inline subtrees, attached messages and non-root related resources do not replace it.
HTML-only mail shows an explicit no-plain-body notice; HTML is never rendered.

Agent-filed records waiting for you are capped at 200; at the cap the queue says how
many are waiting, new filings are refused with that count (nothing is dropped,
and existing items can still be updated), and finishing one makes room.

A `dedupe_key` is scoped by `dedupe_scope`. `profile` (the default whenever the caller's
profile is known) keeps the key private to that profile, so two profiles using the same key
file two items and never update or resolve each other's. `global` shares the key across
profiles on purpose, so they update one record. A caller with no known profile defaults to
`global`. Every filed item records its `profile` and `dedupe_scope`, and the tool's reply
returns both. Records filed before scopes existed keep their keys: a profile adopts a legacy
record its own profile filed (same id, version and any local Done, so nothing resurfaces).
A legacy record that stored no profile, because the tool call did not carry one, is adopted
once by the next profile-scoped caller and then belongs to that profile. Several such records
for one key fail the call instead of choosing one. A `global` caller still updates a legacy
record, and a different profile gets its own item. Resolving by
`id` is always unambiguous; resolving by key from a caller with no profile fails when the key
matches items in several profiles. The tool's record proves what was filed, not whether the external app changed.

See [disclosure](../README.md#disclosure) before enabling model summaries or adding
watched folders. Do not include private card text, exports or raw settings in public
failure reports.
