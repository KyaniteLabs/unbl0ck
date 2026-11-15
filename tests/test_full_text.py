"""Full text for chats and live prompts: bounded polling, on-demand, version-bound retrieval."""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except Exception:  # noqa: BLE001
    FastAPI = None

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("unbl0ck_full_text_needs", ROOT / "needs.py")
needs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(needs)

FIRST = "Opening sentence about the deploy window."
LAST = "Should I merge it now?"
LONG = FIRST + "\n\n" + ("Filler line é ✓ 日本語. " * 60).strip() + "\n" + LAST
RID = "0123456789abcdef"


def chat_db(path: Path, content=LONG, sid="s1"):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE sessions(id TEXT, title TEXT, source TEXT, chat_type TEXT, started_at REAL,"
        " last_activity_at REAL, user_id TEXT, display_name TEXT)"
    )
    db.execute(
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,"
        " timestamp REAL, display_kind TEXT, active INT)"
    )
    now = time.time()
    db.execute(
        "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)", (sid, "", "cli", "dm", now - 60, now - 30, "u", "n")
    )
    db.execute(
        "INSERT INTO messages(session_id, role, content, timestamp) VALUES(?, 'assistant', ?, ?)",
        (sid, content, now - 30),
    )
    db.commit()
    db.close()


class SourceFullText(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        p = patch.object(needs, "_settings", lambda: {"principal_name": "Ada"})
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(needs, "hermes_home", lambda: self.home)
        p.start()
        self.addCleanup(p.stop)
        for name in ("kanban_items", "pairing_items", "outside_items"):
            p = patch.object(needs, name, lambda *a, **kw: [])
            p.start()
            self.addCleanup(p.stop)

    def chat(self, content=LONG):
        chat_db(self.home / "state.db", content)
        (item,) = needs.chat_items(self.home)
        return item

    def wait(self, kind="clarify", prompt=LONG):
        needs.record_wait(self.home, request_id=RID, kind=kind, prompt=prompt, platform="cli")
        (item,) = needs.wait_items(self.home)
        return item

    def test_chat_poll_stays_bounded_and_flags_the_excerpt(self):
        item = self.chat()
        self.assertLessEqual(len(item["ask"]), 480)
        self.assertTrue(item["ask_truncated"])
        self.assertNotIn("text", item)
        self.assertNotIn(FIRST, item["ask"])  # the poll still carries only the tail

    def test_short_chat_is_not_flagged(self):
        self.assertFalse(self.chat("Want me to merge it?")["ask_truncated"])

    def test_chat_full_text_keeps_first_last_unicode_and_newlines(self):
        item = self.chat()
        got = needs.source_text(item["id"], item["version"])
        self.assertEqual(got["text"], LONG)
        self.assertTrue(got["text"].startswith(FIRST))
        self.assertTrue(got["text"].endswith(LAST))
        self.assertIn("\n\n", got["text"])
        self.assertFalse(got["truncated"])

    def test_chat_full_text_is_bounded(self):
        item = self.chat(FIRST + " " + "x" * 50000 + "\n" + LAST)
        got = needs.source_text(item["id"], item["version"])
        self.assertLessEqual(len(got["text"]), needs.FULL_TEXT_LIMIT)
        self.assertTrue(got["truncated"])
        self.assertTrue(got["text"].startswith(FIRST))

    def test_stale_version_is_refused(self):
        item = self.chat()
        db = sqlite3.connect(self.home / "state.db")
        db.execute(
            "INSERT INTO messages(session_id, role, content, timestamp) VALUES('s1','assistant',?,?)",
            ("A newer question for you? " * 30, time.time()),
        )
        db.commit()
        db.close()
        with self.assertRaises(needs.StaleAnswer):
            needs.source_text(item["id"], item["version"])

    def test_missing_record_is_not_found(self):
        item = self.chat()
        (self.home / "state.db").unlink()
        with self.assertRaises(LookupError):
            needs.source_text(item["id"], item["version"])

    def test_source_failure_is_unavailable_not_empty(self):
        item = self.chat()
        with patch.object(needs, "_settings_state", lambda: ({}, "settings unreadable")):
            with self.assertRaises(needs.SourceUnavailable):
                needs.source_text(item["id"], item["version"])

    def test_unrelated_ids_are_rejected(self):
        for bad in ("kanban:b:t", "outside:filed:" + "a" * 32, "pairing:x", "chat", ""):
            with self.assertRaises(ValueError):
                needs.source_text(bad, "1")

    def test_prompt_full_text_round_trips(self):
        item = self.wait()
        self.assertTrue(item["ask_truncated"])
        got = needs.source_text(item["id"], item["version"])
        self.assertEqual(got["text"], LONG)

    def test_prompt_between_4000_and_20000_characters_is_kept_whole(self):
        prompt = "START " + ("middle text. " * 1000) + "Which option do you want, A or B?"
        self.assertGreater(len(prompt), 4001)
        item = self.wait(prompt=prompt)
        got = needs.source_text(item["id"], item["version"])
        self.assertEqual(got["text"], prompt)
        self.assertFalse(got["truncated"])

    def test_prompt_over_the_limit_reports_truncation(self):
        item = self.wait(prompt="x" * (needs.FULL_TEXT_LIMIT + 500))
        got = needs.source_text(item["id"], item["version"])
        self.assertEqual(len(got["text"]), needs.FULL_TEXT_LIMIT)
        self.assertTrue(got["truncated"])

    def test_password_prompt_text_stays_bounded_on_disk(self):
        item = self.wait(kind="sudo", prompt="[sudo] " + "p" * 9000)
        rec = needs._read_json(needs._waits_dir(self.home) / f"{RID}.json")
        self.assertLessEqual(len(rec["prompt"]), 4000)
        self.assertFalse(item["ask_truncated"])

    def test_password_prompt_is_never_expanded(self):
        item = self.wait(kind="sudo", prompt="[sudo] password for ada: " + "p" * 900)
        self.assertFalse(item["ask_truncated"])
        with self.assertRaises(PermissionError):
            needs.source_text(item["id"], item["version"])

    def test_resolved_prompt_is_not_found(self):
        item = self.wait()
        needs.clear_wait(self.home, request_id=RID)
        with self.assertRaises(LookupError):
            needs.source_text(item["id"], item["version"])

    def test_queue_never_carries_full_text(self):
        self.chat()
        self.wait()
        dumped = json.dumps(needs.build_queue(self.home))
        self.assertNotIn(FIRST, dumped)


@unittest.skipIf(FastAPI is None, "fastapi not installed")
class SourceTextRoute(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("ub_api_full_text", ROOT / "dashboard/plugin_api.py")
        self.api = importlib.util.module_from_spec(spec)
        sys.modules["ub_api_full_text"] = self.api
        spec.loader.exec_module(self.api)
        self.calls = []

        def source_text(i, v):
            self.calls.append((i, v))
            return self.result(i, v)

        self.result = lambda i, v: {"id": i, "version": v, "text": "full", "truncated": False}
        self.api._needs = lambda: type(
            "N",
            (),
            {
                "source_text": staticmethod(source_text),
                "StaleAnswer": needs.StaleAnswer,
                "SourceUnavailable": needs.SourceUnavailable,
            },
        )
        app = FastAPI()
        app.include_router(self.api.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def get(self, i="chat:p:s", v="1"):
        return self.client.get("/source/text", params={"id": i, "version": v})

    def test_success(self):
        r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["text"], "full")

    def test_error_mapping(self):
        for exc, code in (
            (needs.StaleAnswer("old"), 409),
            (LookupError("gone"), 404),
            (needs.SourceUnavailable("down"), 503),
            (PermissionError("password"), 403),
            (ValueError("bad"), 400),
        ):
            def boom(i, v, exc=exc):
                raise exc

            self.result = boom
            self.assertEqual(self.get().status_code, code, exc)

    def test_identifier_bounds(self):
        self.assertEqual(self.get("chat:" + "x" * 301).status_code, 400)
        self.assertEqual(self.get(v="v" * 301).status_code, 400)
        self.assertEqual(self.calls, [])


class DesktopFullText(unittest.TestCase):
    SCRIPT = r"""
const fs = require('fs'), assert = require('node:assert/strict');
const src = fs.readFileSync(process.argv[1], 'utf8');
const views = src.slice(src.indexOf('const ANSWER_DRAFT_TTL'), src.indexOf('function Chip('));
const noop = () => {};
const node = (type, props) => ({type, props});
let state = [], cursor = 0, effectCursor = 0, effects = [], hooks = [];
const env = {
  jsx: node, jsxs: node,
  useState: init => { const i = cursor++; if (!(i in state)) state[i] = init; return [state[i], v => { state[i] = typeof v === 'function' ? v(state[i]) : v; }]; },
  // One slot per useEffect call site (by call order), like React: an effect is due when its
  // dependencies differ from the ones it last ran with, and its cleanup runs first.
  useEffect: (fn, d) => { const i = effectCursor++; const prev = hooks[i]; if (!prev || !d || d.some((x, k) => x !== prev.deps[k])) { effects.push([i, fn, d]); } },
  useRef: () => ({}), platformLabel: x => x, LOCAL_PLATFORMS: [], KEEP_BLOCKED: /keep blocked/i,
  button: props => node('button', props), openBoard: noop, ago: () => '', ageClass: () => '', pad: String,
  sfx: {play: noop}, petEmit: noop, host: {notify: noop, notifyError: e => {throw e}},
  $undo: {set: noop}, $queue: 'queue', $fxOn: 'fx', UNDO_MS: 12000, askCount: q => q.count,
  Field: noop, Watermark: noop, Banner: noop, Pet: noop, Ticker: noop, FxToggle: noop, SfxToggle: noop,
  grainUrl: () => '', oldestAskSince: () => 0,
};
const {Item} = new Function('env', `with(env) { ${views}; return {Item}; }`)(env);
function all(n) { if (n == null) return []; if (Array.isArray(n)) return n.flatMap(all); if (typeof n !== 'object') return [n]; return [n, ...all(n.props?.children)]; }
function text(n) { return all(n).filter(x => typeof x === 'string').join(''); }
const FULL = 'FIRST sentence.\n\nmiddle é ✓\nLAST?';
const requests = [];
let mode = 'ok';
const held = [];
const ctx = {rest: async url => {
  requests.push(url);
  if (mode === 'held') return new Promise((resolve, reject) => held.push({url, resolve, reject}));
  if (mode === 'fail') throw new Error('offline');
  return {text: FULL, truncated: mode === 'cut'};
}};
function render(item) { cursor = 0; effectCursor = 0; effects = []; return Item({ctx, item, n: 1, refresh: async () => {}, askEnabled: false}); }
async function flush() {
  const run = effects.splice(0);
  for (const [i, fn, d] of run) { hooks[i]?.cleanup?.(); const cleanup = fn(); hooks[i] = {deps: d, cleanup}; }
  await new Promise(r => setImmediate(r));
}
const settle = () => new Promise(r => setImmediate(r));
function openFull(tree) { all(tree).find(x => x?.type === 'button' && x.props.className === 'ub-full').props.onClick(); }
(async () => {
  for (const [source, id] of [['chat', 'chat:p:s1'], ['prompt', 'prompt:0123456789abcdef']]) {
    state = []; hooks = []; requests.length = 0; mode = 'ok';
    const item = {id, version: '9', source, title: 't', ask: '…tail of it', ask_truncated: true, ask_cut: true, since: 1, profile: 'p', platform: 'cli'};
    let tree = render(item);
    assert.ok(text(tree).includes('last 480 characters'), 'excerpt must be labelled while closed');
    assert.equal(requests.length, 0, 'polling view must not fetch');
    openFull(tree); tree = render(item); await flush(); tree = render(item);
    assert.deepEqual(requests, [`/source/text?id=${encodeURIComponent(id)}&version=9`]);
    assert.ok(text(tree).includes('FIRST sentence.'), 'full text shown');
    assert.ok(!text(tree).includes('last 480 characters'), 'label gone once the full text is shown');
    // a new version drops the old full text
    const next = {...item, version: '10'};
    tree = render(next);
    assert.ok(!text(tree).includes('FIRST sentence.'), 'old version text must not be shown for a new version');
  }
  // failure and retry
  state = []; hooks = []; requests.length = 0; mode = 'fail';
  const item = {id: 'chat:p:s1', version: '9', source: 'chat', title: 't', ask: '…tail', ask_truncated: true, ask_cut: true, since: 1};
  let tree = render(item); openFull(tree); tree = render(item); await flush(); tree = render(item);
  assert.ok(text(tree).includes('Full text could not load: offline'));
  assert.ok(text(tree).includes('last 480 characters'), 'still labelled as an excerpt after failure');
  const retry = all(tree).find(x => x?.type === 'button' && text(x) === 'Retry full text');
  assert.ok(retry, 'retry control');
  mode = 'ok'; retry.props.onClick(); tree = render(item); await flush(); tree = render(item);
  assert.ok(text(tree).includes('FIRST sentence.'));
  // a message over the limit keeps a visible warning after the load succeeds
  state = []; hooks = []; requests.length = 0; mode = 'cut';
  const long = {id: 'chat:p:s3', version: '1', source: 'chat', title: 't', ask: '…tail', ask_truncated: true, ask_cut: true, since: 1};
  tree = render(long); openFull(tree); tree = render(long); await flush(); tree = render(long);
  assert.ok(text(tree).includes('FIRST sentence.'));
  assert.ok(text(tree).includes('longer than 20,000 characters'), 'limit warning must show');
  assert.ok(text(tree).includes('original chat'));
  state = []; hooks = []; mode = 'ok';
  tree = render(long); openFull(tree); tree = render(long); await flush(); tree = render(long);
  assert.ok(!text(tree).includes('longer than 20,000 characters'), 'no warning when complete');
  // 01-097: an older response arriving after a newer request must not overwrite it
  state = []; hooks = []; requests.length = 0; held.length = 0; mode = 'held';
  const v9 = {id: 'chat:p:s9', version: '9', source: 'chat', title: 't', ask: '…tail', ask_truncated: true, ask_cut: true, since: 1};
  const v10 = {...v9, version: '10'};
  tree = render(v9); openFull(tree); tree = render(v9); await flush();
  tree = render(v10); await flush();
  assert.equal(held.length, 2, 'one request per version');
  held[1].resolve({text: 'NEWER TEXT', truncated: false}); await settle(); tree = render(v10);
  assert.ok(text(tree).includes('NEWER TEXT'), 'newest response shown');
  held[0].resolve({text: 'OLDER TEXT', truncated: false}); await settle(); tree = render(v10);
  assert.ok(text(tree).includes('NEWER TEXT') && !text(tree).includes('OLDER TEXT'), 'a late older response must be ignored');
  // a late older failure must not show an error over the newer text
  state = []; hooks = []; held.length = 0;
  tree = render(v9); openFull(tree); tree = render(v9); await flush();
  tree = render(v10); await flush();
  held[1].resolve({text: 'NEWER TEXT', truncated: false}); await settle();
  held[0].reject(new Error('stale failure')); await settle(); tree = render(v10);
  assert.ok(!text(tree).includes('stale failure'), 'a late older failure must be ignored');
  assert.ok(text(tree).includes('NEWER TEXT'));
  // an untruncated chat never fetches
  mode = 'ok';
  state = []; hooks = []; requests.length = 0;
  const short = {id: 'chat:p:s2', version: '1', source: 'chat', title: 't', ask: 'short?', since: 1};
  tree = render(short); openFull(tree); tree = render(short); await flush();
  assert.equal(requests.length, 0);
})().catch(e => { console.error(e); process.exitCode = 1 });
"""

    def test_source_and_generated_views(self):
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                result = subprocess.run(
                    ["node", "-e", self.SCRIPT, str(path)], capture_output=True, text=True, check=False
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
