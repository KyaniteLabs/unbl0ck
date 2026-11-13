"""Execute Desktop recovery workflows against source and generated views."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const fs = require('fs'), assert = require('node:assert/strict');
const src = fs.readFileSync(process.argv[1], 'utf8');
const views = src.slice(src.indexOf('const ANSWER_DRAFT_TTL'), src.indexOf('function Chip('));
const node = (type, props, key) => ({type,props,key}), noop=()=>{};
let state=[],cursor=0,undo={id:'outside:1',title:'Draft',at:Date.now()}, refreshes=0, failures=0;
const env={
 jsx:node,jsxs:node,useState:init=>{const i=cursor++,current=state;if(!(i in current))current[i]=init;return[current[i],v=>{current[i]=typeof v==='function'?v(current[i]):v}]},
 useEffect:noop,useRef:()=>({}),platformLabel:x=>x,LOCAL_PLATFORMS:[],KEEP_BLOCKED:/keep blocked/i,
 button:props=>node('button',props),openBoard:noop,ago:()=>'',ageClass:()=>'',pad:String,
 sfx:{play:noop},petEmit:noop,host:{notify:noop,notifyError:()=>{failures++}},
 $undo:{get:()=>undo,set:v=>{undo=v}},$queue:'queue',$fxOn:'fx',UNDO_MS:12000,askCount:q=>q.count,
 Field:noop,Watermark:noop,Banner:noop,Pet:noop,Ticker:noop,FxToggle:noop,SfxToggle:noop,CardDetail:noop,
 grainUrl:()=>'',oldestAskSince:()=>0,reducedMotion:()=>true,TLDR_ASK:'Summarize',replyPlain:x=>x,replyParts:x=>x,
};
env.useValue=key=>key==='queue'?{items:[],count:0,errors:[],at:Date.now()}:key===env.$undo?undo:false;
const {Item,Page}=new Function('env',`with(env){${views};return{Item,Page};}`)(env);
function all(n){if(n==null)return[];if(Array.isArray(n))return n.flatMap(all);if(typeof n!=='object')return[n];return[n,...all(n.props?.children)]}
const find=(tree,label)=>{const n=all(tree).find(n=>n?.type==='button'&&n.props.children===label);assert.ok(n,'missing button '+label);return n.props};
const item={source:'kanban',id:'kanban:default:task',board:'default',task_id:'task',version:'1',title:'Launch',ask:'Which day?',tier:'ask'};
const refresh=async()=>{refreshes++};
"""

