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
records), then deleted on the next poll, filing or action while UNBL0CK runs (there is no background timer, so records can remain on disk while UNBL0CK is not running); the agent's own `resolve` action removes a record permanently and
is the only action that means the work is finished. Re-filing identical content keeps a
Done item hidden; changed content brings it back.

Search and source filtering use already loaded text. They preserve mounted answer
boxes and conversations and do not change the total queue count. **Clear filters**
restores the list. **Esc** closes TL;DR, Context, then Full text; in Search it clears
the query. **⌘/Ctrl + Enter** submits an answer; wording that says to keep the card
blocked selects the keep-blocked action.

Typed Kanban answers also survive a card disappearing during a partial source failure
or a route remount. Up to 100 drafts are held in session memory, each expiring 30 minutes
after its last edit. A successful answer clears its draft. Drafts are kept for each backend connection separately, so a draft typed for one backend never appears on another. A full page reload or app
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
excerpt labelled as an excerpt. Password prompts are never expanded. A live prompt over
20,000 characters is stored cut, with its last 2,000 characters kept beside it in the same
wait record, so the excerpt is the true end of the prompt; a chat message is read from its
last 100,000 characters.

## What each surface shows

The same queue reaches you through five surfaces. They are deliberately not equal: the
further a surface is from the machine's owner, the less it says. This table is checked by
`tests/test_surface_contract.py`, which also pins what stays common (item identity, order
and status) and each difference below.

<!-- surface-matrix:start -->
| Surface | Visibility | Context | Action routing | Completion | Recovery | Intentional limits |
|---|---|---|---|---|---|---|
| Desktop | Every source and tier: asks, stuck, answered | Context panel, Full text, TL;DR on Kanban | Answer, release, open the session or board, Done, Not for me | After Answer + unblock, release or a filed item's Done the card leaves on the next poll (Undo banner lasts 12 seconds for Done and Not for me); after Answer, keep blocked it stays listed as answered until its state changes | Drafts and Ask jobs survive remounts; per-source errors with retry | Only inside Hermes Desktop |
| CLI and local slash | Asks only, 12 listed; says how many more and how many stuck or answered cards are not listed | One line per ask: where, title, summary or first 140 characters (a password prompt gets a fixed notice instead) | Prints the native `hermes pairing approve` command; no inline action | Run it again | Source errors are printed; run it again | Local surfaces only (`cli`, `tui`, `desktop`, `local`) |
| CLI --json | The raw queue, every tier | Full item fields | None | Run it again | Pairs with `--pack` | Machine owner only; contains private text, including Hermes's already redacted excerpt of a live password prompt |
| Remote slash | Counts only: asks, plus counts of stuck and answered cards and whether the queue is incomplete | None: never a title, text, id, name or approval command | Says to open Desktop or run `hermes unbl0ck` on the machine | Ask again after acting there | "Incomplete" is stated as a count-level fact | Anyone admitted to a gateway can type it, so an unknown or missing platform counts as remote |
| Pocket pack | Up to six asks in queue order; no stuck or answered cards; `omitted` gives counts of what is left out, and `complete` is false (with `omitted.unread_sources` counting each unreadable source once) when a source could not be read or a list was cut at its limit, so an empty pack is not "nothing needs you" | Title, short summary and ask text; a password prompt's text is replaced by a fixed notice | One fixed pointer per card ("Answer in Telegram", "Open pairing requests"); never an approval or inline action | Pack is regenerated, expires in one hour | Written only when you ask for it | Counts in `omitted` are the only disclosure of omitted work |
<!-- surface-matrix:end -->

