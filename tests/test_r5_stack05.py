"""Round-5 review findings owned by the filed-dedupe-scope branch."""

import json
import re
from pathlib import Path

from test_outside import OutsideFixture, needs

ROOT = Path(__file__).resolve().parents[1]


class ResolveKeepsOtherDismissals(OutsideFixture):
    """R5-needs-010 (class A): resolving one filed item must not strip the undo tokens of the
    dismissals that remain."""

    def test_resolving_one_hidden_item_leaves_another_items_undo_token_valid(self):
        a = self.file(dedupe_key="a", title="A")
        b = self.file(dedupe_key="b", title="B")
        items = {i["id"]: i for i in needs.build_queue()["items"]}
        needs.hide(a["id"], items[a["id"]]["version"])
        token_b = needs.hide(b["id"], items[b["id"]]["version"])
        out = self.tools["unbl0ck_file"][2](
            {"action": "resolve", "id": a["id"], "version": a["version"]}, profile="work"
        )
        self.assertTrue(json.loads(out)["resolved"])
        raw = json.loads((needs.data_dir(self.home) / "hidden.json").read_text())
        self.assertNotIn(a["id"], raw)
        self.assertIn(token_b, raw[b["id"]])  # the stored entry still carries B's token
        self.assertTrue(needs.unhide(b["id"], token=token_b))
        self.assertEqual([i["id"] for i in needs.build_queue()["items"]], [b["id"]])


class KeyOnlyResolveDocs(OutsideFixture):
    """R5-needs-044 / R5-desk-012 (class I): the docs may not promise a protection that only
    applies when the caller passes `version`."""

    def test_key_only_resolve_clears_the_current_record_even_if_refiled(self):
        first = self.file(dedupe_key="k", body="one")
        second = self.file(dedupe_key="k", body="two")
        self.assertNotEqual(first["version"], second["version"])
        out = json.loads(
            self.tools["unbl0ck_file"][2]({"action": "resolve", "dedupe_key": "k"}, profile="work")
        )
        self.assertTrue(out["resolved"])  # documented: no version, no refile protection

    def test_key_resolve_with_the_old_version_keeps_the_refiled_record(self):
        first = self.file(dedupe_key="k", body="one")
        self.file(dedupe_key="k", body="two")
        out = json.loads(
            self.tools["unbl0ck_file"][2](
                {"action": "resolve", "dedupe_key": "k", "version": first["version"]},
                profile="work",
            )
        )
        self.assertFalse(out["ok"])
        self.assertEqual(len(list((needs.data_dir(self.home) / "outside").glob("*.json"))), 1)

    def test_readme_says_the_refile_protection_needs_the_version(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        line = next(l for l in text.splitlines() if "unbl0ck_file" in l and "resolve" in l)
        flat = re.sub(r"\s+", " ", line)
        self.assertNotIn("(optionally with `version`); an item another agent has refiled", flat)
        self.assertRegex(
            flat,
            r"[Ww]ithout `version`[^.]*clears whatever record currently holds that key",
        )

    def test_tool_schema_says_the_version_guards_a_key_resolve_too(self):
        props = self.tools["unbl0ck_file"][1]["parameters"]["properties"]
        self.assertIn("dedupe_key", props["version"]["description"])
        self.assertIn("refiled", props["version"]["description"])
