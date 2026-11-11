# Changelog

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
