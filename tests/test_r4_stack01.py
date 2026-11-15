"""Round-4 review findings owned by the first stack branch (full text), group by group."""

import os
import re
import subprocess
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_outside import OutsideFixture, needs, plugin

ROOT = Path(__file__).resolve().parents[1]


def kanban_item(task, tier="ask", priority=0, version="1", **extra):
    return {
        "id": f"kanban:b:{task}", "version": version, "source": "kanban", "board": "b",
        "task_id": task, "title": f"Card {task}", "ask": f"Question {task}", "why": "why",
        "tier": tier, "owner": "o", "priority": priority, "status": "blocked", "since": 100,
        **extra,
    }


class QueueOrderAndSummaries(OutsideFixture):
    def queue_with(self, *items):
        return patch.object(needs, "kanban_items", lambda **kw: [dict(i) for i in items])

    def test_ask_sorts_before_a_higher_priority_stuck_card(self):
        """01-001: the order is tier first, then priority; the README must say so."""
        with self.queue_with(kanban_item("stuck", tier="stuck", priority=100), kanban_item("ask", priority=0)):
            ids = [i["task_id"] for i in needs.build_queue()["items"]]
        self.assertEqual(ids, ["ask", "stuck"])
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("cards keep their board priority, newest first within it", readme)
        self.assertRegex(readme, r"asks (come|sort|are listed) before stuck")

    def test_a_cached_summary_is_not_attached_once_summaries_are_disabled(self):
        """01-002: disabling summaries must stop showing a cached one, as the README says."""
        needs.save_bluf("kanban:b:t1", "1", "Pick a lane.", "Because.", self.home)
        with self.queue_with(kanban_item("t1")):
            self.settings["bluf_enabled"] = True
            (on,) = needs.build_queue()["items"]
            self.assertEqual(on.get("bluf"), "Pick a lane.")
            self.settings["bluf_enabled"] = False
            q = needs.build_queue()
            (off,) = q["items"]
        self.assertFalse(q["bluf_enabled"])
        self.assertNotIn("bluf", off)
        self.assertNotIn("bluf_why", off)
        # Turning them on again brings the cached summary back: nothing was deleted.
        with self.queue_with(kanban_item("t1")):
            self.settings["bluf_enabled"] = True
            (again,) = needs.build_queue()["items"]
        self.assertEqual(again.get("bluf"), "Pick a lane.")


class TerminalOutput(unittest.TestCase):
    ESC = "\x1b]52;c;aGk=\x07\x1b[2J\x9b31m"

    def queue(self, **item):
        base = {
            "id": "kanban:b:t", "source": "kanban", "board": "b", "task_id": "t", "tier": "ask",
            "title": "Plain", "ask": "plain", "version": "1",
        }
        base.update(item)
        return {"count": 1, "items": [base], "errors": []}

    def render(self, q):
        with patch.object(plugin, "_needs", return_value=types.SimpleNamespace(build_queue=lambda: q, _clean=needs._clean)):
            return plugin._summary()

    def assertClean(self, out):
        bad = [c for c in out if (ord(c) < 32 and c != "\n") or 0x7F <= ord(c) <= 0x9F]
        self.assertEqual(bad, [], repr(out))

    def test_titles_and_summaries_cannot_inject_terminal_escapes(self):
        """01-003 (class K, display sanitization): title, summary and approve text are cleaned."""
        out = self.render(self.queue(title="T" + self.ESC, bluf="B" + self.ESC, approve_command="run" + self.ESC))
        self.assertClean(out)
        self.assertIn("T", out)

    def test_every_source_label_is_cleaned(self):
        for item in (
            {"source": "kanban", "board": "b" + self.ESC, "task_id": "t" + self.ESC},
            {"source": "prompt", "kind": "k" + self.ESC, "profile": "p" + self.ESC},
            {"source": "pairing", "platform": "p" + self.ESC},
            {"source": "outside", "kind": "k" + self.ESC, "act_where": "w" + self.ESC},
            {"source": "chat", "platform": "p" + self.ESC, "profile": "x", "chat_with": "c" + self.ESC},
        ):
            with self.subTest(source=item["source"]):
                self.assertClean(self.render(self.queue(**item)))

    def test_source_errors_are_cleaned(self):
        q = self.queue()
        q["errors"] = ["kanban[b" + self.ESC + "]: boom"]
        self.assertClean(self.render(q))


