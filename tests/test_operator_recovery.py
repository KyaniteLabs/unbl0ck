"""Execute Desktop recovery workflows against source and generated views."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const fs = require('fs'), assert = require('node:assert/strict');
const src = fs.readFileSync(process.argv[1], 'utf8');
const views = src.slice(src.indexOf('function Item('), src.indexOf('function Chip('));
const node = (type, props, key) => ({type,props,key}), noop=()=>{};
let state=[],cursor=0,undo={id:'outside:1',title:'Draft',at:Date.now()}, refreshes=0, failures=0;
const env={
 jsx:node,jsxs:node,useState:init=>{const i=cursor++;if(!(i in state))state[i]=init;return[state[i],v=>{state[i]=typeof v==='function'?v(state[i]):v}]},
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
