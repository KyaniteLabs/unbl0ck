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
