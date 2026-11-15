"""Round 5, lane D: the desktop card and page, one test per finding.

Each test drives the real Item or Page component (source and built) through the shared view
harness, so it fails on the old code for the behaviour the finding names."""
import re
import subprocess
import unittest
from pathlib import Path

from test_operator_recovery import HARNESS
from test_outside import needs
from test_r4_stack06 import STORE

ROOT = Path(__file__).resolve().parents[1]


class NodeCase(unittest.TestCase):
    def run_views(self, script):
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                result = subprocess.run(
                    ["node", "-e", HARNESS + STORE + script, str(path)], capture_output=True, text=True, timeout=60
                )
                self.assertEqual(result.returncode, 0, result.stderr)


# Render the real Page for a given queue value.
PAGE = r"""
function page(q){
 const src2=fs.readFileSync(process.argv[1],'utf8');
 const v2=src2.slice(src2.indexOf('const ANSWER_DRAFT_TTL'),src2.indexOf('function Chip('));
 const env2={...env,useValue:k=>k==='queue'?q:k===env.$undo?[]:false};
 const P=new Function('env',`with(env){${v2};return Page}`)(env2);
 cursor=0;state=[];
 return texts(P({ctx:{rest:async()=>({})},refresh:async()=>{}}));
}
"""


class QueueVerdict(NodeCase):
    def test_a_queue_with_unread_chat_sessions_is_never_called_clear(self):
        """R5-desk-011: unread chat sessions make the subtitle and the banner agree that the list may be incomplete."""
        self.run_views(PAGE + r"""
const base={count:0,errors:[],at:Date.now(),coverage:{chat:{sessions_omitted:2}}};
let s=page({...base,items:[]});
assert.ok(s.includes('NOT ALL CLEAR'),s);
assert.ok(!s.includes('Nothing is blocked on you.'),'the subtitle must not call an incomplete list clear: '+s);
const ask={source:'kanban',id:'kanban:default:v1',version:'1',title:'T',ask:'Q?',tier:'ask',since:1};
s=page({...base,items:[ask],count:1});
assert.ok(s.includes('NOT ALL CLEAR'),'asks on the list do not prove the unread sessions hold none: '+s);
""")


class CardStamp(NodeCase):
    def test_an_answer_draft_goes_stale_when_the_card_text_changes_in_place(self):
        """R5-desk-004: a title, owner, description or comment edit keeps the block version; the draft must still go stale."""
        self.run_views(r"""
const sent=[];
const ctx={rest:async(url,o)=>{if(url==='/kanban/answer')sent.push(o.body);return{released:true}}};
const it={...item,id:'kanban:default:a4',version:'1',title:'Ship the launch',ask:'Which day?',comments_sig:'1:a'};
(async()=>{
 mount(ctx,it);
 all(mount(ctx,it)).find(n=>n?.type==='textarea').props.onChange({target:{value:'Monday'}});
 for(const edit of [{title:'Cancel the launch'},{owner:'someone else'},{comments_sig:'2:b'},{body_sig:'b2'}]){
  const t=mount(ctx,{...it,...edit});
  assert.ok(texts(t).includes('older version'),'no stale hint after '+JSON.stringify(edit));
  await find(t,'Answer + unbl0ck').onClick();
 }
 assert.deepEqual(sent,[],'a draft written for other card text must not be posted');
})().catch(e=>{console.error(e);process.exitCode=1});
""")