Actions are not flattened into every surface: a surface gets a new action only after its
authorization model is defined. The local list and the Pocket pack withhold a password
prompt's text; the Pocket pack and remote output carry no pairing code or approval command
(the local list prints the native approve command for the machine's owner). Desktop and
`--json` show only Hermes's already redacted excerpt of a password prompt, to the owner.

## What you can do per source

One queue does not mean one set of abilities. This table is published as `capabilities` in
the queue payload and is checked against the code by `tests/test_capability_matrix.py`.
**Yes** means built. **By design** means withheld on purpose for safety, privacy or native
authorization: it is not a gap and will not be added. **Not yet** is a real gap.

<!-- capability-matrix:start -->
| Source | Context | Model help (TL;DR) | Reply or act in UNBL0CK | Route to the original | Dismiss |
|---|---|---|---|---|---|
| kanban | Yes | Yes | Yes | Yes | Yes |
| chat | Yes | Not yet | By design | Yes | Yes |
| prompt | Yes | By design | By design | Yes | Yes |
| pairing | By design | By design | By design | Yes | Yes |
| outside_filed | Yes | Not yet | By design | Yes | Yes |
| outside_folder | Yes | Not yet | By design | Yes | Yes |
<!-- capability-matrix:end -->

- **Context** is the Context panel for Kanban and the Full text view for chats, live prompts
  and outside items. The **+ Full text** button is on every card without a summary: it
  expands or collapses the text already on the card. Only chats and live prompts that hold a
  shortened tail, and outside items, load the whole record from the source; short chats,
  password prompts and pairing requests never fetch anything. Password prompts are never
  expanded to a larger record, and a pairing request never shows its code or the person's
  platform id.
- **Model help** is the interactive TL;DR: it sends card text to your configured provider
  only on an explicit click, and only for Kanban. It is separate from the one-line summary,
  which is on by default and is sent automatically in the background when the queue is
  read, for Kanban cards, chats, filed items and folder files (see the README disclosure;
  `bluf_enabled: false` stops it). Chats, filed items and folder files therefore get the
  summary but no follow-up questions yet. Live prompts (command approvals, password
  prompts) and pairing requests are never sent to a model. Where TL;DR is missing, the card
  says "TL;DR is not offered here" and why; a Kanban card says so when Ask is turned off.
- **Reply or act** stays in the original surface: a Kanban answer is a comment under your
  name (plus an optional release); chats are answered in the chat, prompts where they were
  asked, pairing with `hermes pairing approve` or the dashboard Pairing page, and filed items
  in the app named on the card. UNBL0CK has no route that sends a message, approves a
  pairing or answers a prompt, so the platform's own sign-in, recipient identity and
  confirmation always apply; `tests/test_capability_matrix.py` fails if a new write route
  (method and path) appears without a review.
- **Route to the original** is **Open session** / **Open chat + reply** for local platforms,
  and a line naming where to act (**Act in:** with its reference) for everything else,
  including watched folder files. Chats, live prompts, pairing requests and Kanban cards
  leave the list on the next poll once the original state changes. Filed items and folder
  files do not: UNBL0CK never checks the original app, so finishing there does not remove the
  card and you mark it **Done** yourself. Watched folder files are read-only, so there is
  nothing to open; Done only hides the card.
- **Dismiss** is **Not for me** (comes back when the source changes) and, for outside items,
  Done with Undo. Neither completes anything in the original app.
- Pocket packs name the original surface for chats, prompts, pairing requests and outside
  items ("Answer in Telegram", "Open pairing requests"). Kanban cards carry two fixed
  choices that name the task and block ("Answer and unblock <task>", "Keep <task>
  blocked"). No pack carries a pairing code, a person's platform id or an answer text.
- With Ask turned off, Ask returns 403, and 503 when the host's model interface is not
  reachable at all; the other sources are unaffected. When the interface is present but the
  provider call fails (for example a connection error), the request is accepted, polling
  reports `failed`, and a plain result request returns 502; a new attempt is allowed.

**TL;DR** starts a read-only model job. **Use as answer** copies a suggestion into the
answer box; the human still submits it. **Discuss in chat** asks the host composer to
create a draft. The original card stays unchanged by these actions.

Two summaries and four Ask calls can run concurrently. A busy Ask returns 429 with a
retry hint rather than joining an unbounded backlog. An Ask poll retries the same job
after a transport failure. A terminal model failure permits a new attempt. Completed
results can be read repeatedly: polling does not consume them. At most 128 settled
results are retained in completion order, and every job expires 15 minutes after creation.
A restart loses in-memory jobs; the UI reports that the answer is no longer available (the gateway restarted or
the result expired) and offers a fresh attempt. The client polls for up to 150 seconds per attempt, counted from the first request
and checked after every response, including a finished answer. Each Ask carries a random
request ID that Retry reuses, so a lost response cannot start a second model call, and it
carries the card version, so a changed card is refused instead of answered from old context.
A follow-up over 20,000 characters, or one the server refuses outright (a client error other than busy or timeout), is not kept in the conversation. After a busy (429) or timeout answer it stays, so Retry can send it again.


After a gateway restart the plugin never sends a request again by itself. Each Ask job
leaves a small restart marker (see below), so a poll reports one of five states:

| State | Meaning | What the card says |
|---|---|---|
| `pending` | The model is still working | Thinking |
| `complete` | The answer is available | The answer |
| `failed` | The model reported an error | The error; Retry sends a new request |
| `unknown` | The restart happened after the request was sent (or the saved record is unreadable, or the answer was finished but not kept) | "Outcome unknown ... Retry sends a new request." The provider may or may not have answered; only you can ask again |
| `not_submitted` | The restart happened before the request reached the model | "Not sent ... Retry sends it now." Nothing left the machine |

Restart markers live in `<organization HERMES_HOME>/plugin-data/unbl0ck/ask/<profile>/<job>.json`: one owner-only
(0600) file per job in an owner-only (0700) directory per profile, at most 256 per profile, deleted 15 minutes
after the request started, when the job is evicted or consumed, or immediately with
`DELETE /card/ask/{job}?card=<id>` (the call fails honestly if the file cannot be removed, and cleanup keeps retrying). The directory also holds a `.lock` file and short-lived `.gone` markers that stop a deleted job from reappearing. A marker holds the job id, state, timestamps, the card id,
provider and model names and an error message. It holds no card text, no question and no
answer. Finished answers are written to the marker only when `ask_persist_replies` is
true (default false; a restart then shows the answer for the rest of the 15 minutes).
An unreadable, oversized, foreign or symlinked marker is treated as `unknown`; an answer too large to keep is reported as such; if the storage cannot be checked the poll reports `unknown` instead of claiming the job never existed. A job whose worker is still running in another gateway process stays `pending`. A job id answers only for the card it was
created for. Markers belong to the profile's own data directory, so profiles do not see
each other's jobs.

Requests send the conversation's latest 50 messages; the server then applies
`ask_history_turns`. The conversation, any running request and its outstanding job are
kept in Desktop session memory, like answer drafts: up to 100 conversations, each
expiring 30 minutes after its last activity. Filtering, a source outage or leaving and
returning to the page restores the conversation without asking the model again; a
request still running when you left is followed to its result. A reload or app restart
clears this memory, and nothing is written to disk, so there is nothing to delete. Conversations
are kept for each backend connection separately: the same card id on another backend or
profile never shows or resends them, and closing a connection removes its conversations, its queue and its Undo list. A card and the page itself are remounted per connection, so an answer being typed or advice for the same card id never carries over to another backend.
One conversation keeps at most 100 messages and 200,000 characters; older messages are
dropped, and the card says so. A conversation that expires while its card stays open
leaves the screen too. If a request's conversation is evicted or expires while the model
is still answering, the card reports that it was dropped from session memory and offers
Retry; the late answer is not shown. The recovered conversation stays bound to the card it
was asked about: if the block version or the title, question, owner, description or comments
(including a comment rewritten in place) changed, it is marked as out of date and its suggestion
cannot be used as an answer until you ask again. A Retry after leaving and returning to the
page reuses the request's id, so a response lost in transit never starts a second model call. A late response from an older request
never replaces a newer one.

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
| Ask fault | The model, its capacity, or transport failed | Retry. A transport retry resumes the same job; after a restart the card says `unknown` or `not sent` and Retry is your decision to ask again. |
| Security reconciliation pending | A signed request has no authenticated current verdict | Follow the [Security recovery procedure](security-review-link.md). An acknowledgement is not an approval. |

## Outside-source limits

Watched folders read regular `.eml`, `.md` and `.txt` files. They skip symlinks and
dotfiles, show the newest 200 waiting files per folder (ties break on path, so the
order is stable), and read at most 256 KiB per file. By default only the folder's
direct files count. Set `outside_recursive` to true to also read nested folders, up
to 3 levels deep, 5,000 directory entries and 2 seconds per folder per poll for the scan
and 2 more for reading; files you already marked Done or Not for me do not use up the
entry bound, symlinked folders and files are never followed, and a path that resolves
outside the watched folder is rejected. Hitting any scan or read bound is reported in the
queue errors, as is a file that cannot be read (the next readable one takes its place).
Cards mark truncated exports. Full text remains within the read limit; use the
original app for the complete document.

Files you mark Done or Not for me no longer use up the 200 window, so finishing a
page brings the next older files into view. When more are waiting, the queue says how
many ("N older files are waiting") until you work through them. UNBL0CK never moves,
renames or deletes source files; a hidden file that changes comes back. The hidden
count covers every dismissed file in a folder; a listing with hidden files included shows
the newest 200 per folder and says how many more it left out.

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
