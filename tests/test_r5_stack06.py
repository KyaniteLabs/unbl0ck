"""Round 5, stack/06: Ask tokens, per-connection scope, Retry, reply bounds and lifecycle coverage.

Each test is the finding's reproducer on the real code path: the real Item component, source and
built, with real timers where the finding is about a lifecycle."""
import importlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_operator_recovery import HARNESS
from test_outside import needs
from test_r4_stack06 import STORE

ROOT = Path(__file__).resolve().parents[1]

try:
    kanban_db = importlib.import_module("hermes_cli.kanban_db")
    kanban_db_connect = importlib.import_module("hermes_cli.kanban_db_connect")
    HERMES_ERROR = None
except ImportError as exc:  # pragma: no cover
    HERMES_ERROR = str(exc)


class NodeCase(unittest.TestCase):
    def run_views(self, script):
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                result = subprocess.run(
                    ["node", "-e", HARNESS + STORE + script, str(path)], capture_output=True, text=True, timeout=60
                )
                self.assertEqual(result.returncode, 0, result.stderr)


class AskTokens(NodeCase):
    def test_retry_after_a_remount_reuses_the_request_id(self):
        """R5-desk-001 (class A): a lost response must not let the remounted card start a second model call."""
        self.run_views(r"""
const ids=[];
const ctx={rest:async(url,o)=>{if(url!=='/card/ask')return{};ids.push(o.body.request_id);
  if(ids.length===1)throw new Error('connection lost');return{ok:true,reply:'Answer.'}}};
const it={...item,id:'kanban:default:rq1',version:'1'};
(async()=>{
 mount(ctx,it);
 await find(mount(ctx,it),'TL;DR').onClick();
 assert.equal(ids.length,1);
 state=[];                                   // the card unmounts and mounts again
 await find(mount(ctx,it),'Retry').onClick();
 assert.equal(ids.length,2);
 assert.ok(ids[0]&&ids[0]===ids[1],'Retry must carry the first request id: '+ids.join(' / '));
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_a_request_started_for_an_older_text_does_not_lend_its_id_to_changed_text(self):
        """R5-desk-001 (class A): the saved id is tied to the card text, like the conversation."""
        self.run_views(r"""
const ids=[];
const ctx={rest:async(url,o)=>{if(url!=='/card/ask')return{};ids.push(o.body.request_id);
  if(ids.length===1)throw new Error('connection lost');return{ok:true,reply:'Answer.'}}};
const it={...item,id:'kanban:default:rq2',version:'1',title:'Old title'};
(async()=>{
 mount(ctx,it);
 await find(mount(ctx,it),'TL;DR').onClick();
 state=[];
 await find(mount(ctx,{...it,title:'New title'}),'Retry').onClick();
 assert.equal(ids.length,2);
 assert.notEqual(ids[0],ids[1],'changed text is a new question and needs a new id');
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_an_evicted_older_request_cannot_reclaim_the_conversation(self):
        """R5-desk-008 (class A): A and B are both evicted; A's late reply must not displace B."""
        self.run_views(r"""
const pending=[];
const ctx={rest:(url,o)=>{if(url!=='/card/ask')return Promise.resolve({});
  return new Promise(res=>pending.push({res,q:o.body.messages[o.body.messages.length-1].content}))}};
const it={...item,id:'kanban:default:ev1',version:'1'};
(async()=>{
 mount(ctx,it);
 find(mount(ctx,it),'TL;DR').onClick();                      // request A
 await new Promise(r=>setTimeout(r,5));
 store.askSessions.delete(it.id);                            // A's session is evicted
 mount(ctx,it);tick();                                       // the card reports the loss and unlocks
 area(mount(ctx,it)).onChange({target:{value:'second question'}});
 find(mount(ctx,it),'Send').onClick();                       // request B
 await new Promise(r=>setTimeout(r,5));
 assert.equal(pending.length,2);
 store.askSessions.delete(it.id);                            // B's session is evicted too
 pending[0].res({ok:true,reply:'A reply'});                  // A completes first, then B
 await new Promise(r=>setTimeout(r,5));
 pending[1].res({ok:true,reply:'B reply'});
 await new Promise(r=>setTimeout(r,5));
 const s=store.askSessions.get(it.id);
 const last=s&&s.msgs.length?s.msgs[s.msgs.length-1].content:'';
 assert.ok(!s||s.msgs.every(m=>m.content!=='Summarize'&&!m.content.startsWith('TL;DR')),'the old conversation came back: '+JSON.stringify(s&&s.msgs));
 assert.ok(!texts(mount(ctx,it)).includes('A reply'),'A reply must never be shown');
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_an_edited_description_or_comment_body_makes_advice_stale(self):
        """R5-desk-005 (class A): the model reads the description and the comment bodies."""
        self.run_views(r"""
const ctx={rest:async()=>({ok:true,reply:'Old advice.'})};
const it={...item,id:'kanban:default:sg1',version:'4',title:'Launch',comments_sig:'1:5:aaaa',body_sig:'bbbb'};
(async()=>{
 mount(ctx,it);
 await find(mount(ctx,it),'TL;DR').onClick();
 state=[];
 assert.equal(find(mount(ctx,it),'Use as answer').disabled,false);
 for(const edited of [{body_sig:'cccc'},{comments_sig:'1:5:dddd'}]){
  state=[];
  const t=mount(ctx,{...it,...edited});
  assert.ok(texts(t).includes('This card changed'),JSON.stringify(edited));
  assert.equal(find(t,'Use as answer').disabled,true,JSON.stringify(edited));
 }
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_text_that_moves_a_separator_between_fields_is_a_different_context(self):
        """R5-desk-023 (class A): delimiter-joined fields collided when a field contained the delimiter."""
        self.run_views(r"""
const ctx={rest:async()=>({ok:true,reply:'Old advice.'})};
const it={...item,id:'kanban:default:dl1',version:'4',title:'A\u001fB',ask:'C'};
(async()=>{
 mount(ctx,it);
 await find(mount(ctx,it),'TL;DR').onClick();
 state=[];
 const t=mount(ctx,{...it,title:'A',ask:'B\u001fC'});
 assert.ok(texts(t).includes('This card changed'),'different visible text must not look unchanged');
 assert.equal(find(t,'Use as answer').disabled,true);
})().catch(e=>{console.error(e);process.exitCode=1});
""")


@unittest.skipIf(HERMES_ERROR, f"Hermes unavailable: {HERMES_ERROR}")
class KanbanContextSignature(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.dict(os.environ, {"HERMES_HOME": self.tmp.name, "HERMES_KANBAN_HOME": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(needs, "_settings", lambda: {"principal_name": "CEO", "comment_author": "operator"})
        p.start()
        self.addCleanup(p.stop)

    def conn(self):
        return kanban_db_connect.connect(board="default")

    def card(self):
        with self.conn() as conn:
            task = kanban_db.create_task(conn, title="Choose a database", body="Use the first plan.", assignee="worker")
            task = getattr(task, "id", task)
            kanban_db.block_task(conn, task, reason="Needs CEO approval", kind="needs_input")
            kanban_db.add_comment(conn, task, author="worker", body="First note")
            return task

    def item(self):
        (item,) = needs.kanban_items(boards=["default"])
        return item

    def test_an_edited_description_changes_the_item_and_the_summary_fingerprint(self):
        """R5-desk-005 (class A): the description is model-visible card text."""
        task = self.card()
        before = self.item()
        with self.conn() as conn:
            conn.execute("UPDATE tasks SET body=? WHERE id=?", ("Use the second plan.", task))
            conn.commit()
        after = self.item()
        self.assertEqual(before["version"], after["version"])
        self.assertNotEqual(before.get("body_sig"), after.get("body_sig"))
        self.assertNotEqual(needs.summary_fingerprint(before), needs.summary_fingerprint(after))

    def test_an_edited_comment_body_changes_the_item_and_the_summary_fingerprint(self):
        """R5-desk-005 (class A): the count and newest id do not move when a comment is rewritten."""
        task = self.card()
        before = self.item()
        with self.conn() as conn:
            conn.execute("UPDATE task_comments SET body=? WHERE task_id=?", ("Rewritten note", task))
            conn.commit()
        after = self.item()
        self.assertNotEqual(before["comments_sig"], after["comments_sig"])
        self.assertNotEqual(needs.summary_fingerprint(before), needs.summary_fingerprint(after))

    def test_the_signatures_carry_no_card_text(self):
        """The signature is a digest: the queue item must not gain a second copy of the text."""
        self.card()
        item = self.item()
        for key in ("body_sig", "comments_sig"):
            self.assertNotIn("second plan", str(item[key]))
            self.assertNotIn("First note", str(item[key]))
            self.assertLess(len(str(item[key])), 40)

    def test_the_summary_fingerprint_cannot_be_forged_by_moving_a_separator(self):
        """R5-desk-023 (class A, server twin): the same collision existed in the summary fingerprint."""
        one = needs.summary_fingerprint({"title": "A\x1fB", "ask": "C"})
        two = needs.summary_fingerprint({"title": "A", "ask": "B\x1fC"})
        self.assertNotEqual(one, two)



# The whole module with its imports stubbed: real atoms, the real register(), Page, Item and Undo.
MODULE = r"""
const fs = require('fs'), assert = require('node:assert/strict');
const src = fs.readFileSync(process.argv[1], 'utf8');
const noop = () => {};
const atoms = [];
const atom = init => { let v = init; const a = {get: () => v, set: x => { v = x }}; atoms.push(a); return a; };
const hooks = {state: [], cursor: 0};
const node = (type, props, key) => ({type, props, key});
const styleEl = () => ({dataset: {}, remove: noop});
const doc = {createElement: styleEl, head: {appendChild: noop}, addEventListener: noop, removeEventListener: noop, hidden: false};
const sdk = {
  atom, host: {notify: noop, notifyError: noop, navigate: noop}, useValue: a => a.get(),
  ROUTES_AREA: 'route', SIDEBAR_NAV_AREA: 'nav', STATUSBAR_AREAS: {right: 'right'},
  useEffect: noop, useRef: () => ({}), jsx: node, jsxs: node,
  useState: init => { const i = hooks.cursor++; if (!(i in hooks.state)) hooks.state[i] = init; return [hooks.state[i], v => { hooks.state[i] = typeof v === 'function' ? v(hooks.state[i]) : v }]; },
};
const body = src.replace(/^import \{[\s\S]*?\} from '[^']+'\n/gm, '').replace(/^import .*\n/gm, '').replace('export default ', 'return ');
const names = Object.keys(sdk);
const plugin = new Function(...names, 'document', 'setInterval', 'clearInterval', 'setTimeout', 'clearTimeout', body)(
  ...names.map(k => sdk[k]), doc, () => 1, noop, () => 1, noop);
const $queue = atoms.find(a => a.get() && 'loading' in a.get());
const $undo = atoms.find(a => Array.isArray(a.get()));
const tick = () => new Promise(r => setImmediate(r));
function all(n) { if (n == null) return []; if (Array.isArray(n)) return n.flatMap(all); if (typeof n !== 'object') return [n]; return [n, ...all(n.props?.children)]; }
function connect(rest) {
  let dispose, page;
  plugin.register({rest, registerMany: list => { page = list[0] }, onDispose: f => { dispose = f }});
  return {page: () => page.render(), dispose: () => dispose(), scope: page.render().props.scope};
}
// Render a connection's Page component once and return its element tree.
function renderPage(conn) {
  hooks.cursor = 0; hooks.state = [];
  const el = conn.page();
  return el.type(el.props);
}
"""


class ConnectionScope(unittest.TestCase):
    def run_module(self, script):
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                result = subprocess.run(
                    ["node", "-e", MODULE + script, str(path)], capture_output=True, text=True, timeout=60
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_failed_queue_on_the_next_connection_does_not_show_the_last_one(self):
        """R5-desk-002 (class F): dispose A, register B whose /queue fails: B shows nothing of A."""
        self.run_module(r"""
(async () => {
  const a = connect(async () => ({items: [{id: 'kanban:default:a', source: 'kanban', title: 'Card of A', version: '1'}], count: 1, errors: []}));
  await tick(); await tick();
  assert.equal($queue.get().items.length, 1, 'A loaded its queue');
  $undo.set([{id: 'kanban:default:a', title: 'Card of A', at: Date.now(), kind: 'hidden', token: 'ta', scope: a.scope}]);
  a.dispose();
  const b = connect(async () => { throw new Error('backend B is down'); });
  await tick(); await tick();
  const q = $queue.get();
  assert.deepEqual(q.items, [], 'B must not show A cards: ' + JSON.stringify(q.items));
  assert.equal(q.fault, 'backend B is down');
  assert.deepEqual(($undo.get() || []).filter(u => u.title === 'Card of A'), [], 'A Undo record must not outlive A');
})().catch(e => { console.error(e); process.exitCode = 1 });
""")

    def test_a_failed_queue_hides_the_last_connection_when_the_next_registers_first(self):
        """R5-desk-002 (class F): the host may register B before it disposes A."""
        self.run_module(r"""
(async () => {
  const a = connect(async () => ({items: [{id: 'kanban:default:a', source: 'kanban', title: 'Card of A', version: '1'}], count: 1, errors: []}));
  await tick(); await tick();
  const b = connect(async () => { throw new Error('backend B is down'); });
  await tick(); await tick();
  assert.deepEqual($queue.get().items, [], 'B must not show A cards: ' + JSON.stringify($queue.get().items));
  a.dispose();
  assert.equal($queue.get().fault, 'backend B is down', 'disposing A leaves what B shows alone');
})().catch(e => { console.error(e); process.exitCode = 1 });
""")

    def test_a_dismissal_made_on_one_connection_is_not_offered_by_another(self):
        """R5-desk-002 (class F): Undo of A's dismissal is not shown by B's page, so it is never sent through B."""
        self.run_module(r"""
(async () => {
  const a = connect(async () => ({items: [], count: 0, errors: []}));
  const b = connect(async () => ({items: [], count: 0, errors: []}));
  await tick(); await tick();
  $undo.set([{id: 'kanban:default:a', title: 'Card of A', at: Date.now(), kind: 'hidden', token: 'ta', scope: a.scope}]);
  const undoButtons = conn => all(renderPage(conn)).filter(x => x?.type === 'button' && all(x.props.children).includes('Undo'));
  assert.equal(undoButtons(b).length, 0, 'B must not offer the dismissal made on A');
  assert.equal(undoButtons(a).length, 1, 'A still offers its own');
})().catch(e => { console.error(e); process.exitCode = 1 });
""")

    def test_the_page_and_each_card_are_keyed_by_connection(self):
        """R5-desk-003 (class F): without a key React reuses mounted answer and advice state across connections."""
        self.run_module(r"""
(async () => {
  const items = [{id: 'kanban:default:t1', source: 'kanban', tier: 'ask', title: 'T', ask: 'Q?', version: '1', board: 'default', task_id: 't1', since: 1}];
  const a = connect(async () => ({items, count: 1, errors: []}));
  await tick(); await tick();
  const keyA = a.page().key;
  const cardsA = all(renderPage(a)).filter(x => x?.props?.item?.id === items[0].id);
  a.dispose();
  const b = connect(async () => ({items, count: 1, errors: []}));
  await tick(); await tick();
  const cardsB = all(renderPage(b)).filter(x => x?.props?.item?.id === items[0].id);
  assert.ok(keyA && b.page().key && keyA !== b.page().key, 'Page needs a per-connection key');
  assert.equal(cardsA.length, 1); assert.equal(cardsB.length, 1);
  assert.ok(cardsA[0].key && cardsA[0].key !== cardsB[0].key, 'the same card id under two connections must not share a mounted component: ' + cardsA[0].key + ' / ' + cardsB[0].key);
})().catch(e => { console.error(e); process.exitCode = 1 });
""")


class DismissalScope(NodeCase):
    def test_every_dismissal_is_stamped_with_its_connection(self):
        """R5-desk-002 (class F): both Undo records carry the scope they were made under."""
        self.run_views(r"""
undo=[];
const ctx={rest:async url=>url==='/hide'?{token:'t1',order:1}:{hidden:true,token:'t2',order:2}};
const render=(it,scope)=>{cursor=0;return store.Item({ctx,item:it,n:1,refresh,askEnabled:false,scope})};
(async()=>{
 await find(render({...item,id:'kanban:default:s1'},'c7'),'Not for me').onClick();
 const o={source:'outside',origin:'filed',id:'outside:s2',version:'1',kind:'note',title:'T',ask:'a',act_where:'x',since:1};
 await find(render(o,'c7'),'Done').onClick();
 const entries=undo;
 assert.ok(entries.length>=1);
 for(const e of entries)assert.equal(e.scope,'c7',JSON.stringify(e));
})().catch(e=>{console.error(e);process.exitCode=1});
""")



class RetryRetries(NodeCase):
    def test_retry_after_the_conversation_was_dropped_while_answering_asks_again(self):
        """R5-desk-007 A (class G): the loss message offers Retry, and Retry must send a request."""
        self.run_views(r"""
const posts=[];
const ctx={rest:(url,o)=>{if(url!=='/card/ask')return Promise.resolve({});posts.push(o.body.messages);
  return posts.length===1?new Promise(()=>{}):Promise.resolve({ok:true,reply:'Fresh answer.'})}};
const it={...item,id:'kanban:default:rt1',version:'1'};
(async()=>{
 mount(ctx,it);
 find(mount(ctx,it),'TL;DR').onClick();
 await new Promise(r=>setTimeout(r,5));
 store.askSessions.delete(it.id);                 // evicted while the model is answering
 mount(ctx,it);tick();                            // the observer interval reports the loss
 const lost=mount(ctx,it);
 assert.ok(texts(lost).includes('dropped from session memory'),texts(lost));
 await find(lost,'Retry').onClick();
 assert.equal(posts.length,2,'Retry must send a request');
 assert.equal(posts[1].length,1,'only the retried message is sent, never the lost history');
 assert.ok(texts(mount(ctx,it)).includes('Fresh answer.'));
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_retry_after_a_refused_follow_up_sends_the_follow_up_again(self):
        """R5-desk-007 B (class G): the rolled-back history ends in an assistant turn; Retry still has to act."""
        self.run_views(r"""
const posts=[];
const ctx={rest:async(url,o)=>{if(url!=='/card/ask')return{};posts.push(o.body.messages);
  if(posts.length===2)throw Object.assign(new Error('422 refused'),{status:422});
  return{ok:true,reply:'Reply '+posts.length+'.'}}};
const it={...item,id:'kanban:default:rt2',version:'1'};
(async()=>{
 mount(ctx,it);
 await find(mount(ctx,it),'TL;DR').onClick();
 area(mount(ctx,it)).onChange({target:{value:'my follow-up'}});
 await find(mount(ctx,it),'Send').onClick();
 assert.equal(posts.length,2);
 assert.ok(texts(mount(ctx,it)).includes('refused'));
 await find(mount(ctx,it),'Retry').onClick();
 assert.equal(posts.length,3,'Retry must send a request');
 const last=posts[2][posts[2].length-1];
 assert.deepEqual([last.role,last.content],['user','my follow-up']);
 assert.ok(texts(mount(ctx,it)).includes('Reply 3.'));
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_no_retry_button_is_shown_when_there_is_nothing_to_retry(self):
        """R5-desk-007 (class G): a fault with an assistant turn last and an empty input has nothing to resend."""
        self.run_views(r"""
const ctx={rest:async()=>({ok:true,reply:'Reply.'})};
const it={...item,id:'kanban:default:rt3',version:'1'};
(async()=>{
 mount(ctx,it);
 await find(mount(ctx,it),'TL;DR').onClick();
 store.updateAskSession(it.id,{error:'Something odd'});
 state=[];
 const t=mount(ctx,it);
 assert.ok(texts(t).includes('Something odd'));
 assert.ok(!all(t).some(n=>n?.type==='button'&&all(n.props.children).includes('Retry')),'a Retry that cannot act must not be offered');
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_retry_after_a_refused_first_ask_asks_again(self):
        """R5-desk-007 (class G): a first request refused outright leaves no history; Retry still acts."""
        self.run_views(r"""
const posts=[];
const ctx={rest:async(url,o)=>{if(url!=='/card/ask')return{};posts.push(o.body.messages);
  if(posts.length===1)throw Object.assign(new Error('422 refused'),{status:422});
  return{ok:true,reply:'Now answered.'}}};
const it={...item,id:'kanban:default:rt4',version:'1'};
(async()=>{
 mount(ctx,it);
 await find(mount(ctx,it),'TL;DR').onClick();
 await find(mount(ctx,it),'Retry').onClick();
 assert.equal(posts.length,2,'Retry must send a request');
 assert.ok(texts(mount(ctx,it)).includes('Now answered.'));
})().catch(e=>{console.error(e);process.exitCode=1});
""")


class Stack06Docs(unittest.TestCase):
    def test_docs_name_the_text_that_makes_advice_stale_and_the_retry_id(self):
        text = " ".join((ROOT / "docs/operator-guide.md").read_text().split())
        for needle in (
            "owner, description or comments (including a comment rewritten in place)",
            "reuses the request's id",
            "removes its conversations, its queue and its Undo list",
            "remounted per connection",
        ):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
