"""Desktop keeps a bounded history of local dismissals, each undoable on its own."""
import subprocess
import unittest
from pathlib import Path

from test_operator_recovery import HARNESS

ROOT = Path(__file__).resolve().parents[1]


class UndoHistory(unittest.TestCase):
    def run_views(self, script):
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                result = subprocess.run(["node", "-e", HARNESS + script, str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_consecutive_dismissals_are_all_kept_newest_first_and_bounded(self):
        self.run_views(r"""
undo=[];
const calls=[];
const ctx={rest:async(url,o)=>{calls.push(url);return url==='/hide'?{ok:true}:{ok:true,done:true,local_only:true}}};
const mk=(n,origin)=>({source:'outside',origin,kind:'draft',id:'outside:filed:'+n,version:'v'+n,title:'T'+n,ask:'a',act_where:'w'});
(async()=>{
 for(let n=1;n<=7;n++){
  cursor=0;state=[];
  const it=Item({ctx,item:mk(n,'filed'),n:1,refresh,askEnabled:false});
  await find(it,'Done').onClick();
 }
 assert.equal(undo.length,5,'history is bounded');
 assert.equal(undo[0].id,'outside:filed:7');
 assert.ok(undo.every(u=>u.kind==='done'));
 // dismissing the same item again does not duplicate its entry
 cursor=0;state=[];
 await find(Item({ctx,item:mk(7,'filed'),n:1,refresh,askEnabled:false}),'Done').onClick();
 assert.equal(undo.length,5);
 // each entry is separately undoable and a restore removes only that entry
 const page=()=>{cursor=0;state=[];return Page({ctx,refresh})};
 const buttons=all(page()).filter(n=>n?.type==='button'&&n.props.children==='Undo');
 assert.equal(buttons.length,5);
 await buttons[2].props.onClick();
 assert.equal(undo.length,4);
 assert.ok(!undo.some(u=>u.id==='outside:filed:5'));
})().catch(e=>{console.error(e);process.exitCode=1});
""")

    def test_late_undo_response_leaves_a_newer_dismissal_of_the_same_item_alone(self):
        for outcome in ("ok", "fail"):
            with self.subTest(outcome=outcome):
                self.run_views(r"""
const outcome=%r;
const old={id:'outside:filed:1',version:'v1',title:'Old',kind:'done',at:Date.now()};
undo=[old];
let release;
const ctx={rest:()=>new Promise((res,rej)=>{release=()=>outcome==='ok'?res({restored:true}):rej(new Error('net'))})};
(async()=>{
 cursor=0;state=[];
 const pending=all(Page({ctx,refresh})).find(n=>n?.type==='button'&&n.props.children==='Undo').props.onClick();
 // the item resurfaces and is dismissed again while the first request is still pending
 const newer={...old,version:'v2',title:'New',at:old.at+5000};
 undo=[newer];
 release();
 await pending;
 assert.equal(undo.length,1,'newer dismissal survives');
 assert.equal(undo[0].title,'New');
 assert.equal(undo[0].version,'v2');
 assert.ok(!undo[0].failed,'not marked failed by the older request');
})().catch(e=>{console.error(e);process.exitCode=1});
""" % outcome)

    def test_local_done_says_it_changes_nothing_in_the_original_app(self):
        self.run_views(r"""
const ctx={rest:async()=>({ok:true})};
const filed=Item({ctx,item:{source:'outside',origin:'filed',kind:'draft',id:'outside:filed:1',version:'v',title:'T',ask:'a',act_where:'Mail'},n:1,refresh,askEnabled:false});
const t=all(filed).filter(x=>typeof x==='string').join('');
assert.ok(/does not send, file or finish anything/.test(t));
const folder=Item({ctx,item:{source:'outside',origin:'folder',kind:'notes',id:'outside:folder:'+'a'.repeat(64),version:'v',title:'T',ask:'a',act_where:'Notes'},n:1,refresh,askEnabled:false});
assert.ok(/not changed, moved or deleted/.test(all(folder).filter(x=>typeof x==='string').join('')));
""")

    def test_expired_entry_is_announced_and_removed(self):
        self.run_views(r"""
const origTimeout=global.setTimeout;
let fire;
global.setTimeout=(fn)=>{fire=fn;return 1};
env.useEffect=(fn)=>{fn()};
undo=[{id:'outside:filed:9',title:'Old',at:Date.now()-UNDO_MS_PLACEHOLDER-1,kind:'done'}];
""".replace("UNDO_MS_PLACEHOLDER", "13000") + r"""
const ctx={rest:async()=>({ok:true})};
cursor=0;state=[];
Page({ctx,refresh});
assert.ok(fire,'an expiry timer is scheduled');
fire();
assert.equal(undo.length,0);
cursor=0;
const text=all(Page({ctx,refresh})).filter(x=>typeof x==='string').join('');
assert.ok(text.includes('Undo window ended: Old'));
global.setTimeout=origTimeout;
""")


if __name__ == "__main__":
    unittest.main()
