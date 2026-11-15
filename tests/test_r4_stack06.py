"""Round 4, stack/06: Ask conversations in Desktop session memory are bounded, scoped and honest."""
import subprocess
import unittest
from pathlib import Path

from test_operator_recovery import HARNESS

ROOT = Path(__file__).resolve().parents[1]

STORE = r"""
const store=new Function('env',`with(env){${views};const opt=f=>{try{return f()}catch(e){return undefined}};return{Item,Page,readAskSession,updateAskSession,clearAskSession,askSessions,askKey:opt(()=>askKey),endAskScope:opt(()=>endAskScope),boundAsk:opt(()=>boundAsk)};}`)(env);
const texts=tree=>all(tree).filter(x=>typeof x==='string').join('|');
// Real effects and a controllable interval clock: an effect that is never run proves nothing.
let effects=[],cleanups=[],timers=[];
env.useEffect=fn=>effects.push(fn);
global.setInterval=(fn,ms)=>{timers.push({fn,ms,on:true});return timers.length-1};
global.clearInterval=i=>{if(timers[i])timers[i].on=false};
const mount=(ctx,it,scope)=>{cursor=0;effects=[];const t=store.Item({ctx,item:it,n:1,refresh,askEnabled:true,scope});
  cleanups.forEach(c=>typeof c==='function'&&c());cleanups=effects.map(f=>f());return t};
const tick=()=>timers.filter(t=>t.on).forEach(t=>t.fn());
const area=tree=>all(tree).find(n=>n?.type==='textarea'&&n.props['aria-label']==='Ask a follow-up').props;
"""


