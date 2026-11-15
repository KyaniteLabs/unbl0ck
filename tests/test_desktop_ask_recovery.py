"""Ask conversations survive card remounts like answer drafts, without a second model call."""
import subprocess
import unittest
from pathlib import Path

from test_operator_recovery import HARNESS

ROOT = Path(__file__).resolve().parents[1]

STORE = r"""
const store=new Function('env',`with(env){${views};return{Item,readAskSession,updateAskSession,clearAskSession,askSessions};}`)(env);
const texts=tree=>all(tree).filter(x=>typeof x==='string').join('|');
"""


class AskRecovery(unittest.TestCase):
    def run_views(self, script):
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                result = subprocess.run(["node", "-e", HARNESS + STORE + script, str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_session_store_is_bounded_expires_and_keeps_cards_separate(self):
        self.run_views(r"""
for(let i=0;i<105;i++)store.updateAskSession('card:'+i,{msgs:[{role:'user',content:'q'+i}],version:'v1'});
assert.equal(store.askSessions.size,100);
assert.equal(store.readAskSession('card:0'),undefined);
assert.equal(store.readAskSession('card:104').msgs[0].content,'q104');
store.askSessions.get('card:104').at=Date.now()-31*60*1000;
assert.equal(store.readAskSession('card:104'),undefined,'expired conversations are dropped');
store.clearAskSession('card:103');assert.equal(store.readAskSession('card:103'),undefined);
""")

    def test_conversation_survives_remount_without_a_second_model_call(self):
        self.run_views(r"""
let posts=0;
const ctx={rest:async(url,o)=>{if(url==='/card/ask'){posts++;return{ok:true,reply:'Pick Monday.'}}return{}}};
const it={...item,id:'kanban:default:ask1',version:'7'};
const render=()=>{cursor=0;return store.Item({ctx,item:it,n:1,refresh,askEnabled:true})};
(async()=>{
 render();
 await find(render(),'TL;DR').onClick();     // opens and asks for the bottom line
 assert.equal(posts,1);
 state=[];                                  // filtered out / source outage / route change
 const back=texts(render());
 assert.ok(back.includes('Pick Monday.'),'reply restored: '+back);
 assert.equal(find(render(),'Hide TL;DR').disabled,false);
 assert.equal(posts,1,'remount never asks the model again');
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_running_request_is_followed_after_remount_and_result_is_not_lost(self):
        self.run_views(r"""
let posts=0,release,effects=[];
const ctx={rest:async(url)=>{if(url==='/card/ask'){posts++;return{pending:true,job:'j1'}}
  return new Promise(r=>{release=()=>r({ok:true,reply:'Late but kept.'})})}};
env.useEffect=fn=>effects.push(fn);
const it={...item,id:'kanban:default:ask2',version:'3'};
const render=()=>{cursor=0;effects=[];return store.Item({ctx,item:it,n:1,refresh,askEnabled:true})};
const realSet=global.setTimeout;global.setTimeout=(fn)=>realSet(fn,0);
(async()=>{
 render();
 const first=find(render(),'TL;DR').onClick();
 await new Promise(r=>realSet(r,20));
 const s=store.readAskSession(it.id);
 assert.equal(s.running,true);assert.equal(s.job.id,'j1');assert.equal(s.job.version,'3');
 state=[];                                  // the card unmounted mid-request
 const tree=render();
 assert.ok(texts(tree).includes('Thinking'),'a running request shows as busy');
 assert.equal(posts,1);
 release();await new Promise(r=>realSet(r,1700));await first;
 const done=store.readAskSession(it.id);
 assert.equal(done.running,false);
 assert.equal(done.msgs[done.msgs.length-1].content,'Late but kept.');
 state=[];assert.ok(texts(render()).includes('Late but kept.'));
 assert.equal(posts,1);
 global.setTimeout=realSet;
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_changed_question_marks_recovered_conversation_stale(self):
        self.run_views(r"""
store.updateAskSession('kanban:default:ask3',{msgs:[{role:'user',content:'q'},{role:'assistant',content:'old advice'}],version:'1'});
const it={...item,id:'kanban:default:ask3',version:'2'};
state=[];cursor=0;
const t=texts(store.Item({ctx:{rest:async()=>({})},item:it,n:1,refresh,askEnabled:true}));
assert.ok(t.includes('This card changed'));
const use=all(store.Item({ctx:{rest:async()=>({})},item:it,n:1,refresh,askEnabled:true})).find(n=>n?.type==='button'&&n.props.children==='Use as answer');
assert.equal(use.props.disabled,true,'a stale suggestion cannot become the answer');
""")

    def test_old_response_cannot_overwrite_a_newer_request(self):
        self.run_views(r"""
const resolvers=[];
const ctx={rest:()=>new Promise(r=>resolvers.push(r))};
const it={...item,id:'kanban:default:ask4',version:'1'};
const render=()=>{cursor=0;return store.Item({ctx,item:it,n:1,refresh,askEnabled:true})};
(async()=>{
 render();
 const old=find(render(),'TL;DR').onClick();
 await new Promise(r=>setTimeout(r,5));
 // a newer request starts for the same card (new gen), as after a changed question
 store.updateAskSession(it.id,{msgs:[{role:'user',content:'newer'}],version:'1',running:true,gen:store.readAskSession(it.id).gen+1});
 resolvers[0]({ok:true,reply:'OLD ANSWER'});
 await old;
 const s=store.readAskSession(it.id);
 assert.equal(s.running,true,'the newer request still owns the session');
 assert.deepEqual(s.msgs.map(m=>m.content),['newer']);
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_failed_request_is_remembered_with_its_error_and_retryable(self):
        self.run_views(r"""
const ctx={rest:async()=>{throw new Error('model down')}};
const it={...item,id:'kanban:default:ask5',version:'1'};
const render=()=>{cursor=0;return store.Item({ctx,item:it,n:1,refresh,askEnabled:true})};
(async()=>{
 render();
 await find(render(),'TL;DR').onClick();
 state=[];
 const t=texts(render());
 assert.ok(t.includes('model down'));
 assert.equal(store.readAskSession(it.id).running,false);
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_reload_clears_and_profiles_stay_separate(self):
        self.run_views(r"""
store.updateAskSession('chat:work:s1',{msgs:[{role:'user',content:'work q'}],version:'1'});
store.updateAskSession('chat:home:s1',{msgs:[{role:'user',content:'home q'}],version:'1'});
assert.equal(store.readAskSession('chat:work:s1').msgs[0].content,'work q');
assert.equal(store.readAskSession('chat:home:s1').msgs[0].content,'home q');
// a page reload re-evaluates the module: a fresh instance starts empty
const reloaded=new Function('env',`with(env){${views};return{readAskSession};}`)(env);
assert.equal(reloaded.readAskSession('chat:work:s1'),undefined);
""")

    def _recovered(self, state, detail, extra=""):
        return r"""
let posts=0,urls=[];
const ctx={rest:async(url)=>{urls.push(url);if(url==='/card/ask'){posts++;return{pending:true,job:'j9'}}
  return{ok:false,state:'%s',detail:'%s'%s}}};
const it={...item,id:'kanban:default:ask9',version:'1'};
const render=()=>{cursor=0;return store.Item({ctx,item:it,n:1,refresh,askEnabled:true})};
const realSet=global.setTimeout;global.setTimeout=(fn)=>realSet(fn,0);
(async()=>{
 render();
 const first=find(render(),'TL;DR').onClick();
 await new Promise(r=>realSet(r,1800));await first;
 assert.ok(urls.some(u=>u.includes('poll=true')&&u.includes('card='+encodeURIComponent(it.id))),'poll names its card: '+urls);
 assert.equal(posts,1,'the first ask is the only POST');
 state=[];
 const t=texts(render());
 const s=store.readAskSession(it.id);
 assert.equal(s.running,false);assert.equal(s.job,null,'a recovered job is never followed again');
 CHECKS
 assert.equal(posts,1,'recovery never resends by itself');
 global.setTimeout=realSet;
})().catch(e=>{console.error(e);process.exitCode=1});
""" % (state, detail, extra)

    def test_unknown_outcome_after_restart_is_explicit_and_not_resent(self):
        script = self._recovered("unknown", "may or may not have completed", ",resubmit_safe:false").replace(
            "CHECKS", "assert.ok(t.includes('Outcome unknown. may or may not have completed Retry sends a new request.'),t);"
        )
        self.run_views(script)

    def test_not_submitted_after_restart_says_nothing_was_sent(self):
        script = self._recovered("not_submitted", "nothing was sent", ",resubmit_safe:true").replace(
            "CHECKS", "assert.ok(t.includes('Not sent. nothing was sent Retry sends it now.'),t);"
        )
        self.run_views(script)

    def test_nothing_is_persisted_outside_session_memory(self):
        for path in (ROOT / "desktop/src/plugin.src.js",):
            text = path.read_text()
            body = text[text.index("const ASK_SESSION_TTL"):text.index("function Item(")]
            for banned in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
                self.assertNotIn(banned, body)


if __name__ == "__main__":
    unittest.main()