class EmptyQueueWording(unittest.TestCase):
    def render(self, items, func="_summary"):
        q = {"count": 0, "items": items, "errors": []}
        n = types.SimpleNamespace(build_queue=lambda: q, _clean=needs._clean)
        with patch.object(plugin, "_needs", return_value=n):
            return getattr(plugin, func)()

    STUCK = [{"id": "kanban:b:t", "source": "kanban", "tier": "stuck", "title": "Stuck", "ask": "x", "board": "b", "task_id": "t"}]

    def test_stuck_cards_are_never_called_nothing_blocked(self):
        """01-019: no asks but a listed stuck card is not 'Nothing is blocked on you'."""
        for func in ("_summary", "_count_only"):
            with self.subTest(func=func):
                out = self.render(self.STUCK, func)
                self.assertNotIn("Nothing is blocked", out)
                self.assertIn("stuck", out.lower())

    def test_a_truly_empty_queue_still_says_nothing_is_blocked(self):
        self.assertEqual(self.render([]), "Nothing is blocked on you.")
        self.assertEqual(self.render([], "_count_only"), "Nothing is blocked on you.")


def run_views(script):
    out = []
    for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
        result = subprocess.run(["node", "-e", script, str(path)], capture_output=True, text=True)
        out.append((path.name, result.returncode, result.stderr))
    return out


PRELUDE = r"""
const fs = require('fs'), assert = require('node:assert/strict');
const src = fs.readFileSync(process.argv[1], 'utf8');
const views = src.slice(src.indexOf('const ANSWER_DRAFT_TTL'), src.indexOf('function Chip('));
const node = (type, props, key) => ({type, props, key}), noop = () => {};
let state = [], cursor = 0, undoSet = [];
const env = {
  jsx: node, jsxs: node, useState: init => { const i = cursor++; if (!(i in state)) state[i] = init; return [state[i], v => { state[i] = typeof v === 'function' ? v(state[i]) : v; }]; },
  useEffect: noop, useRef: () => ({}), platformLabel: x => x, LOCAL_PLATFORMS: [], KEEP_BLOCKED: /keep blocked/i,
  button: props => node('button', props), openBoard: noop, ago: () => '', ageClass: () => '', pad: String,
  sfx: {play: noop}, petEmit: noop, host: {notify: noop, notifyError: noop},
  $undo: {set: v => { undoSet.push(v) }, get: () => null}, $queue: 'queue', $fxOn: 'fx', UNDO_MS: 12000, askCount: q => q.counts?.asks ?? q.count ?? 0,
  Field: noop, Watermark: noop, Banner: noop, Pet: noop, Ticker: noop, FxToggle: noop, SfxToggle: noop,
  grainUrl: () => '', oldestAskSince: () => 0, reducedMotion: () => true,
};
const {Item, Page} = new Function('env', `with(env) { ${views}; return {Item, Page}; }`)(env);
function all(n) { if (n == null) return []; if (Array.isArray(n)) return n.flatMap(all); if (typeof n !== 'object') return [n]; return [n, ...all(n.props?.children)]; }
function text(n) { return all(n).filter(x => typeof x === 'string').join(''); }
"""


class DesktopAllClear(unittest.TestCase):
    def check(self, body):
        for name, code, err in run_views(PRELUDE + body):
            with self.subTest(file=name):
                self.assertEqual(code, 0, err)

    def test_desktop_never_claims_all_clear_beside_a_listed_stuck_card(self):
        """01-019: one stuck card and zero asks is not ALL CLEAR / 'Nothing is blocked on you'."""
        self.check(r"""
const stuck = {source:'kanban',id:'kanban:b:t',version:'1',title:'Stuck one',ask:'x',tier:'stuck',board:'b',task_id:'t',since:1};
const ctx = {rest: async () => ({})}, refresh = async () => {};
env.useValue = key => key === 'queue' ? q : false;
function render(q0) { globalThis.q = q0; state = []; cursor = 0; return text(Page({ctx, refresh})); }
let page = render({items:[stuck], count:0, counts:{asks:0,stuck:1,answered:0}, errors:[], at:Date.now()});
assert.ok(!page.includes('ALL CLEAR'), 'stuck card beside ALL CLEAR');
assert.ok(!page.includes('Nothing is blocked on you'), 'stuck card beside the nothing-blocked line');
assert.ok(/stuck/i.test(page.replace(/KANBAN\.STUCK/, '')), 'the stuck card must be acknowledged');
page = render({items:[], count:0, counts:{asks:0,stuck:0,answered:0}, errors:[], at:Date.now()});
assert.ok(page.includes('ALL CLEAR') && page.includes('Nothing is blocked on you'), 'a truly empty queue stays all clear');
""")


