# Changelog

- Recover Ask results after interrupted polling without repeating model work; bound completed-result retention.

- Keep settings read failures from widening chat visibility, model disclosure or answer identity; resume configured behavior after repair.

## Unreleased

- **Recovery survives a temporary outage.** Failed Undo keeps its retry control and cannot submit twice while pending. Failed answers refresh the queue immediately so a posted answer with a failed release shows its recovery action. Long TL;DR conversations keep their displayed history while respecting the API's 50-message transport limit.
- **Failures stay visible and recoverable.** Corrupt source records, schema/config failures and folder limits mark the queue incomplete. Unreadable prompt records survive inspection, stale cleanup cannot hide healthy prompts, and visibility writes refuse corrupt state. Summary fallback status is explicit; model work has bounded admission, rejected submissions cannot strand jobs, and Ask expiry works without a new request.
- **Find cards in a large queue.** Search titles, previews, apps and references, or filter by source. Typed answers survive filtering, and the status count still covers the whole queue.
- **Read and recover in place.** Every card has a keyboard-accessible full-text toggle. Full-text and Context failures have retry controls; folder Done can be undone for 12 seconds. Long subjects and unbroken text wrap on small screens, and text composition no longer submits an unfinished answer or Ask.
- **Exports show the right content.** Plain-text attachments and attached messages never stand in for an HTML-only draft body. Oversized exports disclose the 256 KiB preview limit.
- **Faults stay honest.** The CLI and status chip no longer imply an empty queue is complete when a source fails. Combined `--json --pack` keeps stdout valid JSON. Disabling the Desktop plugin ignores late responses and cannot restart its polling.
- **One click gets the bottom line.** "Ask about this card" is now **TL;DR**: clicking it asks your model, straight away, what the card wants from you and what to pick, in plain words and under 80 words. Follow-up questions still work in the same panel.
- **Ask no longer comes back empty on reasoning models.** With a 900-token cap, MiniMax M3.1 Flash spent the whole budget thinking and returned no text, which showed as "The model returned an empty answer". The cap is now 4000, inline `<think>` blocks are dropped, and a reply with no answer text says so and suggests a retry.
- **Real asks stop vanishing from the queue.** A negation now counts only in the same clause and only in the three words right before the ask, so "Cannot proceed: needs CEO approval to merge" and "Needs your approval. Not urgent." are listed. After the ask, only "is not needed/required" cancels it. New phrasings are recognised: "approval from Ada", "can't deploy without your approval", "Please approve…", "need the human to…". A relayed order ("CEO says: …") still counts as a stop unless it asks for *your* approval or decision. On the live board, 16 of 209 blocked cards are now listed, up from 10.
- **Fewer false chat asks.** Question marks inside URLs, sign-offs like "Anything else you need?", questions the turn answers itself ("Why did it fail? Because…"), "which option was faster" and an archived decision packet no longer count as asks.
- **"Answer, keep blocked" cards stay in the queue.** Before, answering a card without unblocking it made it vanish, so it stayed blocked with no way back from UNBL0CK. Now it stays listed as `answered` (counted apart from new asks, no model summary) with a **Release (unbl0ck)** button that unblocks it without posting a second comment.
- **The shortcut shows what it will do.** When your answer says to keep the card blocked, ⌘/Ctrl + Enter keeps it blocked; the ⏎ mark now moves to **Answer, keep blocked** and the hint says so. The Release button no longer shows a shortcut it does not have.
- **Docs say what actually happens.** Failed summaries are retried after 10 minutes and the page polls every 45 seconds; listing pairing requests prunes expired ones, as `hermes pairing list` does; the CLI points to the `/unblock` page.

## 0.5.0 (2026-11-11)

- **Things waiting outside Hermes (#10).** The new **OUTSIDE.HERMES** queue group collects mail drafts, call notes and other items through the `unbl0ck_file` agent tool or read-only watched `.eml`/`.md`/`.txt` folders (`outside_dirs`, off by default). Agent claims are labelled, filing supports deduplication and resolve, and BLUF summaries include outside text when enabled. Done deletes only UNBL0CK's filed records; folder Done hides until the file changes. No external app APIs, credentials or automatic external actions.

## 0.4.6 (2026-11-11)

Fixes from the hackathon roast (two independent audits).

- **Your answer goes to the question you read.** The desktop sends the queue item's version with each answer. If the card was blocked again after you opened it, the answer is refused (HTTP 409) and nothing is posted.
- **A failed unblock can be retried.** When the answer posts but the card won't unblock, the card stays in the queue marked `release_failed`. Answering it again retries the release without posting the answer twice. The marker lives in `plugin-data/unbl0ck/pending-release.json`.
- **Every blocked card is listed** (from PR #1): cards with no recorded ask show as `stuck`, with the reason. Answered cards leave the queue in both tiers. Stuck cards don't get a model summary, so a long stuck list doesn't trigger a model call per card.
- **State files are safe across processes.** `hidden.json`, `bluf.json` and `pending-release.json` are written under a file lock, so the dashboard and the agent plugin no longer drop each other's writes. A failed write no longer leaves a temp file behind.
- **The owner filter fails closed.** With `owner_user_ids` set, a gateway chat whose user is unknown is no longer shown or summarised.
- **`GET /pack` is read-only.** `POST /pack` writes `pocket-pack.json`.
- **No personal names in shipped files.** Tests and the design canon use generic names.
- **CI runs the tests in the pinned Hermes runtime** with a throwaway `HERMES_HOME`, and fails if any test is skipped.
