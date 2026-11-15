"""Round 5, lane D: the desktop card and page, one test per finding.

Each test drives the real Item or Page component (source and built) through the shared view
harness, so it fails on the old code for the behaviour the finding names."""
import subprocess
import unittest
from pathlib import Path

from test_operator_recovery import HARNESS
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


if __name__ == "__main__":
    unittest.main()