class DocsMatchBehavior(unittest.TestCase):
    def test_architecture_doc_states_validation_status_codes_and_card_bound(self):
        """01-021: the doc contradicted itself about GET Card's bound and the oversize status."""
        text = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
        self.assertNotIn("GET Card does not apply the same length limit", text)
        self.assertRegex(text, r"GET Card[^.]*300")
        self.assertRegex(text, r"(?s)POST Answer/Ask.{0,400}422")
        self.assertNotRegex(text, r"oversized[^.]*\b400\b")


class CardReferenceBound(unittest.TestCase):
    def test_get_card_rejects_oversized_references_with_400(self):
        """01-021: GET /card applies the same 300-character bound as the POST models."""
        import importlib.util
        import sys

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        spec = importlib.util.spec_from_file_location("ub_api_r4_s01", ROOT / "dashboard/plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        sys.modules["ub_api_r4_s01"] = api
        spec.loader.exec_module(api)
        app = FastAPI()
        app.include_router(api.router)
        client = TestClient(app)
        for params in ({"id": "k" * 301}, {"board": "b" * 301, "task_id": "t"}, {"board": "b", "task_id": "t" * 301}):
            with self.subTest(params=list(params)):
                self.assertEqual(client.get("/card", params=params).status_code, 400)


def load_api(tag):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(f"ub_api_r4_{tag}", ROOT / "dashboard/plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    sys.modules[f"ub_api_r4_{tag}"] = api
    spec.loader.exec_module(api)
    return api


class AskAdmission(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        self.api = load_api("ask")
        self.calls = []
        self.enabled = True
        self.version = "7"

        def card_context(b, t):
            self.calls.append("card_context")
            return {"id": t, "version": self.version}

        def build(card, history):
            self.calls.append("build")
            return list(history)

        self.needs = types.SimpleNamespace(
            ask_enabled=lambda: self.enabled, card_context=card_context, build_ask_messages=build,
            bluf_enabled=lambda: self.enabled,
        )
        self.api._needs = lambda: self.needs
        self.api._checked_card_ref = lambda i, b, t: ("factory", "t_1")
        self.llm_calls = []

        def complete(messages, **kw):
            self.llm_calls.append(messages)
            return types.SimpleNamespace(text="Pick A", provider="p", model="m")

        self.api._loaded_ctx = lambda: types.SimpleNamespace(llm=types.SimpleNamespace(complete=complete))
        app = FastAPI()
        app.include_router(self.api.router)
        self.c = TestClient(app)

    def post(self, **over):
        body = {"id": "x", "messages": [{"role": "user", "content": "which?"}]}
        body.update(over)
        return self.c.post("/card/ask", json=body)

    def test_malformed_messages_are_rejected_not_dropped(self):
        """01-004: unknown roles, extra keys, non-text content and a trailing assistant turn fail."""
        bad = [
            [{"role": "system", "content": "x"}],
            [{"role": "user", "content": "x", "extra": 1}],
            [{"role": "user", "content": 5}],
            [{"role": "user", "content": {"a": 1}}],
            [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
            [{"role": "user", "content": "   "}],
            [{"role": "tool", "content": "q"}, {"role": "user", "content": "q"}],
            [],
        ]
        for msgs in bad:
            with self.subTest(msgs=msgs):
                self.assertIn(self.post(messages=msgs).status_code, (400, 422))
        self.assertEqual(self.calls, [])
        ok = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
        self.assertEqual(self.post(messages=ok).status_code, 200)

    def test_busy_server_does_no_card_work_before_refusing(self):
        """01-008: a 429 is decided before the card is read or the prompt built."""
        held = []
        while self.api._ASK_SLOTS.acquire(blocking=False):
            held.append(1)
        try:
            self.assertEqual(self.post().status_code, 429)
        finally:
            for _ in held:
                self.api._ASK_SLOTS.release()
        self.assertEqual(self.calls, [])

    def test_a_failed_card_read_releases_the_slot(self):
        self.needs.card_context = lambda b, t: (_ for _ in ()).throw(LookupError("gone"))
        for _ in range(6):
            self.assertEqual(self.post().status_code, 404)
        self.assertEqual(self.post(version=None).status_code, 404)

    def test_an_ask_for_an_old_question_version_is_refused(self):
        """01-016: the card moved on after the human read it."""
        self.assertEqual(self.post(version="6").status_code, 409)
        self.assertEqual(self.llm_calls, [])
        self.assertEqual(self.post(version="7").status_code, 200)

    def test_a_repeated_request_id_returns_the_same_job(self):
        """01-017: a lost response can be retried without a second model call."""
        import time

        rid = "ab" * 16
        first = self.post(request_id=rid).json()
        again = self.post(request_id=rid).json()
        self.assertEqual(first["job"], rid)
        self.assertEqual(again["job"], rid)
        for _ in range(40):
            if self.llm_calls:
                break
            time.sleep(0.05)
        time.sleep(0.2)
        self.assertEqual(len(self.llm_calls), 1)
        self.assertEqual(self.post(request_id="not hex!").status_code, 422)

    def test_a_model_call_is_skipped_when_ask_is_turned_off_meanwhile(self):
        """01-005: the setting is re-read at the model boundary."""
        import time

        self.enabled = True
        job = self.api.uuid.uuid4().hex
        self.api._ASK_JOBS.create(job)
        self.enabled = False
        self.api._run_ask(job, self.api._loaded_ctx(), [{"role": "user", "content": "q"}])
        self.assertEqual(self.llm_calls, [])
        self.assertEqual(self.api._ASK_JOBS.get(job)["status"], self.api.jobs.AskState.ERROR)

    def test_a_summary_is_not_requested_or_saved_when_summaries_are_turned_off(self):
        saved = []
        self.needs.recent_comments = lambda it: []
        self.needs.bluf_messages = lambda it, c: [{"role": "user", "content": "x"}]
        self.needs.parse_bluf = lambda t: ("b", "w")
        self.needs.save_bluf = lambda *a: saved.append(a)
        self.enabled = False
        item = {"id": "kanban:b:t", "version": "1"}
        self.api._BLUF_JOBS.start(("kanban:b:t", "1"))
        self.api._run_bluf(self.needs, self.api._loaded_ctx(), item)
        self.assertEqual(self.llm_calls, [])
        self.assertEqual(saved, [])


class AnswerAndPackRoutes(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        self.api = load_api("routes")
        real = needs
        self.answered = []
        self.writes = []
        self.api._needs = lambda: types.SimpleNamespace(
            normalize_board_slug=real.normalize_board_slug,
            answer_card=lambda *a, **k: self.answered.append(a) or {"ok": True},
            build_queue=lambda: {"items": [], "count": 0, "errors": [], "bluf_enabled": False, "at": __import__("time").time()},
            pocket_pack=real.pocket_pack,
            write_pocket_pack=lambda q, home=None, pack=None: self.writes.append(pack) or Path("p.json"),
        )
        app = FastAPI()
        app.include_router(self.api.router)
        self.c = TestClient(app)

    def test_an_answer_needs_a_board_and_a_card(self):
        """01-010: an empty board must not fall through to the ambient board, nor an empty card id."""
        for body in ({"board": "", "task_id": "t", "text": "yes"}, {"board": "b", "task_id": "", "text": "yes"},
                     {"board": "  ", "task_id": "t", "text": "yes"}):
            with self.subTest(body=body):
                self.assertEqual(self.c.post("/kanban/answer", json=body).status_code, 400)
        self.assertEqual(self.answered, [])

    def test_the_card_context_and_answer_reject_empty_targets(self):
        for fn in (real_needs().answer_card,):
            with self.assertRaises(ValueError):
                fn("", "t", "x", release=False)
            with self.assertRaises(ValueError):
                fn("b", "", "x", release=False)
        with self.assertRaises(ValueError):
            real_needs().card_context("b", "")

    def test_post_pack_writes_the_pack_it_returns(self):
        """01-011: one pack object is built, written and returned."""
        out = self.c.post("/pack").json()
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.writes[0], out)


def real_needs():
    return needs


class OutsideLookups(OutsideFixture):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        self.api = load_api("outside")
        self.api._needs = lambda: needs
        app = FastAPI()
        app.include_router(self.api.router)
        self.client = TestClient(app)

    def folder_item(self, name="a.txt", text="body"):
        self.watch()
        (self.folder / name).write_text(text)
        return next(i for i in needs.outside_items() if i["path"] == name)

    def test_full_text_of_a_file_below_the_poll_window_is_still_served(self):
        """01-006: a file that is not among the newest 200 can still be opened and marked Done."""
        import os

        self.watch()
        old = self.folder / "old.txt"
        old.write_text("the oldest note")
        os.utime(old, (1_000, 1_000))
        for i in range(210):
            (self.folder / f"n{i:03d}.txt").write_text(f"note {i}")
        ids = {i["path"] for i in needs.outside_items()}
        self.assertNotIn("old.txt", ids)
        item_id = "outside:folder:" + __import__("hashlib").sha256(str(old.resolve()).encode()).hexdigest()
        version = needs.outside_lookup(item_id, self.home)["version"]
        got = self.client.get("/outside/text", params={"id": item_id, "version": version})
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json()["text"], "the oldest note")

    def test_a_missing_item_is_404_but_an_unreadable_one_is_503(self):
        """01-006/01-007: absence and failure to read are different answers."""
        item = self.folder_item()
        self.assertEqual(self.client.get("/outside/text", params={"id": item["id"], "version": item["version"]}).status_code, 200)
        gone = "outside:folder:" + "0" * 64
        self.assertEqual(self.client.get("/outside/text", params={"id": gone, "version": "1"}).status_code, 404)
        with patch.object(os, "scandir", side_effect=PermissionError("denied")):
            r = self.client.get("/outside/text", params={"id": item["id"], "version": item["version"]})
        self.assertEqual(r.status_code, 503, r.text)

    def test_a_corrupt_filed_record_is_503_on_text_and_done(self):
        result = self.file()
        self.record(result).write_text("{not json")
        text = self.client.get("/outside/text", params={"id": result["id"], "version": "x"})
        self.assertEqual(text.status_code, 503, text.text)
        done = self.client.post("/outside/done", json={"id": result["id"], "version": "x"})
        self.assertEqual(done.status_code, 503, done.text)
        self.assertTrue(self.record(result).exists())   # never deleted on a failed read

    def test_a_filed_item_that_is_gone_stays_404(self):
        result = self.file()
        self.record(result).unlink()
        self.assertEqual(self.client.get("/outside/text", params={"id": result["id"], "version": "x"}).status_code, 404)
        self.assertEqual(self.client.post("/outside/done", json={"id": result["id"], "version": "x"}).status_code, 404)


class ShortMultilineText(OutsideFixture):
    def test_short_text_with_line_breaks_offers_full_text(self):
        """01-012: the one-line excerpt loses line breaks even when short, so the original must be reachable."""
        self.assertTrue(needs._is_excerpted("Line one\nLine two\n\n  - item"))
        self.assertTrue(needs._is_excerpted("a\r\nb"))
        self.assertFalse(needs._is_excerpted("One plain line."))
        self.assertFalse(needs._is_excerpted("  trailing newline stripped\n"))
        self.assertTrue(needs._is_excerpted("x" * 481))

    def test_desktop_note_matches_what_was_changed(self):
        src = (ROOT / "desktop/src/plugin.src.js").read_text(encoding="utf-8")
        self.assertIn("one-line version", src)
        self.assertIn("last 480 characters", src)


ITEM_PRELUDE = r"""
const fs = require('fs'), assert = require('node:assert/strict');
const src = fs.readFileSync(process.argv[1], 'utf8');
const tldr = src.match(/const TLDR_ASK = .*\n/)[0];
const views = tldr + src.slice(src.indexOf('const ANSWER_DRAFT_TTL'), src.indexOf('function Chip('));
const noop = () => {};
const node = (type, props) => ({type, props});
let state = [], cursor = 0, effects = [], deps = [], effectCursor = 0, undoSet = [];
const env = {
  jsx: node, jsxs: node,
  useState: init => { const i = cursor++; if (!(i in state)) state[i] = init; return [state[i], v => { state[i] = typeof v === 'function' ? v(state[i]) : v; }]; },
  useEffect: (fn, d) => { const i = effectCursor++; const prev = deps[i]; if (!prev || !d || d.some((x, k) => x !== prev[k])) { effects.push([i, fn, d]); } deps[i] = d; },
  useRef: () => ({}), platformLabel: x => x, LOCAL_PLATFORMS: [], KEEP_BLOCKED: /keep blocked/i,
  button: props => node('button', props), openBoard: noop, ago: () => '', ageClass: () => '', pad: String,
  sfx: {play: noop}, petEmit: noop, host: {notify: noop, notifyError: e => {throw e}},
  $undo: {set: v => { undoSet.push(v) }}, $queue: 'queue', $fxOn: 'fx', UNDO_MS: 12000, askCount: q => q.count,
  Field: noop, Watermark: noop, Banner: noop, Pet: noop, Ticker: noop, FxToggle: noop, SfxToggle: noop,
  grainUrl: () => '', oldestAskSince: () => 0, replyPlain: x => x, replyParts: x => x,
};
const {Item} = new Function('env', `with(env) { ${views}; return {Item}; }`)(env);
function all(n) { if (n == null) return []; if (Array.isArray(n)) return n.flatMap(all); if (typeof n !== 'object') return [n]; return [n, ...all(n.props?.children)]; }
function text(n) { return all(n).filter(x => typeof x === 'string').join(''); }
async function flush() { const run = effects.splice(0); for (const [, fn] of run) fn(); await new Promise(r => setImmediate(r)); }
function fullButton(tree) { return all(tree).find(x => x?.type === 'button' && x.props.className === 'ub-full'); }
"""


class DesktopExcerptLabel(unittest.TestCase):
    def check(self, body):
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                result = subprocess.run(
                    ["node", "-e", ITEM_PRELUDE + body, str(path)], capture_output=True, text=True, check=False
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_excerpt_is_labelled_whenever_it_is_what_is_shown(self):
        """01-013 and 01-012: closing Full text again must bring the label back, and a short
        multi-line message says it was flattened rather than claiming it was cut."""
        self.check(r"""
const ctx = {rest: async () => ({text: 'FIRST\nSECOND', truncated: false})};
function render(item) { cursor = 0; effects = []; effectCursor = 0; return Item({ctx, item, n: 1, refresh: async () => {}, askEnabled: false}); }
(async () => {
  const item = {id: 'chat:p:s1', version: '9', source: 'chat', title: 't', ask: '…tail', ask_truncated: true, since: 1};
  let tree = render(item);
  assert.ok(text(tree).includes('last 480 characters'));
  fullButton(tree).props.onClick(); tree = render(item); await flush(); tree = render(item);
  assert.ok(text(tree).includes('FIRST'));
  assert.ok(!text(tree).includes('last 480 characters'));
  fullButton(tree).props.onClick(); tree = render(item);
  assert.ok(!text(tree).includes('FIRST'), 'closed again shows the excerpt');
  assert.ok(text(tree).includes('last 480 characters'), 'the excerpt label must come back when Full text is closed');
  state = []; deps = [];
  const short = {id: 'chat:p:s2', version: '1', source: 'chat', title: 't', ask: 'a b c', ask_truncated: true, since: 1};
  tree = render(short);
  assert.ok(text(tree).includes('one-line version'), 'a flattened short message is described as a one-line version');
  assert.ok(!text(tree).includes('last 480 characters'));
})().catch(e => { console.error(e); process.exitCode = 1 });
""")


class JobTables(unittest.TestCase):
    def load(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("ub_jobs_r4", ROOT / "jobs.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_pruning_never_resets_the_attempt_count_of_a_failing_summary(self):
        """01-023: a cooled-down failure carries its attempt count; pruning the table must not drop it."""
        jobs = self.load()
        clock = types.SimpleNamespace(t=1000.0)
        table = jobs.BlufJobs(clock=lambda: clock.t)
        for i in range(jobs.BlufJobs.PRUNE_OVER + 1):
            key = (f"x{i}", "1")
            self.assertTrue(table.start(key))
            table.fail(key, "e")
        clock.t += jobs.BlufJobs.RETRY_S + 1
        self.assertTrue(table.start(("new", "1")))
        self.assertEqual(table.attempts(("x0", "1")), 1)
        self.assertEqual(table.attempts(("x500", "1")), 1)

    def test_a_stuck_pending_job_is_still_pruned_and_the_table_has_a_hard_cap(self):
        jobs = self.load()
        clock = types.SimpleNamespace(t=1000.0)
        table = jobs.BlufJobs(clock=lambda: clock.t)
        self.assertTrue(table.start(("stuck", "1")))
        clock.t += jobs.BlufJobs.STUCK_S + 1
        for i in range(jobs.BlufJobs.PRUNE_OVER + 1):
            key = (f"x{i}", "1")
            table.start(key)
            table.fail(key, "e")
            clock.t += 1
        table.start(("new", "1"))
        self.assertIsNone(table.state(("stuck", "1")))
        for i in range(jobs.BlufJobs.HARD_CAP + 50):
            key = (f"y{i}", "1")
            table.start(key)
            table.fail(key, "e")
            clock.t += 1
        self.assertLessEqual(len(table), jobs.BlufJobs.HARD_CAP + 1)

    def test_an_evicted_ask_answer_is_gone_from_the_table(self):
        """The in-memory settled limit evicts in completion order (the journal-backed cases of
        01-009, 01-024 and 01-025 are covered in tests/test_r4_stack07.py on this branch)."""
        jobs = self.load()
        jobs.AskJobs.MAX_SETTLED = 1
        table = jobs.AskJobs()
        table.create("a")
        table.create("b")
        table.finish("a", "A")
        table.finish("b", "B")
        self.assertIsNone(table.get("a"), "an evicted answer is gone, not recoverable")
        self.assertEqual(table.get("b")["reply"], "B")


ASK_HELPERS = r"""
const btn = (tree, label) => all(tree).find(x => x?.type === 'button' && x.props.children === label);
const box = tree => all(tree).find(x => x?.type === 'textarea' && x.props['aria-label'] === 'Ask a follow-up');
const tick = () => new Promise(r => setImmediate(r));
const chat = {source: 'kanban', id: 'kanban:default:task', board: 'default', task_id: 'task', version: '1', title: 'Launch', ask: 'Which day?', tier: 'ask', since: 1};
function mount(ctx, item = chat, extra = {}) {
  state = []; deps = []; effects = [];
  const render = () => { cursor = 0; effects = []; effectCursor = 0; return Item({ctx, item, n: 1, refresh: extra.refresh || (async () => {}), askEnabled: true}); };
  return render;
}
"""


class DesktopRound4(unittest.TestCase):
    def check(self, body):
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                result = subprocess.run(
                    ["node", "-e", ITEM_PRELUDE + ASK_HELPERS + body, str(path)],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_undo_is_offered_before_the_queue_refresh_finishes(self):
        """01-014: a hung refresh must not hold back Undo for a dismissal that already succeeded."""
        self.check(r"""
const ctx = {rest: async url => url === '/outside/done' ? {hidden: true} : {}};
const item = {id: 'outside:x', version: '1', source: 'outside', origin: 'filed', title: 't', ask: 'a', act_where: 'somewhere', since: 1};
const render = mount(ctx, item, {refresh: () => new Promise(() => {})});
(async () => {
  btn(render(), 'Done').props.onClick();
  await tick(); await tick();
  assert.equal(undoSet.length, 1, 'Undo must be registered while the refresh is still pending');
  assert.equal([].concat(undoSet[0])[0].id, 'outside:x');  // stack/03 keeps a list of dismissals
})().catch(e => { console.error(e); process.exitCode = 1 });
""")

    def test_a_refused_oversize_follow_up_never_enters_the_history(self):
        """01-015: 20,001 characters is refused on the client; a later short question is sent alone."""
        self.check(r"""
const sent = [];
const ctx = {rest: async (url, o) => { sent.push(o?.body); return {reply: 'ok'}; }};
const render = mount(ctx);
(async () => {
  btn(render(), 'TL;DR').props.onClick(); await tick();
  const first = sent.length;
  box(render()).props.onChange({target: {value: 'x'.repeat(20001)}});
  await btn(render(), 'Send').props.onClick();
  assert.equal(sent.length, first, 'an oversize message is not sent');
  assert.ok(text(render()).includes('20000'), 'the limit is explained');
  assert.equal(box(render()).props.value.length, 20001, 'the text stays in the box');
  box(render()).props.onChange({target: {value: 'short'}});
  await btn(render(), 'Send').props.onClick();
  const last = sent[sent.length - 1];
  assert.ok(last.messages.every(m => m.content.length <= 20000), 'the oversize text never reaches the server');
})().catch(e => { console.error(e); process.exitCode = 1 });
""")

    def test_an_admission_refusal_rolls_the_message_back_and_restores_the_input(self):
        """01-015: a 4xx refusal removes the refused message from history, so it is never resent."""
        self.check(r"""
const sent = [];
let refuse = true;
const ctx = {rest: async (url, o) => {
  sent.push(o?.body);
  if (url === '/card/ask' && refuse && o.body.messages.length > 1) { const e = new Error('HTTP 422: too long'); e.status = 422; throw e; }
  return {reply: 'fine'};
}};
const render = mount(ctx);
(async () => {
  btn(render(), 'TL;DR').props.onClick(); await tick();
  box(render()).props.onChange({target: {value: 'bad follow-up'}});
  await btn(render(), 'Send').props.onClick();
  assert.equal(box(render()).props.value, 'bad follow-up', 'the refused text comes back for editing');
  refuse = false;
  box(render()).props.onChange({target: {value: 'good follow-up'}});
  await btn(render(), 'Send').props.onClick();
  const last = sent[sent.length - 1].messages.map(m => m.content);
  assert.ok(!last.includes('bad follow-up'), 'the refused message is not resent');
  assert.ok(last.includes('good follow-up'));
})().catch(e => { console.error(e); process.exitCode = 1 });
""")

    def test_the_request_carries_the_card_version_and_a_stable_request_id(self):
        """01-016 and 01-017: the version binds the request; Retry after a lost POST reuses the id."""
        self.check(r"""
const posts = [];
let lose = true;
const ctx = {rest: async (url, o) => {
  if (url === '/card/ask') {
    posts.push(o.body);
    if (lose) { lose = false; throw new Error('network down'); }
    return {reply: 'answer'};
  }
  return {};
}};
const render = mount(ctx);
(async () => {
  btn(render(), 'TL;DR').props.onClick(); await tick();
  assert.equal(posts[0].version, '1');
  assert.match(posts[0].request_id, /^[0-9a-f]{32}$/);
  await btn(render(), 'Retry').props.onClick();
  assert.equal(posts.length, 2);
  assert.equal(posts[1].request_id, posts[0].request_id, 'Retry reuses the id of the lost request');
  box(render()).props.onChange({target: {value: 'next'}});
  await btn(render(), 'Send').props.onClick();
  assert.notEqual(posts[2].request_id, posts[0].request_id, 'a new question gets a new id');
})().catch(e => { console.error(e); process.exitCode = 1 });
""")

    def test_the_150_second_limit_covers_posts_sleeps_and_terminal_replies(self):
        """01-018: polls that each take 30 s must fail at the limit, even when the last one answers."""
        self.check(r"""
let now = 0;
env.Date = {now: () => now};
env.setTimeout = (fn, ms) => { now += ms; fn(); return 1; };
let polls = 0;
const ctx = {rest: async url => {
  if (url === '/card/ask') { now += 30000; return {pending: true, job: 'j'}; }
  if (!url.startsWith('/card/ask/')) return {};
  polls++; now += 30000;
  return polls < 6 ? {pending: true, job: 'j'} : {ok: true, reply: 'late answer'};
}};
const render = mount(ctx);
(async () => {
  btn(render(), 'TL;DR').props.onClick(); await tick(); await tick();
  assert.ok(!text(render()).includes('late answer'), 'an answer after 150 s is not accepted');
  assert.ok(text(render()).includes('over 150 s'));
  assert.ok(now <= 150000 + 30000 + 1500, 'no sleep runs past the limit: ' + now);
})().catch(e => { console.error(e); process.exitCode = 1 });
""")


class CoverageClaims(unittest.TestCase):
    def test_no_session_cap_exists_so_an_older_ask_is_never_omitted(self):
        """01-020 DISPUTED on this branch: chat discovery has no 500-session cap and reports no
        sessions_omitted, so an older unanswered ask among 600 newer answered chats is still listed
        and ALL CLEAR cannot hide an omission. A later branch that adds a cap owns that fix."""
        import tempfile
        from test_cover import home_with, needs as cover_needs, ASK
        rows = [("old", "", "telegram", "dm", None, "assistant", ASK)]
        rows += [(f"new{i}", "", "telegram", "dm", None, "user", "thanks") for i in range(600)]
        with tempfile.TemporaryDirectory() as d:
            ids = [i["session_id"] for i in cover_needs.chat_items(home_with(Path(d), rows))]
        self.assertEqual(ids, ["old"])
        self.assertNotIn("sessions_omitted", (ROOT / "needs.py").read_text())

    def test_verification_doc_describes_the_folder_paging_this_branch_implements(self):
        """01-022: the paging branch replaces the "pagination not adopted" sentence with the
        completion-driven paging it implements, and the doc must not claim the opposite."""
        doc = " ".join((ROOT / "docs/verification.md").read_text().split())
        self.assertNotIn("Folder pagination and automatic export archiving were not adopted", doc)
        self.assertIn("Done or Not for me on a file brings an older one into view", doc)
        needs_src = (ROOT / "needs.py").read_text()
        self.assertIn("older files are waiting", needs_src)
        self.assertNotIn("page_token", needs_src)


if __name__ == "__main__":
    unittest.main()
