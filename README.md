# UNBL0CK

See the [canonical documentation map](docs/README.md) for operator recovery, architecture,
verification, and the [Security Hermes review link](docs/security-review-link.md).

Agents get stuck waiting on you, and those questions end up scattered across Kanban boards, chats and other apps. UNBL0CK gathers them into one queue: every blocked Kanban card waiting on your answer, every chat on any platform (Desktop, CLI, TUI, Telegram, Discord and the rest) whose last turn is the bot asking you something, every agent paused right now on a clarify question, a command approval or an admin password prompt, every new person waiting for you to approve their pairing request, and mail drafts, call notes or other items outside Hermes filed by your agent or read from watched folders.

To answer a card, type into the queue. UNBL0CK posts your answer as a comment through native Kanban and, if you choose, unblocks the card. A chat opens straight from the queue. Each card leads with a one-line summary of what you must decide, and the original text is one click away. Nothing is decided for you and silence never counts as a yes. UNBL0CK adds no ranking of its own: within a source, asks come before stuck and answered cards, then by board priority, newest first.

## What you get

- **Desktop:** an `/unblock` page showing the live queue, plus a "Needs you" count. Each card leads with a plain-English bottom line (BLUF) written by your configured model, with the raw block reason under **+ Full text**. Each Kanban card has **Answer + unblock**, **Answer, keep blocked**, **Open board** and **Not for me**, plus a lazy **Context** panel with the description, comments, parent/child cards, workspace and branch, recent events, last run outcome and attachments. **TL;DR** asks your model for the card's bottom line in plain words (what it wants from you and what to pick) in one click, and takes follow-up questions in a small read-only side chat; **Use as answer** copies its last reply into the answer box, and **Discuss in chat** opens a normal Hermes chat with the card pre-seeded. The model never acts: you still click Answer. **Not for me** can be undone for 12 seconds; after that, hidden items come back when the source changes: a new block, or a new chat turn. If your answer reads like "keep it blocked", the card warns you before **Answer + unblock**. **Esc** closes TL;DR, then Context, then Full text.
- **Slash command:** `/unbl0ck` lists what is waiting on you in the CLI, TUI and Desktop. On a messaging platform (Telegram, Discord and the rest) anyone the bot admits can type it, so there it only says how many things are waiting, never what they are.
- **CLI:** `hermes unbl0ck` (and `/unbl0ck` on a local surface) prints the queue, naming who each gateway chat is with and giving the approve command for each pairing request. Add `--json` for the raw queue, or `--pack` to write a small card-format JSON pack for other displays to read.

## Install

```
hermes plugins install unbl0ck
```

Then set your name, so UNBL0CK knows which asks are aimed at you. Use the plugin's settings in Desktop (**Settings → Plugins → UNBL0CK**), or:

```
hermes config set plugins.entries.unbl0ck.settings.principal_name "Ada, Ada Lovelace"
```

`principal_name` is comma-separated: list the names your agents use for you. The words in `role_words` (see `plugin.yaml` for the default), "principal" and "human" always count. Your answers are posted under the first name, unless you set `comment_author`. If no name is set, only those generic words count as your name: a one-to-one chat or a Kanban block that says "your approval" is still detected, but in a group, channel or thread the ask must contain one of them.

**Summaries** are on by default. Turn them off with `bluf_enabled: false`; cards then show the raw block reason.

**Watched folders** are off by default. Set `outside_dirs` to comma-separated absolute paths (`~` is expanded), or use `UNBL0CK_OUTSIDE_DIRS` when the setting is empty:

```
hermes config set plugins.entries.unbl0ck.settings.outside_dirs "~/Documents/Drafts,~/Documents/Plaud"
```

**TL;DR** (the Ask feature) is on by default. Turn it off with `ask_enabled: false` in the same settings block (or `hermes config set plugins.entries.unbl0ck.settings.ask_enabled false`). The Context panel and **Discuss in chat** keep working when Ask is off.

Requires Hermes `>= 0.23.0`.

## How detection works

