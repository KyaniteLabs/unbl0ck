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


class Stack06Docs(unittest.TestCase):
    def test_docs_name_the_text_that_makes_advice_stale_and_the_retry_id(self):
        text = " ".join((ROOT / "docs/operator-guide.md").read_text().split())
        for needle in (
            "owner, description or comments (including a comment rewritten in place)",
            "reuses the request's id",
        ):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
