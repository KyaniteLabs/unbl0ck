"""Execute the source and generated Desktop views for inbox usability regressions."""

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DesktopQuality(unittest.TestCase):
    def run_views(self, script):
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                result = subprocess.run(["node", "-e", script, str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_disposal_ignores_a_late_queue_response(self):
        self.run_views(r"""
const fs = require('fs'), assert = require('node:assert/strict');
const src = fs.readFileSync(process.argv[1], 'utf8');
let resolve, dispose, writes = 0, follows = 0, intervals = 0;
const noop = () => {};
const env = {
  CSS:'', PATH:'/unblock', ROUTES_AREA:'route', SIDEBAR_NAV_AREA:'nav', STATUSBAR_AREAS:{right:'right'}, POLL_MS:45000,
  document:{createElement:()=>({dataset:{},remove:noop}),head:{appendChild:noop},addEventListener:noop,removeEventListener:noop},
  setTimeout:()=>{follows++;return 1},clearTimeout:noop,setInterval:()=>{intervals++;return 2},clearInterval:()=>{intervals--},
  $queue:{get:()=>({}),set:()=>{writes++}},sfx:{close:noop,play:noop},petEmit:noop,askScopeSeq:0,endAskScope:noop,
};
const plugin = new Function('env', `with(env) { return (${src.slice(src.indexOf('export default ') + 'export default '.length)}); }`)(env);
plugin.register({rest:()=>new Promise(r=>{resolve=r}),registerMany:noop,onDispose:f=>{dispose=f}});
dispose();
resolve({items:[{id:'a',bluf_pending:true}],errors:[]});
setImmediate(()=>{assert.equal(writes,0,'disposed plugin must ignore late data');assert.equal(follows,0,'disposed plugin must not restart polling');assert.equal(intervals,0)});
""")

    def test_a_refresh_after_an_action_never_reuses_a_load_that_started_before_it(self):
        self.run_views(r"""
const fs = require('fs'), assert = require('node:assert/strict');
const src = fs.readFileSync(process.argv[1], 'utf8');
const resolvers = [], writes = [];
const noop = () => {};
const env = {
  CSS:'', PATH:'/unblock', ROUTES_AREA:'route', SIDEBAR_NAV_AREA:'nav', STATUSBAR_AREAS:{right:'right'}, POLL_MS:45000,
  document:{createElement:()=>({dataset:{},remove:noop}),head:{appendChild:noop},addEventListener:noop,removeEventListener:noop},
  setTimeout:()=>1,clearTimeout:noop,setInterval:()=>2,clearInterval:noop,
  $queue:{get:()=>({}),set:v=>{writes.push(v)}},sfx:{close:noop,play:noop},petEmit:noop,askScopeSeq:0,endAskScope:noop,
  jsx:(type,props)=>({type,props}),Page:'Page',Chip:'Chip',
};
const plugin = new Function('env', `with(env) { return (${src.slice(src.indexOf('export default ') + 'export default '.length)}); }`)(env);
let refresh;
plugin.register({rest:()=>new Promise(r=>{resolvers.push(r)}),registerMany:list=>{refresh=list[0].render().props.refresh},onDispose:noop});
(async () => {
  assert.equal(resolvers.length, 1, 'the first load started at registration');
  // An action completes while that load is still in flight: its answer was read BEFORE the action.
  const after = refresh();
  resolvers[0]({items:[{id:'old'}],errors:[]});
  await new Promise(r => setImmediate(r));
  assert.equal(resolvers.length, 2, 'a load that began after the action must run');
  resolvers[1]({items:[{id:'new'}],errors:[]});
  await after;
  assert.equal(writes[writes.length - 1].items[0].id, 'new', 'the queue shown is the one read after the action');
})().catch(e => { console.error(e); process.exitCode = 1 });
""")

    def test_chip_warns_when_no_items_could_be_loaded(self):
        self.run_views(r"""
const fs = require('fs'), assert = require('node:assert/strict');
const src = fs.readFileSync(process.argv[1], 'utf8');
const chip = src.match(/function Chip\(\) \{[\s\S]*?\n\}/)[0];
for (const q of [{count:0,errors:['unavailable']},{count:0,fault:'offline'}]) {
 const render = new Function('jsx','useValue','$queue','askCount',`${chip};return Chip;`)((type,props)=>props,()=>q,{},q=>q.count);
 assert.equal(render().children,'◆ QUEUE INCOMPLETE');
}
""")

    def test_filters_retain_cards_and_folder_done_can_be_undone(self):
        script = r"""
const fs = require('fs'), assert = require('node:assert/strict');
const src = fs.readFileSync(process.argv[1], 'utf8');
const views = src.slice(src.indexOf('const ANSWER_DRAFT_TTL'), src.indexOf('function Chip('));
const node = (type, props, key) => ({type, props, key}), noop = () => {};
let state = [], cursor = 0, undo = [];
const env = {
  jsx: node, jsxs: node, useState: init => { const i = cursor++; if (!(i in state)) state[i] = init; return [state[i], v => { state[i] = typeof v === 'function' ? v(state[i]) : v; }]; },
  useEffect: noop, useRef: () => ({}), platformLabel: x => x, LOCAL_PLATFORMS: [], KEEP_BLOCKED: /keep blocked/i,
  button: props => node('button', props), openBoard: noop, ago: () => '', ageClass: () => '', pad: String,
  sfx: {play: noop}, petEmit: noop, host: {notify: noop, notifyError: e => {throw e}},
  $undo: {get: () => undo, set: v => {undo = v}}, $queue: 'queue', $fxOn: 'fx', UNDO_MS: 12000, askCount: q => q.count,
  Field: noop, Watermark: noop, Banner: noop, Pet: noop, Ticker: noop, FxToggle: noop, SfxToggle: noop,
  grainUrl: () => '', oldestAskSince: () => 0, reducedMotion: () => true,
};
const {Item, Page} = new Function('env', `with(env) { ${views}; return {Item, Page}; }`)(env);
function all(n) { if (n == null) return []; if (Array.isArray(n)) return n.flatMap(all); if (typeof n !== 'object') return [n]; return [n, ...all(n.props?.children)]; }
const ctx = {rest: async () => ({ok: true, hidden: true})}, refresh = async () => {};
const draft = {source:'outside',origin:'folder',kind:'draft',id:'outside:1',version:'v1',title:'Renewal',ask:'Review this reply',act_where:'Outlook',ref:'draft-7'};
const kanban = {source:'kanban',id:'kanban:board:task',version:'v1',title:'Launch',ask:'Awaiting approval',tier:'ask'};
const q = {items:[kanban,draft],count:2,errors:[],at:Date.now()};
env.useValue = key => key === 'queue' ? q : false;
function page() { cursor = 0; return Page({ctx,refresh}); }
let tree = page();
all(tree).find(n => n?.type === 'input').props.onChange({target:{value:'OUTLOOK draft-7'}});
tree = page();
let cards = all(tree).filter(n => n?.type === Item);
assert.equal(cards.length,2,'filtering must retain mounted cards');
assert.equal(cards.find(n => n.key === kanban.id).props.hidden,true);
assert.equal(cards.find(n => n.key === draft.id).props.hidden,false);
assert.ok(all(tree).includes('1 of 2 cards shown · this box searches titles and previews of the cards loaded here'));
all(tree).find(n => n?.type === 'button' && n.props.children === 'Clear filters').props.onClick();
tree = page();
assert.equal(all(tree).filter(n => n?.type === Item && !n.props.hidden).length,2);
all(tree).find(n => n?.type === 'select').props.onChange({target:{value:'kanban'}});
assert.equal(all(page()).filter(n => n?.type === Item && !n.props.hidden).length,1);
state = []; cursor = 0;
const folder = Item({ctx,item:draft,n:1,refresh,askEnabled:false});
(async () => {
  await all(folder).find(n => n?.type === 'button' && n.props.children === 'Done — hides until the file changes').props.onClick();
  assert.equal(undo[0].id,draft.id);assert.equal(undo[0].kind,'done');
  state = []; cursor = 0;
  const card = Item({ctx,item:kanban,n:1,refresh,askEnabled:false});
  assert.ok(all(card).some(n => n?.type === 'button' && n.props.children === '+ FULL TEXT'),'unsummarized cards need a keyboard control');
})().catch(e => {console.error(e);process.exitCode=1});
"""
        self.run_views(script)