- **Kanban:** a card counts as needing you when its latest block is clearly waiting on you. Either the block reason names a decision for you ("Awaiting Ada's approval", "human gate: credentials"), or it is a `needs_input` block that names you. Blocks that only report an order ("Ada said stop") are skipped. Answered and stuck cards can remain visible in the queue, but do not count in the needs-you total or Pocket pack. A card you answered with **Answer, keep blocked** stays listed, marked answered, with a **Release (unbl0ck)** button for when it is ready; other answered cards leave the queue.
- **Chats:** any chat a person uses, on any platform, from the last 14 days (configurable up to 90 with `chat_window_days`; the queue says which window it used; chats older than 14 days are listed but never summarized automatically) counts when the bot's final visible turn asks you something and you have not replied after it. Agent-to-agent sessions (Kanban workers, cron, subagents, one-shot runs) are skipped. A long chat that Hermes compressed into a continuation is checked only at its newest link. In a group, channel or thread the ask must name you, because there the bot may be talking to someone else. On Telegram, Discord and other gateway platforms other people can DM the bot too, so the card says who the chat is with; set `owner_user_ids` to your own platform user ids (comma-separated) and chats with anyone else are left out. Desktop, CLI and TUI chats open in place; for Telegram, Discord and other platforms the card tells you where to reply.
- **Pairing requests:** when someone new messages a bot and gets a pairing code, they wait under **ACCESS.REQUESTS** until you approve them or the request expires after an hour. The card shows the exact command, `hermes pairing approve <platform> <request-id>` (the admin-side id the dashboard's Pairing page also uses), or you can approve on that page. UNBL0CK asks the gateway's own pairing store (`PairingStore.list_pending()`), which never returns the code they were sent, and never shows the person's platform user id. Like `hermes pairing list`, that call clears expired requests as it reads.
- **Live prompts:** when any agent pauses on a clarify question, a dangerous-command approval or an admin password prompt, Hermes fires its stock `on_human_input_request` hook and UNBL0CK lists it under **LIVE.PROMPTS** until `on_human_input_resolved` fires. Answer it where it was asked; UNBL0CK shows the prompt (already redacted by Hermes) and the platform it is waiting in. A prompt whose process has exited, or that has been open for over a day, is dropped on the next poll, so a crash or a missed hook never leaves a ghost behind. Hooks load when the gateway starts, so a newly installed UNBL0CK picks up live prompts after the next gateway restart.

The queue is ordered the way it is urgent: live prompts first (a run is paused), then pairing requests (they expire), then Kanban cards (asks before stuck and answered cards, then by board priority, newest first), then outside items, then chats. The slash command, CLI and Pocket pack use the same order. The pack bounds each card's text (a cut ends in `[truncated]`, and `complete` is false when text was cut or cards were left out), names the board and block in every Kanban action, and offers no unblock for block-loop cards.

What it cannot see: a card that is waiting on you without saying so in its block reason, and a chat question so indirect that it does not read as a question for you.

### Things waiting outside Hermes

Mail drafts waiting to be sent and call notes waiting to be read appear under **OUTSIDE.HERMES** through either of two inputs:

- Your agent calls **`unbl0ck_file`** with `kind` (`draft`, `notes`, `approval` or `other`), `title`, `body` and `act_where` (for example, "Outlook → Drafts" or "Plaud app"). Optional `ref` holds an id, URL or path for you to find the original; it stays plain selectable text and is never fetched or opened automatically. These cards say **FILED BY AGENT**: UNBL0CK records the agent's claim, without checking the external app. A `dedupe_key` lets the agent update the same item in place; it is private to the filing profile unless `dedupe_scope` is `global`, and a global key is cleared with `dedupe_scope: "global"` on the resolve call too. To clear it, call the same tool with `action: "resolve"` and either its returned `id` together with the returned `version`, or its `dedupe_key`. With `version`, an item another agent has refiled since is not resolved. Without `version` (allowed only for a `dedupe_key`), the resolve clears whatever record currently holds that key, a refile included, so pass `version` whenever another agent may refile the same key. Filed items are capped at 200; titles, bodies, action locations and references are limited to 160, 8000, 120 and 300 characters and are cut to fit, while a `dedupe_key` over 120 characters is refused rather than shortened. A cut body ends with `[truncated]`.
- A **watched folder** supplies `.eml` drafts and `.md`/`.txt` notes. Outlook can **Save As** `.eml`, and many mail clients keep drafts as `.eml` in a folder. Plaud exports transcripts and summaries as `.txt`/`.md`. UNBL0CK reads the subject, recipient and first plain-text mail part, never HTML; a note's first non-empty line becomes its title. Symlinks, dotfiles and paths escaping the folder never count; subfolders count only when `outside_recursive` is true (3 levels, 5,000 entries not already marked Done or Not for me, and 2 seconds each for scanning and for reading per poll, reported when hit). Each poll shows the newest 200 waiting files per folder, and Done or Not for me brings older ones into view; files are read at most 256 KiB each. These cards say **FOLDER**.

Each card shows **Act in:** and its reference so you can finish in the original app. For an agent-filed card, **Done** marks the record done and offers **Undo**; the filed record is kept on disk for 15 minutes (at most 25 retained), then removed. Cleanup runs on the next poll, filing or action while UNBL0CK runs; there is no background timer, so a done record can stay on disk while UNBL0CK is not running. For folder cards, **Done — hides until the file changes** hides that file's current version, exactly as **Not for me** does. UNBL0CK never moves, edits or deletes your watched files. Both kinds offer **Not for me** and the existing BLUF summary with **+ Full text**. With summaries enabled, call notes get a bottom line so you can decide what needs reading.

Use **Search queue** and the source selector to find a card by title, preview, app, recipient or reference. The filter box searches only text already loaded in the queue and says so; **Search full text** makes one bounded, read-only look at the stored text behind the cards, reports what it could not cover, and never calls your model. The status count keeps showing the whole queue, and filtering preserves typed answers and open conversations. **Clear filters** restores the list; **Esc** clears the search box. Every card has a keyboard-accessible **+ Full text** button, including when summaries are off. Failed full-text and Context requests have inline retry controls. **Done** on either kind offers the 12-second **Undo**.

Typed answers and Ask conversations also survive temporary source failure and route remounts in bounded session memory (never written to disk, cleared on reload), with
their original question version. See the [operator guide](docs/operator-guide.md) for
retention, stale-draft recovery and the watched-folder selection limit.

Mail body selection excludes attachments and attached messages. HTML-only drafts keep the no-plain-text-body notice. An export larger than 256 KiB shows a preview-limit notice so you know to read the complete file in its original app.

## Disclosure

The things you would want to know before installing:

- **No telemetry.** UNBL0CK sends no usage data anywhere. The Desktop page talks only to this plugin's own backend, through the plugin SDK.
- **Summaries send item text to your model provider, once per change.** With `bluf_enabled` on (the default), each new or changed Kanban card's, chat's, agent-filed item's or watched-folder file's title and text (text cut to 6000 characters; live prompts and pairing requests are never sent), plus a Kanban card's last six comments (each cut to 600 characters, so the summary skips what is already settled), go to the model provider configured in Hermes, through Hermes's own plugin LLM API (logged in its plugin audit log), in a background thread. The result is cached in `bluf.json`; nothing is resent until the item changes (a new version, an edited title or text, a new owner or a new comment), except that a failed summary is retried after 10 minutes, then 20, and is given up after 3 tries until the item changes. The open page and its status chip poll the queue every 45 seconds, so with the setting on, new items are summarized without you opening them. Set `bluf_enabled: false` to send nothing automatically.
- **Ask sends card content to your model provider when you ask.** Clicking **TL;DR**, or sending a follow-up there, sends that card's context (without its workspace path, which is also replaced with `[workspace]` wherever it appears in the card text; your home folder shows as `~`, and secret-looking values such as tokens, passwords and credentials in URLs are replaced with `[redacted]`, as they are in summaries) and the question to the model provider configured in Hermes, through Hermes's own plugin LLM API, and logs the call in Hermes's plugin audit log. Long card fields are cut to fit and end in `[truncated]`, the block reason is kept ahead of the description, and your own question is sent whole. **Discuss in chat** only types a draft into a normal Hermes chat; the chat you then start makes its own model calls. Turn Ask off with `ask_enabled: false`.
- **No credentials.** There is no Outlook, Gmail or Plaud API integration and no account credentials are needed. Those items reach the queue through your agent filing them with `unbl0ck_file` or through a watched folder. The text you file or export may itself contain private information; choose watched folders accordingly.
- **Reads outside its own data:**
  - Kanban boards, through Hermes's Kanban modules (`hermes_cli.kanban_db`).
  - The `state.db` chat history of the root Hermes home and of every profile under it, opened **read-only**, so it can find chats that end with a question for you.
  - Pending pairing requests for the Hermes home and each profile, through the gateway's `PairingStore.list_pending()` (never the codes): only the display name, platform, request age and the admin-side request id (for the approve command) are used. Listing goes through the store exactly as `hermes pairing list` does, so it also prunes expired requests and may create the store's folder. Because that store reads a damaged pending-requests file as "no requests", UNBL0CK also parses each `*-pending.json` once and drops the result, only to report a damaged file as a failed Pairing source instead of an empty one; no field of it is read, kept or shown.
  - Hermes's `on_human_input_request` / `on_human_input_resolved` hook payloads (kind, redacted prompt, session id, platform).
  - Only the folders configured in `outside_dirs` (or `UNBL0CK_OUTSIDE_DIRS`): eligible `.eml`, `.md` and `.txt` files, read-only within the limits above.
- **Writes:**
  - Only when you click Answer: one comment on the card, under your configured author name, and an unblock if you chose **Answer + unblock**. Nothing else is ever written to Kanban. The Context panel and Ask never write to the card.
  - Its own files under `<HERMES_HOME>/plugin-data/unbl0ck/`: `ask/<profile>/<job>.json` (one owner-only restart marker per Ask request in an owner-only directory per profile, at most 256 per profile, deleted after 15 minutes, plus a `.lock` file and short-lived `.gone` delete markers; it holds the state, timestamps and card id but no card text, question or answer, unless you set `ask_persist_replies: true` to keep finished answers there), `hidden.json` ("Not for me" and Done on every card except watched-folder files, capped at 1000; the same two actions on watched-folder files are capped separately at 5000 per watched folder, for up to 8 folders, so paging through a large backlog keeps its progress; the oldest entries go first, and the file is also kept under the 16 MiB state limit), `waits/<id>.json` (one small owner-only file per live prompt, deleted when it is answered), `outside/<id>.json` (one owner-only 0600 file per agent-filed item, capped at 200 active, plus at most 25 retained done records for 15 minutes, so up to 225 files on disk; deleted by the next cleanup after expiry or on agent resolve), `bluf.json` (cached summaries, capped at 2000; watched-folder summaries are shared across profiles, and failed-source summaries are retained, subject to that cap) and `chat-backfill.json` (the oldest time a queue read reached and the ids, at most 1000, of chats only a widened `chat_window_days` found, so they are never summarized automatically; no chat text) and `pocket-pack.json` (only when you ask for the pack).
  - In Desktop, two display toggles (effects and sound) in `localStorage`.
- **No shell commands and no self-updating.** The only subprocess is `ps -o lstart=` on hosts without `/proc` (macOS, the BSDs), to read a waiting process's start time so a reused PID is not mistaken for it. Summaries and Ask replies run in small in-process thread pools inside the Hermes backend.
- **Sound:** off by default. Switch on **SFX** and the Desktop page plays short synthesized UI sounds through Web Audio; no audio context is created before that. Your choice is kept in `localStorage`.
- **Hermes internals:** Kanban access uses Hermes's internal `hermes_cli.kanban_db*` modules, because no public plugin API for Kanban comments and unblocks exists yet. To keep each poll fast, the queue also reads the newest block event and your latest comment per card with one indexed SQL query each, falling back to the public `list_events`/`list_comments` if the schema differs. A Hermes release that changes those modules can break the queue until UNBL0CK is updated. `hermes plugins validate` reports this as a warning. Settings are read with `hermes_cli.config.load_config_readonly()` and the Hermes home with `hermes_constants.get_hermes_home()`.
- **Optional environment fallbacks:** `UNBL0CK_PRINCIPAL`, `UNBL0CK_AUTHOR`, `UNBL0CK_OWNER_IDS` and `UNBL0CK_OUTSIDE_DIRS` stand in for `principal_name`, `comment_author`, `owner_user_ids` and `outside_dirs` when those settings are empty. None is required and none holds a secret.
- **`require_answer_version`** (default off): when on, `POST /kanban/answer` refuses comment-only answers that omit the queue item version (428, nothing written); a release without a version is still refused with 400. Desktop always sends it; the setting is for scripted callers.

## Platform limits

State files (`hidden.json`, `bluf.json`, `pending-release.json`) are guarded by a thread lock everywhere and by an `fcntl` file lock on macOS and Linux. **Windows has no cross-process lock**: two processes writing at once there can lose an update. On macOS and Linux a lock held longer than 5 seconds raises `TimeoutError` instead of hanging.

## Develop

```
python -m pytest -rs tests
hermes plugins validate .
```

The Desktop UI source is `desktop/src/plugin.src.js`. Run `python desktop/build.py` to regenerate `desktop/plugin.js` with the fonts inlined. The design tokens and visual canon are in `design/`.

## Credits

The ASCII mascot is converted from Hermes Agent's Nous girl art (MIT, © Nous Research) and
the panel's blink, wink and talk geometry comes from the Hermes Gadget SDK (MIT). The fonts
are SIL OFL 1.1; the Share Tech Mono subset is renamed UB Tech Mono because the original
reserves the name "Share". See [NOTICE](NOTICE). UNBL0CK is an independent project by
KyaniteLabs, not endorsed by Nous Research.

## Failure recovery

The [operator guide](docs/operator-guide.md#recover-from-a-fault) owns the recovery
procedures and retention limits. Source failures are visible; settings uncertainty
suspends chat collection, model features and Kanban answers until repaired. Healthy
sources remain usable. See [verification](docs/verification.md) for tested paths and
the live checks still required before acceptance.

## License

MIT. See [LICENSE](LICENSE).
