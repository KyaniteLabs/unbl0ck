"""Round-4 review findings owned by the first stack branch (full text), group by group."""

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


if __name__ == "__main__":
    unittest.main()
