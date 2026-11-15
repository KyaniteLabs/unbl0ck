"""Capability matrix: what each source lets you do, and why not (issue #33)."""

import importlib.util
import re
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from test_operator_recovery import HARNESS

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("unbl0ck_needs_caps", ROOT / "needs.py")
needs = importlib.util.module_from_spec(spec)
sys.modules["unbl0ck_needs_caps"] = needs
spec.loader.exec_module(needs)

ROWS = ("kanban", "chat", "prompt", "pairing", "outside_filed", "outside_folder")
WORDS = {"supported": "Yes", "restricted": "By design", "missing": "Not yet"}


class Matrix(unittest.TestCase):
    def test_every_source_and_ability_is_classified(self):
        self.assertEqual(set(needs.CAPABILITIES), set(ROWS))
        for row, cells in needs.CAPABILITIES.items():
            self.assertEqual(tuple(cells), needs.CAPABILITY_ABILITIES, row)
            for ability, cell in cells.items():
                self.assertIn(cell["status"], needs.CAPABILITY_STATUSES, (row, ability))
                if cell["status"] != "supported":
                    self.assertGreaterEqual(len(cell["why"]), 20, f"{row}.{ability} must say why")

    def test_every_queue_source_has_a_row(self):
        literal = set(re.findall(r'"source": "(\w+)"', (ROOT / "needs.py").read_text()))
        self.assertEqual(literal, set(needs.SOURCES))
        for source in needs.SOURCES:
            keys = {needs.capability_key({"source": source, "origin": o}) for o in ("filed", "folder")}
            self.assertTrue(keys <= set(ROWS) and "" not in keys, source)

    def test_unknown_source_has_no_row(self):
        self.assertEqual(needs.capability_key({"source": "mystery"}), "")

    def test_safety_invariants(self):
        caps = needs.CAPABILITIES
        for row in ("chat", "prompt", "pairing", "outside_filed", "outside_folder"):
            self.assertNotEqual(caps[row]["reply"]["status"], "supported", f"{row}: no inline send or approve")
        for row in ("prompt", "pairing"):
            self.assertEqual(caps[row]["model_help"]["status"], "restricted", f"{row} is never sent to a model")
        self.assertIn("password", caps["prompt"]["model_help"]["why"])
        self.assertIn("code", caps["pairing"]["context"]["why"])
        self.assertEqual(caps["outside_folder"]["route"]["status"], "restricted")  # read-only folder

    def test_gaps_are_marked_missing_not_restricted(self):
        # Only intentional safety rules may read "by design"; unbuilt help must say "not yet".
        self.assertEqual(needs.CAPABILITIES["chat"]["model_help"]["status"], "missing")
        self.assertEqual(needs.CAPABILITIES["outside_filed"]["model_help"]["status"], "missing")
        self.assertEqual(needs.CAPABILITIES["kanban"]["model_help"]["status"], "supported")

    def test_operator_guide_table_matches_the_matrix(self):
        text = (ROOT / "docs/operator-guide.md").read_text()
        block = text.split("<!-- capability-matrix:start -->")[1].split("<!-- capability-matrix:end -->")[0]
        rows = {}
        for line in block.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 6 and cells[0] in ROWS:
                rows[cells[0]] = cells[1:]
        self.assertEqual(set(rows), set(ROWS))
        for row, cells in rows.items():
            expected = [WORDS[needs.CAPABILITIES[row][a]["status"]] for a in needs.CAPABILITY_ABILITIES]
            self.assertEqual(cells, expected, row)

    def test_queue_payload_publishes_the_matrix(self):
        import tempfile

        with tempfile.TemporaryDirectory() as home, mock.patch.object(needs, "_settings", lambda: {}):
            q = needs.build_queue(Path(home))
        self.assertEqual(q["capabilities"], needs.CAPABILITIES)


NOTE = r"""
const caps=%s;
const render=it=>{cursor=0;state=[];return Item({ctx:{rest:async()=>({})},item:it,n:1,refresh,askEnabled:true,capabilities:caps})};
const texts=t=>all(t).filter(x=>typeof x==='string').join('|');
const buttons=t=>all(t).filter(n=>n?.type==='button').map(n=>n.props.children);
"""


