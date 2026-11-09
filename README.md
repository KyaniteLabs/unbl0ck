# UNBL0CK

Agents get stuck waiting on you, and those questions end up scattered across Kanban boards and chats. UNBL0CK gathers them into one queue: every blocked Kanban card waiting on your answer, and every Bot Mode chat whose last turn is the bot asking you something.

To answer a card, type into the queue. UNBL0CK posts your answer as a comment through native Kanban and, if you choose, unblocks the card. A chat opens straight from the queue. Nothing is decided for you, nothing is ranked or rewritten, and silence never counts as a yes.

## What you get

- **Desktop:** an `/unblock` page showing the live queue, plus a "Needs you" count. Each Kanban card has **Answer + unblock**, **Answer, keep blocked**, **Open board** and **Not for me**, plus a lazy **Context** panel with the description, comments, parent/child cards, workspace and branch, recent events, last run outcome and attachments. **Ask about this card** opens a small read-only side chat that explains the card and recommends an option; **Use as answer** copies its last reply into the answer box, and **Discuss in chat** opens a normal Hermes chat with the card pre-seeded. The model never acts: you still click Answer. Hidden items come back when the source changes: a new block, or a new chat turn.
- **Slash command:** `/unbl0ck` lists what is waiting on you, inside any Hermes chat.
- **CLI:** `hermes unbl0ck` prints the queue. Add `--json` for the raw queue, or `--pack` to write a small card-format JSON pack for other displays to read.

## Install

```
hermes plugins install unbl0ck
```

Then set your name, so UNBL0CK knows which asks are aimed at you. Use the plugin's settings in Desktop (**Settings → Plugins → UNBL0CK**), or:

```
hermes config set plugins.entries.unbl0ck.settings.principal_name "Ada, Ada Lovelace"
```

`principal_name` is comma-separated: list the names your agents use for you. "CEO", "principal" and "human" always count. Your answers are posted under the first name, unless you set `comment_author`. If no name is set, only those generic words are detected.

**Ask about this card** is on by default. Turn it off with `ask_enabled: false` in the same settings block (or `hermes config set plugins.entries.unbl0ck.settings.ask_enabled false`). The Context panel and **Discuss in chat** keep working when Ask is off.

Requires Hermes `>= 0.23.0`.

## How detection works

- **Kanban:** a card counts when its latest block is clearly waiting on you. Either the block reason names a decision for you ("Awaiting Ada's approval", "human gate: credentials"), or it is a `needs_input` block that names you. Blocks that only report an order ("Ada said stop") are skipped. So are cards you have already answered since the block.
- **Bot Mode:** a canonical Bot Chat from the last 14 days counts when the bot's final visible turn asks you something and you have not replied after it.

## Disclosure

The things you would want to know before installing:

- **No background network calls.** UNBL0CK sends no telemetry or usage data. The Desktop page talks only to this plugin's own backend, through the plugin SDK.
- **Ask sends card content to your model provider, only when you ask.** Pressing **Ask about this card** sends that card's context and your question to the model provider configured in Hermes, through Hermes's own plugin LLM API, and logs the call in Hermes's plugin audit log. Nothing is sent until you press Ask. **Discuss in chat** only types a draft into a normal Hermes chat; the chat you then start makes its own model calls. Turn Ask off with `ask_enabled: false`.
- **No credentials.** It neither reads nor stores secrets.
- **Reads outside its own data:**
  - Kanban boards, through Hermes's Kanban modules (`hermes_cli.kanban_db`).
  - The `state.db` chat history of the root Hermes home and of every profile under it, opened **read-only**, so it can find Bot Chats that end with a question for you.
- **Writes:**
  - Only when you click Answer: one comment on the card, under your configured author name, and an unblock if you chose **Answer + unblock**. Nothing else is ever written to Kanban. The Context panel and Ask never write to the card.
  - Its own files under `<HERMES_HOME>/plugin-data/unbl0ck/`: `hidden.json` ("Not for me" items) and `pocket-pack.json` (only when you ask for the pack).
  - In Desktop, two display toggles (effects and sound) in `localStorage`.
- **No shell commands, no subprocesses, no background processes, and no self-updating.**
- **Sound:** the Desktop page plays short synthesized UI sounds through Web Audio. Turn them off with **SFX**.
- **Hermes internals:** Kanban access uses Hermes's internal `hermes_cli.kanban_db*` modules, because no public plugin API for Kanban comments and unblocks exists yet. A Hermes release that changes those modules can break the queue until UNBL0CK is updated. `hermes plugins validate` reports this as a warning.

## Develop

```
python -m unittest discover -s tests -v
hermes plugins validate .
```

The Desktop UI source is `desktop/src/plugin.src.js`. Run `python desktop/build.py` to regenerate `desktop/plugin.js` with the fonts inlined. The design tokens and visual canon are in `design/`.

## Credits

The ASCII mascot is converted from Hermes Agent's Nous girl art (MIT, © Nous Research), and the fonts are under the SIL Open Font License. See [NOTICE](NOTICE). UNBL0CK is an independent project by KyaniteLabs and is not endorsed by Nous Research.

## License

MIT. See [LICENSE](LICENSE).