class Stack06Ask(unittest.TestCase):
    def run_views(self, script):
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                result = subprocess.run(
                    ["node", "-e", HARNESS + STORE + script, str(path)], capture_output=True, text=True, timeout=60
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    # 06-001: the 30-minute retention also covers a card that stays mounted.
    def test_mounted_conversation_is_dropped_when_its_session_expires(self):
        self.run_views(r"""
const ctx={rest:async()=>({ok:true,reply:'First advice.'})};
const it={...item,id:'kanban:default:m1',version:'1'};
(async()=>{
 mount(ctx,it);
 await find(mount(ctx,it),'TL;DR').onClick();
 assert.ok(texts(mount(ctx,it)).includes('First advice.'));
 store.askSessions.get(it.id).at=Date.now()-31*60*1000;   // 31 minutes without activity
 tick();                                                   // the mounted card's expiry check runs
 assert.ok(!texts(mount(ctx,it)).includes('First advice.'),'expired advice must leave the screen');
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_followup_after_expiry_never_resends_the_expired_history(self):
        self.run_views(r"""
const sent=[];
const ctx={rest:async(url,o)=>{if(url==='/card/ask'){sent.push(o.body.messages);return{ok:true,reply:'ok '+sent.length}}return{}}};
const it={...item,id:'kanban:default:m2',version:'1'};
(async()=>{
 mount(ctx,it);
 await find(mount(ctx,it),'TL;DR').onClick();
 store.askSessions.get(it.id).at=Date.now()-31*60*1000;   // expired; no timer fired yet
 area(mount(ctx,it)).onChange({target:{value:'second question'}});
 await find(mount(ctx,it),'Send').onClick();
 assert.equal(sent.length,2);
 assert.deepEqual(sent[1].map(m=>m.content),['second question'],'expired history is not sent again');
 assert.ok(!texts(mount(ctx,it)).includes('ok 1'));
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    # 06-002: one conversation is bounded in messages and characters, and says so.
    def test_one_conversation_is_bounded_in_messages_and_characters(self):
        self.run_views(r"""
const big='x'.repeat(20000);
let m=[];
for(let i=0;i<120;i++){m.push({role:'user',content:big});m.push({role:'assistant',content:big})}
const r=store.boundAsk(m);
assert.ok(r.msgs.length<=100,'message cap: '+r.msgs.length);
const chars=r.msgs.reduce((n,x)=>n+x.content.length,0);
assert.ok(chars<=200000,'character cap: '+chars);
assert.equal(r.msgs[0].role,'user');
assert.ok(r.dropped>0);
assert.equal(store.boundAsk([{role:'user',content:'a'}]).dropped,0);
""")

    def test_long_follow_up_run_keeps_a_bounded_history_and_shows_a_notice(self):
        self.run_views(r"""
const ctx={rest:async(url)=>url==='/card/ask'?{ok:true,reply:'y'.repeat(20000)}:{}};
const it={...item,id:'kanban:default:m3',version:'1'};
(async()=>{
 mount(ctx,it);
 await find(mount(ctx,it),'TL;DR').onClick();
 for(let i=0;i<30;i++){
  area(mount(ctx,it)).onChange({target:{value:'q'+i+'z'.repeat(19990)}});
  await find(mount(ctx,it),'Send').onClick();
 }
 const s=store.readAskSession(it.id);
 const chars=s.msgs.reduce((n,x)=>n+x.content.length,0);
 assert.ok(chars<=200000,'stored characters '+chars);
 assert.ok(s.msgs.length<=100);
 assert.equal(s.trimmed,true);
 assert.ok(texts(mount(ctx,it)).includes('Earlier messages in this conversation were dropped'));
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    # 06-003: an outstanding request whose entry is evicted reports the lost work.
    def test_evicted_outstanding_request_shows_an_error_and_retry(self):
        self.run_views(r"""
let release;
const ctx={rest:(url)=>url==='/card/ask'?new Promise(r=>{release=r}):Promise.resolve({})};
const it={...item,id:'kanban:default:e1',version:'1'};
(async()=>{
 mount(ctx,it);
 const first=find(mount(ctx,it),'TL;DR').onClick();
 await new Promise(r=>setTimeout(r,5));
 for(let i=0;i<100;i++)store.updateAskSession('other:'+i,{msgs:[{role:'user',content:'x'}],version:'1'});
 assert.equal(store.readAskSession(it.id),undefined,'the running entry was evicted');
 release({ok:true,reply:'LATE'});
 await first;
 const t=texts(mount(ctx,it));
 assert.ok(t.includes('dropped from session memory'),'the loss is reported: '+t);
 find(mount(ctx,it),'Retry');
 assert.ok(!t.includes('Thinking'));
 assert.equal(store.readAskSession(it.id).running,false);
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_remounted_observer_reports_an_evicted_request(self):
        self.run_views(r"""
const ctx={rest:()=>new Promise(()=>{})};
const it={...item,id:'kanban:default:e2',version:'1'};
(async()=>{
 mount(ctx,it);
 find(mount(ctx,it),'TL;DR').onClick();
 await new Promise(r=>setTimeout(r,5));
 state=[];                       // card unmounted and remounted: the observer follows the session
 mount(ctx,it);
 store.clearAskSession(it.id);   // evicted while the request is outstanding
 tick();
 const t=texts(mount(ctx,it));
 assert.ok(t.includes('dropped from session memory'),t);
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    # 06-004: conversations are scoped to the backend connection, not just the card id.
    def test_same_card_id_on_another_backend_never_shows_or_resends_the_conversation(self):
        self.run_views(r"""
const sentB=[];
const ctxA={rest:async()=>({ok:true,reply:'PRIVATE ADVICE FROM A'})};
const ctxB={rest:async(url,o)=>{if(url==='/card/ask'){sentB.push(o.body.messages);return{ok:true,reply:'B ok'}}return{}}};
const it={...item,id:'kanban:default:t',version:'1'};
(async()=>{
 mount(ctxA,it,'c1');
 await find(mount(ctxA,it,'c1'),'TL;DR').onClick();
 assert.ok(texts(mount(ctxA,it,'c1')).includes('PRIVATE ADVICE FROM A'));
 state=[];
 const onB=texts(mount(ctxB,it,'c2'));
 assert.ok(!onB.includes('PRIVATE ADVICE FROM A'),'backend B must not see backend A conversation');
 assert.equal(store.readAskSession(store.askKey('c2',it.id)),undefined);
 // disposing a connection removes its conversations
 store.endAskScope('c1');
 assert.equal(store.readAskSession(store.askKey('c1',it.id)),undefined);
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    # Sweep (class F): answer drafts had the same card-id-only key as Ask conversations.
    def test_answer_draft_on_one_backend_never_appears_on_another(self):
        self.run_views(r"""
const ctx={rest:async()=>({ok:true})};
const it={...item,id:'kanban:default:d1',version:'1'};
const box=(scope)=>all(mount(ctx,it,scope)).find(n=>n?.type==='textarea').props;
box('c1').onChange({target:{value:'Monday, private to backend A'}});
state=[];
assert.equal(box('c1').value,'Monday, private to backend A','same connection restores it');
state=[];
assert.equal(box('c2').value,'','another backend never sees it');
store.endAskScope('c1');
state=[];
assert.equal(box('c1').value,'','closing the connection drops its drafts');
""")

    def test_late_reply_after_the_connection_is_disposed_is_not_recreated(self):
        self.run_views(r"""
let release;
const ctx={rest:(url)=>url==='/card/ask'?new Promise(r=>{release=r}):Promise.resolve({})};
const it={...item,id:'kanban:default:t2',version:'1'};
(async()=>{
 mount(ctx,it,'c9');
 const first=find(mount(ctx,it,'c9'),'TL;DR').onClick();
 await new Promise(r=>setTimeout(r,5));
 store.endAskScope('c9');
 release({ok:true,reply:'LATE'});
 await first;
 assert.equal(store.readAskSession(store.askKey('c9',it.id)),undefined,'a disposed connection keeps nothing');
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    # 06-005: an unknown job is not necessarily a restart.
    def test_unknown_job_message_does_not_claim_a_restart_alone(self):
        self.run_views(r"""
const err=Object.assign(new Error('unknown or expired ask job'),{status:404});
let n=0;
const ctx={rest:async(url)=>{if(url==='/card/ask')return{pending:true,job:'j1'};n++;throw err}};
const it={...item,id:'kanban:default:u1',version:'1'};
const realSet=global.setTimeout;global.setTimeout=(fn)=>realSet(fn,0);
(async()=>{
 mount(ctx,it);
 find(mount(ctx,it),'TL;DR').onClick();
 await new Promise(r=>realSet(r,50));
 const t=texts(mount(ctx,it));
 assert.ok(t.includes('restarted or the result expired'),t);
 assert.ok(!/gateway restarted while/.test(t));
 global.setTimeout=realSet;
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    # 01-047 (Ask advice half): the card text can change without a new block event.
    def test_edited_card_text_marks_recovered_advice_stale_without_a_new_version(self):
        self.run_views(r"""
const ctx={rest:async()=>({ok:true,reply:'Old advice.'})};
const it={...item,id:'kanban:default:f1',version:'4',title:'Launch',comments_sig:'c1'};
(async()=>{
 mount(ctx,it);
 await find(mount(ctx,it),'TL;DR').onClick();
 state=[];
 let t=mount(ctx,it);
 assert.equal(all(t).find(n=>n?.type==='button'&&n.props.children==='Use as answer').props.disabled,false);
 state=[];
 for(const edited of [{title:'Launch (renamed)'},{ask:'A different question?'},{comments_sig:'c2'}]){
  const changed={...it,...edited};
  state=[];t=mount(ctx,changed);
  assert.ok(texts(t).includes('This card changed'),JSON.stringify(edited));
  assert.equal(all(t).find(n=>n?.type==='button'&&n.props.children==='Use as answer').props.disabled,true);
 }
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    # 06-006: lifecycle coverage that really runs the recovery effect and Retry.
    def test_recovery_effect_follows_a_running_request_and_cleans_up(self):
        self.run_views(r"""
const ctx={rest:()=>new Promise(()=>{})};
const it={...item,id:'kanban:default:l1',version:'1'};
(async()=>{
 mount(ctx,it);
 find(mount(ctx,it),'TL;DR').onClick();
 await new Promise(r=>setTimeout(r,5));
 state=[];
 const t=mount(ctx,it);
 assert.ok(texts(t).includes('Thinking'));
 assert.ok(timers.some(x=>x.on),'the remounted card subscribes to the running session');
 // the other instance finishes and stores its result
 store.updateAskSession(it.id,{msgs:[{role:'user',content:'q'},{role:'assistant',content:'DONE LATER'}],running:false});
 tick();
 assert.ok(texts(mount(ctx,it)).includes('DONE LATER'),'the subscription restores the finished result');
 timers.length=0;
 mount(ctx,it);                          // a finished conversation subscribes to nothing
 assert.ok(!timers.some(x=>x.on&&x.ms===500));
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    # R5-desk-022 (class L): the subscription must stop when the card unmounts while work is pending.
    def test_unmounting_while_a_request_is_pending_stops_the_recovery_and_expiry_timers(self):
        self.run_views(r"""
const ctx={rest:()=>new Promise(()=>{})};
const it={...item,id:'kanban:default:l3',version:'1'};
(async()=>{
 mount(ctx,it);
 find(mount(ctx,it),'TL;DR').onClick();
 await new Promise(r=>setTimeout(r,5));
 state=[];
 timers.length=0;
 mount(ctx,it);                                       // remounted with the request still running
 assert.ok(timers.some(x=>x.on&&x.ms===500),'the recovery timer is running while work is pending');
 cleanups.forEach(c=>typeof c==='function'&&c());     // the card unmounts; the request is still pending
 assert.ok(store.readAskSession(it.id).running,'the request is still pending');
 assert.ok(!timers.some(x=>x.on&&x.ms===500),'the recovery timer must be cleared on unmount');
 // a settled conversation shows an expiry check; it must also be cleared on unmount
 store.updateAskSession(it.id,{msgs:[{role:'user',content:'q'},{role:'assistant',content:'done'}],running:false});
 state=[];timers.length=0;
 mount(ctx,it);
 assert.ok(timers.some(x=>x.on&&x.ms===15000),'the expiry check is running for a settled conversation');
 cleanups.forEach(c=>typeof c==='function'&&c());
 assert.ok(!timers.some(x=>x.on&&x.ms===15000),'the expiry check must be cleared on unmount');
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_retry_button_restores_a_failed_ask(self):
        self.run_views(r"""
let calls=0;
const ctx={rest:async(url)=>{if(url!=='/card/ask')return{};calls++;if(calls===1)throw new Error('model down');return{ok:true,reply:'Recovered answer.'}}};
const it={...item,id:'kanban:default:l2',version:'1'};
(async()=>{
 mount(ctx,it);
 await find(mount(ctx,it),'TL;DR').onClick();
 assert.ok(texts(mount(ctx,it)).includes('model down'));
 state=[];                                   // remount: the failed request is remembered
 await find(mount(ctx,it),'Retry').onClick();
 assert.equal(calls,2);
 const t=texts(mount(ctx,it));
 assert.ok(t.includes('Recovered answer.'));
 assert.ok(!t.includes('model down'));
})().catch(e=>{console.error(e);process.exitCode=1});
""")


class Stack06Docs(unittest.TestCase):
    def test_docs_state_the_bounds_scope_and_expiry(self):
        guide = (ROOT / "docs/operator-guide.md").read_text()
        text = " ".join(guide.split())
        for needle in (
            "at most 100 messages and 200,000 characters",
            "dropped from session memory",
            "each backend connection",
            "title, question, owner, description or comments",
            "a draft typed for one backend never appears on another",
        ):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