class AskBudget(NodeCase):
    def test_every_message_sent_is_within_the_limits_and_starts_with_a_user_turn(self):
        """R5-desk-009: stored replies are cut to the per-message limit and the 50-message window opens on a user turn."""
        self.run_views(r"""
const posts=[];
const ctx={rest:async(url,o)=>{if(url!=='/card/ask')return{};posts.push(o.body.messages);
  return{ok:true,reply:posts.length===1?'z'.repeat(30000):'Reply '+posts.length+'.'}}};
const it={...item,id:'kanban:default:bd1',version:'1'};
(async()=>{
 mount(ctx,it);
 await find(mount(ctx,it),'TL;DR').onClick();
 for(let i=0;i<25;i++){
  area(mount(ctx,it)).onChange({target:{value:'question '+i}});
  await find(mount(ctx,it),'Send').onClick();
 }
 assert.equal(posts.length,26,'every follow-up was sent');
 for(const sent of posts){
  assert.ok(sent.length<=50,'transport cap: '+sent.length);
  assert.equal(sent[0].role,'user','a request must open on a user turn, not '+sent[0].role);
  for(const m of sent)assert.ok(Array.from(m.content).length<=20000,'over the per-message limit: '+m.role+' '+m.content.length);
 }
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_a_poll_that_hangs_past_the_limit_fails_at_the_limit(self):
        """R5-desk-010: each poll is raced against what is left of the 150 s budget, not checked only after it returns."""
        self.run_views(r"""
let now=1e12;const t0=now;Date.now=()=>now;
const realSet=global.setTimeout;global.setTimeout=fn=>realSet(fn,0);
let polls=0;
const ctx={rest:url=>{
 if(url==='/card/ask')return Promise.resolve({ok:true,pending:true,job:'j1'});
 if(url.startsWith('/card/ask/')){polls++;return new Promise(()=>{})}
 return Promise.resolve({})}};
const it={...item,id:'kanban:default:dl1',version:'1'};
(async()=>{
 mount(ctx,it);
 const asked=find(mount(ctx,it),'TL;DR').onClick();
 await new Promise(r=>realSet(r,20));
 assert.equal(polls,1,'the poll is in flight');
 now=t0+150001;tick();
 await Promise.race([asked,new Promise(r=>realSet(r,200))]);
 const s=store.readAskSession(it.id);
 assert.equal(s.running,false,'the ask is still waiting on a poll past the limit');
 assert.match(s.error,/over 150 s/);
 assert.equal(s.job,'j1','the job is kept, so Retry follows it instead of asking again');
})().catch(e=>{console.error(e);process.exitCode=1});
""")


class StrongerTests(NodeCase):
    def test_a_terminal_reply_that_settles_after_the_limit_is_refused(self):
        """R5-desk-020: the first response or a poll that answers after 150 s is refused, with no other guard to hit first."""
        self.run_views(r"""
let now=1e12;Date.now=()=>now;
const realSet=global.setTimeout;global.setTimeout=fn=>realSet(fn,0);
async function run(id,rest){
 state=[];const it={...item,id:'kanban:default:'+id,version:'1'};
 mount({rest},it);await find(mount({rest},it),'TL;DR').onClick();
 return store.readAskSession(it.id);
}
(async()=>{
 const t0=now;
 let s=await run('tl1',async url=>{if(url==='/card/ask'){now=t0+151000;return{ok:true,reply:'late first'}}return{}});
 assert.ok(!s.msgs.some(m=>m.content==='late first'),'a late first response is not accepted');
 assert.match(s.error,/over 150 s/);
 const t1=now;
 s=await run('tl2',async url=>{
  if(url==='/card/ask')return{ok:true,pending:true,job:'j'};
  if(url.startsWith('/card/ask/')){now=t1+151000;return{ok:true,reply:'late poll'}}
  return{}});
 assert.ok(!s.msgs.some(m=>m.content==='late poll'),'a late terminal poll is not accepted');
 assert.match(s.error,/over 150 s/);
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_not_for_me_keeps_the_token_the_server_returned(self):
        """R5-desk-021: Undo of a Kanban "Not for me" carries the hide token from the response, not a local value."""
        self.run_views(r"""
const ctx={rest:async url=>url==='/hide'?{token:'tok-from-server',order:7}:{}};
const it={...item,id:'kanban:default:nf1',version:'4'};
(async()=>{
 undo=[];
 await find(mount(ctx,it),'Not for me').onClick();
 assert.equal(undo.length,1,JSON.stringify(undo));
 assert.equal(undo[0].token,'tok-from-server');
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_unmounting_while_an_ask_runs_stops_the_card_observer(self):
        """R5-desk-022: the card's 500 ms observer is cleared on unmount while the request is still pending."""
        self.run_views(r"""
const ctx={rest:url=>url==='/card/ask'?new Promise(()=>{}):Promise.resolve({})};
const it={...item,id:'kanban:default:um1',version:'1'};
(async()=>{
 mount(ctx,it);
 find(mount(ctx,it),'TL;DR').onClick();
 await new Promise(r=>setTimeout(r,5));
 mount(ctx,it);                                   // remounted while the request runs: the observer starts
 const observers=()=>timers.filter(t=>t.on&&t.ms===500);
 assert.equal(observers().length,1,'the observer runs while the ask is pending');
 assert.ok(store.readAskSession(it.id).running,'the request is still pending');
 cleanups.forEach(c=>typeof c==='function'&&c());cleanups=[];   // unmount
 assert.equal(observers().length,0,'an unmounted card must not keep its observer running');
})().catch(e=>{console.error(e);process.exitCode=1});
""")


class FullTextOffer(NodeCase):
    def test_password_prompts_and_pairing_requests_offer_no_full_text(self):
        """R5-desk-026: the guide says password prompts never offer Full text and pairing requests show neither."""
        self.run_views(r"""
const ctx={rest:async()=>({})};
const full=t=>all(t).some(n=>n?.type==='button'&&String(n.props.children).includes('FULL TEXT'));
const sudo={source:'prompt',kind:'sudo',id:'prompt:s1',version:'1',title:'sudo',ask:'[sudo] password for the operator:',since:1};
const pair={source:'pairing',id:'pairing:telegram:p1',version:'1',title:'Pairing request',ask:'A person asks to pair',platform:'telegram',since:1};
const chat={source:'chat',id:'chat:c1',version:'1',title:'Chat',ask:'Short question?',platform:'cli',since:1};
for(const it of [sudo,pair]){state=[];assert.ok(!full(mount(ctx,it)),it.source+' must not offer Full text')}
state=[];assert.ok(full(mount(ctx,chat)),'a chat still unfolds its text');
""")


def pack_choices(item):
    """The Pocket choices for one Kanban card, with the task named as the guide names it."""
    pack = needs.pocket_pack({"items": [item], "errors": []})
    where = re.escape(item["task_id"]) + r" on board \S+ \(block [^)]*\)"
    return [re.sub(where, "<task>", o["send"]) for o in pack["cards"][0]["options"]]


class PocketRowMatchesPack(unittest.TestCase):
    def test_the_guide_names_every_kanban_pocket_choice(self):
        """R5-desk-027: the surface matrix and the Pocket bullet name the choices pocket_pack gives a Kanban card."""
        text = (ROOT / "docs/operator-guide.md").read_text()
        row = next(l for l in text.splitlines() if l.startswith("| Pocket pack"))
        flat = " ".join(text.split())
        base = {"source": "kanban", "id": "kanban:default:t1", "task_id": "t1", "board": "default", "version": "7", "title": "T",
                "ask": "Q?", "why": "blocked", "profile": "default", "tier": "ask", "since": 1}
        for status in ("blocked", "triage"):
            for choice in pack_choices({**base, "status": status}):
                self.assertIn(choice, row, status)
                self.assertIn(choice, flat, status)
        self.assertNotIn("inline action", row)


if __name__ == "__main__":
    unittest.main()