class OperatorRecovery(unittest.TestCase):
    def run_views(self, script):
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                result = subprocess.run(["node", "-e", HARNESS + script, str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_undo_failure_is_retryable_and_pending_cannot_double_submit(self):
        self.run_views(r"""
let reject, calls=0;
const ctx={rest:()=>{calls++;return calls===1?new Promise((_,r)=>{reject=r}):Promise.resolve({ok:true})}};
const render=()=>{cursor=0;return Page({ctx,refresh})};
(async()=>{
 const first=find(render(),'Undo').onClick();
 assert.equal(find(render(),'Undo').disabled,true);
 await find(render(),'Undo').onClick();assert.equal(calls,1);
 reject(new Error('temporary outage'));await first;
 assert.ok(undo,'failed restoration must preserve retry');
 assert.equal(find(render(),'Undo').disabled,false);
 await find(render(),'Undo').onClick();assert.equal(undo,null);assert.equal(calls,2);
 assert.equal(failures,1);assert.equal(refreshes,1);
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_partial_answer_failure_refreshes_recovery_actions(self):
        self.run_views(r"""
const ctx={rest:async()=>{throw new Error('answer posted but release failed')}};
const render=()=>{cursor=0;return Item({ctx,item,n:1,refresh,askEnabled:false})};
all(render()).find(n=>n?.type==='textarea').props.onChange({target:{value:'Monday'}});
(async()=>{await find(render(),'Answer + unbl0ck').onClick();assert.equal(failures,1);assert.equal(refreshes,1)})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_source_fault_remount_preserves_answer_and_original_question_version(self):
        self.run_views(r"""
const ctx={rest:async()=>({ok:true})};
const render=()=>{cursor=0;return Item({ctx,item,n:1,refresh,askEnabled:false})};
const input=()=>all(render()).find(n=>n?.type==='textarea').props;
input().onChange({target:{value:'Monday, after the launch review'}});
state=[]; // source failed and the card unmounted
assert.equal(input().value,'Monday, after the launch review');
state=[];item.version='2'; // it returned with a new blocking question
assert.equal(input().value,'Monday, after the launch review');
assert.equal(find(render(),'Answer + unbl0ck').disabled,true);
input().onChange({target:{value:''}});
input().onChange({target:{value:'New answer'}});
assert.equal(find(render(),'Answer + unbl0ck').disabled,false);
(async()=>{await find(render(),'Answer + unbl0ck').onClick();state=[];assert.equal(input().value,'')})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_answer_draft_cache_is_bounded_expires_and_keeps_cards_separate(self):
        self.run_views(r"""
const store=new Function('env',`with(env){${views};return{readAnswerDraft,rememberAnswerDraft,answerDrafts};}`)(env);
for(let i=0;i<105;i++)store.rememberAnswerDraft('card:'+i,'answer '+i,'v1');
assert.equal(store.answerDrafts.size,100);assert.equal(store.readAnswerDraft('card:0'),undefined);
assert.equal(store.readAnswerDraft('card:104').text,'answer 104');
store.answerDrafts.get('card:104').at=Date.now()-31*60*1000;
assert.equal(store.readAnswerDraft('card:104'),undefined);
store.rememberAnswerDraft('card:103','','v1');assert.equal(store.readAnswerDraft('card:103'),undefined);
""")

    def test_late_old_answer_completion_cannot_erase_a_new_remounted_draft(self):
        self.run_views(r"""
let resolve;
const ctx={rest:()=>new Promise(r=>{resolve=r})};
const render=()=>{cursor=0;return Item({ctx,item,n:1,refresh,askEnabled:false})};
const input=()=>all(render()).find(n=>n?.type==='textarea').props;
(async()=>{
 input().onChange({target:{value:'Submitted answer A'}});
 const pending=find(render(),'Answer + unbl0ck').onClick();
 state=[]; // the source failed and recovered during the old request
 input().onChange({target:{value:'New unsent answer B'}});
 resolve({ok:true,released:true});await pending;
 assert.equal(input().value,'New unsent answer B');
 state=[];
 assert.equal(input().value,'New unsent answer B','late old success must preserve newer cached draft');
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_long_ask_conversation_bounds_request_without_dropping_display(self):
        self.run_views(r"""
const sizes=[];
const ctx={rest:async(path,options)=>path==='/card/ask'?(sizes.push(options.body.messages.length),{reply:'Monday'}):{}};
const render=()=>{cursor=0;return Item({ctx,item,n:1,refresh,askEnabled:true})};
(async()=>{
 find(render(),'TL;DR').onClick();await new Promise(r=>setImmediate(r));
 for(let i=0;i<30;i++){
  all(render()).find(n=>n?.type==='textarea'&&n.props['aria-label']==='Ask a follow-up').props.onChange({target:{value:'Follow up '+i}});
  await find(render(),'Send').onClick();
 }
 assert.equal(sizes.length,31);assert.ok(Math.max(...sizes)<=50,'history must fit the API bound');
 assert.equal(sizes[20],41,'configured history above 20 must remain available to the server');
 assert.ok(all(render()).filter(n=>n?.props?.className==='ub-msg assistant').length===31,'visible conversation must remain intact');
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_transient_poll_failure_retries_the_same_job_without_another_model_call(self):
        self.run_views(r"""
let posts=0,polls=0;
env.setTimeout=fn=>{fn();return 1};
const ctx={rest:async path=>{
 if(path==='/card/ask'){posts++;return{pending:true,job:'saved'}}
 assert.equal(path,'/card/ask/saved?poll=true');
 if(++polls===1)throw new Error('temporary transport failure');
 return{ok:true,reply:'Recovered answer'};
}};
const render=()=>{cursor=0;return Item({ctx,item,n:1,refresh,askEnabled:true})};
(async()=>{
 find(render(),'TL;DR').onClick();await new Promise(r=>setImmediate(r));
 await find(render(),'Retry').onClick();
 assert.equal(posts,1);assert.equal(polls,2);
 assert.ok(all(render()).some(n=>n?.props?.className==='ub-msg assistant'));
})().catch(e=>{console.error(e);process.exitCode=1});
""")