class DesktopNotes(unittest.TestCase):
    def run_views(self, script):
        import json

        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                prefix = HARNESS + NOTE % json.dumps(needs.CAPABILITIES)
                result = subprocess.run(["node", "-e", prefix + script, str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_unavailable_model_help_is_explained_on_every_other_source(self):
        self.run_views(r"""
const samples={
 chat:{source:'chat',id:'chat:p:s1',title:'Chat',ask:'Ship it?',tier:'ask',version:'1',profile:'p',platform:'telegram'},
 prompt:{source:'prompt',id:'prompt:p:1',title:'Prompt',ask:'Run rm?',kind:'approval',version:'1',profile:'p'},
 pairing:{source:'pairing',id:'pairing:telegram:r1',title:'Pairing',ask:'New person',platform:'telegram',tier:'ask',version:'1'},
 outside_filed:{source:'outside',origin:'filed',kind:'draft',id:'outside:o1',title:'Draft',ask:'Send?',act_where:'Mail',tier:'ask',version:'1'},
 outside_folder:{source:'outside',origin:'folder',kind:'notes',id:'outside:f1',title:'File',ask:'Read',act_where:'Folder',tier:'ask',version:'1'},
};
for(const [row,it] of Object.entries(samples)){
 const tree=render(it);
 assert.ok(!buttons(tree).includes('TL;DR'),row+' has no TL;DR button');
 assert.ok(texts(tree).includes('TL;DR is not offered here: '+caps[row].model_help.why),row+' explains why: '+texts(tree));
}
""")

    def test_kanban_keeps_tldr_and_shows_no_limitation(self):
        self.run_views(r"""
const tree=render(item);
assert.ok(buttons(tree).includes('TL;DR'));
assert.ok(!texts(tree).includes('TL;DR is not offered here'));
""")

    def test_password_prompts_say_they_are_never_sent_to_a_model(self):
        self.run_views(r"""
const tree=render({source:'prompt',id:'prompt:p:2',title:'sudo',ask:'[sudo] password for me:',kind:'sudo',version:'1',profile:'p'});
assert.ok(texts(tree).includes('password prompts'),texts(tree));
assert.ok(!buttons(tree).includes('TL;DR'));
""")

    def test_missing_capabilities_prop_adds_no_note(self):
        self.run_views(r"""
cursor=0;state=[];
const tree=Item({ctx:{rest:async()=>({})},item:{source:'chat',id:'chat:p:s9',title:'C',ask:'?',tier:'ask',version:'1',profile:'p'},n:1,refresh,askEnabled:true});
assert.ok(!texts(tree).includes('not offered here'));
""")


try:
    import fastapi
    from fastapi.testclient import TestClient
except Exception:  # noqa: BLE001
    fastapi = None


@unittest.skipIf(fastapi is None, "fastapi not installed")
class ApiBoundaries(unittest.TestCase):
    def setUp(self):
        s = importlib.util.spec_from_file_location("ub_api_caps", ROOT / "dashboard/plugin_api.py")
        self.api = importlib.util.module_from_spec(s)
        sys.modules["ub_api_caps"] = self.api
        s.loader.exec_module(self.api)
        self.sent = []
        self.ask_on = True
        needs_stub = types.SimpleNamespace(
            ask_enabled=lambda: self.ask_on,
            card_context=lambda b, t: {"id": t},
            build_ask_messages=lambda c, h: list(h),
            normalize_board_slug=lambda b: b,
            ask_journal_dir=lambda: None,
            ask_persist_replies=lambda: False,
        )
        self.api._needs = lambda: needs_stub
        self.llm = True

        def complete(messages, **kw):
            self.sent.append(messages)
            return types.SimpleNamespace(text="x", provider="p", model="m")

        self.api._loaded_ctx = lambda: types.SimpleNamespace(llm=types.SimpleNamespace(complete=complete)) if self.llm else None
        kb = types.SimpleNamespace(board_exists=lambda b: True)
        patcher = mock.patch.dict(sys.modules, {"hermes_cli": types.SimpleNamespace(kanban_db=kb), "hermes_cli.kanban_db": kb})
        patcher.start()
        self.addCleanup(patcher.stop)
        app = fastapi.FastAPI()
        app.include_router(self.api.router)
        self.c = TestClient(app)

    def ask(self, id_):
        return self.c.post("/card/ask", json={"id": id_, "messages": [{"role": "user", "content": "what?"}]})

    def test_no_model_help_for_other_sources_and_no_provider_call(self):
        for id_ in ("chat:p:s1", "prompt:p:1", "pairing:telegram:r1", "outside:o1"):
            r = self.ask(id_)
            self.assertEqual(r.status_code, 400, id_)
            self.assertIn("not a Kanban card", r.json()["detail"])
        self.assertEqual(self.sent, [])

    def test_unsupported_provider_and_disabled_ask_fail_closed(self):
        self.llm = False
        self.assertEqual(self.ask("kanban:default:t1").status_code, 503)
        self.llm = True
        self.ask_on = False
        self.assertEqual(self.ask("kanban:default:t1").status_code, 403)
        self.assertEqual(self.sent, [])

    def test_no_route_can_send_a_reply_approve_pairing_or_answer_a_prompt(self):
        routes = {(m, r.path) for r in self.api.router.routes for m in getattr(r, "methods", ()) if m != "HEAD"}
        writes = {p for m, p in routes if m in ("POST", "PUT", "PATCH", "DELETE")}
        self.assertEqual(
            writes,
            {"/card/ask", "/card/ask/{job}", "/kanban/answer", "/hide", "/unhide", "/outside/done", "/pack"},
            "a new write route needs a capability-matrix decision and a native-authorization review",
        )


class ReadOnlyExport(unittest.TestCase):
    def test_pocket_pack_never_carries_pairing_or_prompt_secrets(self):
        secret = "SECRET-CODE-123"
        queue = {"items": [
            {"source": "pairing", "id": "pairing:telegram:req9", "title": "Pairing", "ask": "New person", "why": "w",
             "tier": "ask", "platform": "telegram", "code": secret, "user_id": "u-77", "approve_command": f"hermes pairing approve telegram {secret}"},
            {"source": "prompt", "id": "prompt:p:1", "title": "Prompt", "ask": "Run it?", "why": "w", "tier": "ask", "profile": "p"},
            {"source": "chat", "id": "chat:p:s1", "title": "Chat", "ask": "Ship?", "why": "w", "tier": "ask", "profile": "p", "platform": "telegram"},
            {"source": "outside", "id": "outside:o1", "title": "Draft", "ask": "Send?", "why": "w", "tier": "ask", "act_where": "Mail"},
        ]}
        pack = needs.pocket_pack(queue)
        blob = repr(pack)
        self.assertNotIn(secret, blob)
        self.assertNotIn("u-77", blob)
        for card in pack["cards"]:
            for option in card["options"]:
                # A fixed instruction that points back at the original surface, never an inline action.
                self.assertRegex(option["send"], r"^(Open|Answer|Act) ")
                self.assertNotRegex(option["send"], r"(?i)approve .*telegram|code")


if __name__ == "__main__":
    unittest.main()
