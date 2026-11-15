"""Round-5 findings for the Desktop card: stale answer drafts, the Ask deadline and the excerpt label."""

import subprocess
import unittest
from pathlib import Path

from test_outside import needs
from test_r4_stack01 import ASK_HELPERS, ITEM_PRELUDE

ROOT = Path(__file__).resolve().parents[1]


class DesktopRound5(unittest.TestCase):
    def check(self, body):
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                result = subprocess.run(
                    ["node", "-e", ITEM_PRELUDE + ASK_HELPERS + body, str(path)],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_answer_typed_for_one_question_is_not_sent_for_an_edited_one(self):
        """R5-desk-004 (class A): an edit in place keeps the block version, so the text binds the draft."""
        self.check(r"""
const posts = [];
const ctx = {rest: async (url, o) => { posts.push(o?.body); return {ok: true, released: true}; }};
state = []; deps = [];
let item = {...chat};
const render = () => { cursor = 0; effects = []; effectCursor = 0; return Item({ctx, item, n: 1, refresh: async () => {}, askEnabled: false}); };
const area = tree => all(tree).find(x => x?.type === 'textarea' && x.props['aria-label'] === 'Answer for task');
const answerBtn = tree => all(tree).find(x => x?.type === 'button' && x.props.children === 'Answer + unbl0ck');
(async () => {
  let tree = render();
  area(tree).props.onChange({target: {value: 'Monday'}});
  tree = render();
  assert.equal(answerBtn(tree).props.disabled, false, 'the draft for the current question can be sent');
  item = {...chat, ask: 'Which database should we use?'};
  tree = render();
  assert.equal(answerBtn(tree).props.disabled, true, 'a draft for the old question is not sendable');
  await answerBtn(tree).props.onClick();
  assert.equal(posts.length, 0, 'nothing is posted for the edited question');
  assert.ok(text(tree).includes('older version'), 'the draft says why');
})().catch(e => { console.error(e); process.exitCode = 1 });
""")

    def test_no_poll_starts_once_the_sleep_has_used_up_the_limit(self):
        """R5-desk-010 (class D): the budget is checked after the sleep, before the next request."""
        self.check(r"""
let now = 0;
env.Date = {now: () => now};
env.setTimeout = (fn, ms) => { now += ms; fn(); return 1; };
let polls = 0;
const ctx = {rest: async url => {
  if (url === '/card/ask') { now += 149500; return {pending: true, job: 'j'}; }
  if (!url.startsWith('/card/ask/')) return {};
  polls++; now += 30000;
  return {pending: true, job: 'j'};
}};
const render = mount(ctx);
(async () => {
  btn(render(), 'TL;DR').props.onClick(); await tick(); await tick();
  assert.equal(polls, 0, 'a poll started at the limit');
  assert.ok(text(render()).includes('over 150 s'));
})().catch(e => { console.error(e); process.exitCode = 1 });
""")

    def test_a_terminal_reply_after_the_limit_is_refused_whichever_response_carries_it(self):
        """R5-desk-020 (class L): both terminal-response checks are reached, and the overshoot is one request."""
        self.check(r"""
async function run(scenario) {
  let now = 0;
  env.Date = {now: () => now};
  env.setTimeout = (fn, ms) => { now += ms; fn(); return 1; };
  const ctx = {rest: async url => scenario(url, () => now, d => { now += d; })};
  const render = mount(ctx);
  btn(render(), 'TL;DR').props.onClick(); await tick(); await tick();
  return {shown: text(render()), now};
}
(async () => {
  // The first response is already terminal and arrives after the limit.
  let r = await run((url, _n, add) => { add(151000); return {ok: true, reply: 'late first'}; });
  assert.ok(!r.shown.includes('late first'), 'a late first response is not accepted');
  assert.ok(r.shown.includes('over 150 s'));
  // A poll answers after the limit: the pending loop has not stopped it yet.
  let polls = 0;
  r = await run((url, _n, add) => {
    if (url === '/card/ask') return {pending: true, job: 'j'};
    if (!url.startsWith('/card/ask/')) return {};
    polls++; add(polls === 1 ? 151000 : 1); return {ok: true, reply: 'late poll'};
  });
  assert.ok(!r.shown.includes('late poll'), 'a late terminal poll is not accepted');
  assert.ok(r.shown.includes('over 150 s'));
  assert.ok(r.now <= 180000, 'the overshoot is at most one request: ' + r.now);
})().catch(e => { console.error(e); process.exitCode = 1 });
""")

    def test_a_literal_leading_ellipsis_is_not_called_a_cut(self):
        """R5-desk-024 (class K): only a cut excerpt says 'last 480 characters'."""
        message = "…Can you\napprove?"
        self.assertTrue(needs._is_excerpted(message))
        self.assertFalse(needs._is_cut(message))
        self.assertTrue(needs._is_cut("x" * 481))
        self.check(r"""
const ctx = {rest: async () => ({})};
const flat = {id: 'chat:p:s1', version: '1', source: 'chat', title: 't', ask: '…Can you approve?', ask_truncated: true, ask_cut: false, since: 1};
const cut = {id: 'chat:p:s2', version: '1', source: 'chat', title: 't', ask: '…tail', ask_truncated: true, ask_cut: true, since: 1};
const mountItem = it => { state = []; deps = []; cursor = 0; effects = []; effectCursor = 0; return Item({ctx, item: it, n: 1, refresh: async () => {}, askEnabled: false}); };
assert.ok(!text(mountItem(flat)).includes('last 480 characters'), 'a flattened message is not called cut');
assert.ok(text(mountItem(flat)).includes('one-line version'));
assert.ok(text(mountItem(cut)).includes('last 480 characters'));
""")

    def test_the_server_marks_a_real_cut_in_the_item(self):
        import tempfile
        from test_cover import home_with, needs as cover_needs

        rows = [
            ("long", "", "telegram", "dm", None, "assistant", "w " * 400 + "Can you approve?"),
            ("flat", "", "telegram", "dm", None, "assistant", "\u2026Can you\napprove?"),
        ]
        with tempfile.TemporaryDirectory() as d:
            items = {i["session_id"]: i for i in cover_needs.chat_items(home_with(Path(d), rows))}
        self.assertTrue(items["long"]["ask_cut"])
        self.assertTrue(items["flat"]["ask_truncated"])
        self.assertFalse(items["flat"]["ask_cut"])


if __name__ == "__main__":
    unittest.main()
