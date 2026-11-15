"""Cross-surface contract: the same queue, shown on every surface (issue #47).

Desktop is the full surface. The CLI and local slash list asks, the remote slash command
gives counts, and the Pocket pack carries at most six asks. These tests pin what stays
common (identity, order, status) and every intentional difference."""

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("unbl0ck_pkg_surface", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
pkg = importlib.util.module_from_spec(spec)
sys.modules["unbl0ck_pkg_surface"] = pkg
spec.loader.exec_module(pkg)

nspec = importlib.util.spec_from_file_location("unbl0ck_needs_surface", ROOT / "needs.py")
needs = importlib.util.module_from_spec(nspec)
sys.modules["unbl0ck_needs_surface"] = needs
nspec.loader.exec_module(needs)

RID = "req-9f3a7c"
SECRET_TEXT = "wire the SECRET-BODY to acme"


def kanban(i, tier="ask"):
    return {"source": "kanban", "id": f"kanban:default:t{i}", "board": "default", "task_id": f"t{i}", "tier": tier,
            "title": f"Card {i}", "ask": f"{SECRET_TEXT} {i}?", "why": "blocked", "profile": "default", "version": "1"}


def make_queue():
    prompt = {"source": "prompt", "id": "prompt:ops:1", "tier": "ask", "kind": "approval", "title": "ops approval", "ask": "rm -rf /srv/secret-thing", "why": "w",
              "profile": "ops", "version": "1"}
    pairing = {"source": "pairing", "id": f"pairing:telegram:{RID}", "tier": "ask", "title": "Ana wants to talk", "ask": "New person", "why": "w",
               "profile": "default", "platform": "telegram", "approve_command": f"hermes pairing approve telegram {RID}", "version": "1"}
    outside = {"source": "outside", "id": "outside:o1", "tier": "ask", "kind": "draft", "title": "Draft", "ask": f"{SECRET_TEXT} draft?", "why": "w",
               "profile": "default", "act_where": "Mail", "origin": "filed", "version": "1"}
    chat = {"source": "chat", "id": "chat:default:s1", "tier": "ask", "title": "Chat ask", "ask": f"{SECRET_TEXT} chat?", "why": "w",
            "profile": "default", "platform": "telegram", "chat_with": "Mallory", "version": "1"}
    # build_queue orders sources prompt, pairing, kanban, outside, chat (needs.SOURCE_ORDER).
    asks = [prompt, pairing] + [kanban(i) for i in range(1, 9)] + [outside, chat]
    others = [kanban(90, "stuck"), kanban(91, "stuck"), kanban(92, "answered")]
    items = asks + others
    return {"count": len(asks), "counts": {"asks": len(asks), "answered": 1, "stuck": 2}, "errors": [], "items": items, "hidden": 0}


def with_queue(q):
    pkg._needs = lambda: types.SimpleNamespace(build_queue=lambda: q, pocket_pack=needs.pocket_pack, _clean=needs._clean, _SUDO_WITHHELD=needs._SUDO_WITHHELD)
    return q


class CommonIdentityOrderStatus(unittest.TestCase):
    def setUp(self):
        self.q = with_queue(make_queue())
        self.asks = [i for i in self.q["items"] if i["tier"] not in ("stuck", "answered")]

    def test_local_list_follows_queue_order_and_lists_only_asks(self):
        out = pkg._summary(limit=50)
        lines = [l for l in out.splitlines() if l.startswith("- [")]
        self.assertEqual(len(lines), len(self.asks))
        for line, item in zip(lines, self.asks):
            self.assertIn(item["title"], line)
        for other in (i for i in self.q["items"] if i["tier"] in ("stuck", "answered")):
            self.assertNotIn(other["title"], out)

    def test_pack_cards_are_the_first_six_asks_in_queue_order(self):
        pack = needs.pocket_pack(self.q)
        self.assertEqual([c["id"] for c in pack["cards"]], [i["id"] for i in self.asks[:6]])

    def test_fixture_uses_the_real_source_order(self):
        order = [needs.SOURCE_ORDER[i["source"]] for i in self.asks]
        self.assertEqual(order, sorted(order))

    def test_every_surface_agrees_on_the_number_of_asks(self):
        n = self.q["count"]
        self.assertIn(f"{n} thing(s) waiting on you", pkg._summary())
        self.assertIn(f"{n} thing(s) waiting on you", pkg._count_only())
        self.assertIn(f"{n} thing(s) waiting on you", pkg.slash_unbl0ck(platform="cli"))
        self.assertIn(f"{n} thing(s) waiting on you", pkg.slash_unbl0ck(platform="telegram"))

    def test_unknown_surface_is_treated_as_remote(self):
        for surface in (None, "", "cli ", "webhook", "mystery"):
            self.assertEqual(pkg.slash_unbl0ck(platform=surface), pkg._count_only())


class IntentionalDifferences(unittest.TestCase):
    def setUp(self):
        self.q = with_queue(make_queue())

    def test_remote_output_is_counts_only(self):
        out = pkg.slash_unbl0ck(platform="telegram")
        for leak in (SECRET_TEXT, "Card ", "Mallory", RID, "approve", "rm -rf", "ops", "Ana", "kanban:", "default"):
            self.assertNotIn(leak, out)
        self.assertIn("Open UNBL0CK in Hermes Desktop", out)
        self.assertIn("hermes unbl0ck", out)

    def test_remote_output_discloses_stuck_and_answered_by_count_only(self):
        out = pkg.slash_unbl0ck(platform="telegram")
        self.assertIn("2 stuck", out)
        self.assertIn("1 answered", out)

    def test_local_list_discloses_the_limit_and_the_unlisted_cards(self):
        out = pkg._summary(limit=5)
        self.assertIn("…and 7 more. Open /unblock in Desktop.", out)
        self.assertIn("Not listed here: 2 stuck, 1 answered", out)

    def test_pack_discloses_what_it_left_out_as_counts(self):
        pack = needs.pocket_pack(self.q)
        self.assertEqual(pack["omitted"], {"asks": 6, "stuck": 2, "answered": 1, "unread_sources": 0, "see": "Open UNBL0CK in Hermes Desktop"})
        blob = json.dumps(pack["omitted"])
        self.assertNotIn(SECRET_TEXT, blob)
        self.assertNotIn("Card ", blob)

    def test_pack_has_no_stuck_or_answered_cards(self):
        ids = {c["id"] for c in needs.pocket_pack(self.q)["cards"]}
        self.assertFalse(ids & {"kanban:default:t90", "kanban:default:t91", "kanban:default:t92"})

    def test_pack_options_point_back_and_carry_no_secret(self):
        blob = json.dumps(needs.pocket_pack(self.q))
        self.assertNotIn(RID, blob.replace(f"pairing:telegram:{RID}", ""))  # the id appears only as the card id
        self.assertNotIn("hermes pairing approve", blob)

    def test_local_list_keeps_the_native_pairing_command(self):
        self.assertIn(f"approve: hermes pairing approve telegram {RID}", pkg._summary(limit=50))

    def test_json_surface_is_the_full_queue(self):
        self.assertEqual({i["id"] for i in self.q["items"]} >= {"kanban:default:t90", "kanban:default:t92"}, True)


class EmptyAndIncomplete(unittest.TestCase):
    def test_nothing_waiting_but_stuck_cards_is_not_an_all_clear(self):
        q = with_queue({"count": 0, "counts": {"asks": 0, "answered": 1, "stuck": 1}, "errors": [], "items": [kanban(1, "stuck"), kanban(2, "answered")]})
        for out in (pkg._summary(), pkg.slash_unbl0ck(platform="telegram")):
            self.assertNotIn("Nothing is blocked on you.", out)
            self.assertIn("1 stuck", out)
            self.assertIn("1 answered", out)
            self.assertIn("Desktop", out)
        self.assertNotIn("Card ", pkg.slash_unbl0ck(platform="telegram"))
        self.assertEqual(q["count"], 0)

    def test_incomplete_empty_ask_list_still_discloses_stuck_and_answered(self):
        with_queue({"count": 0, "counts": {"asks": 0, "answered": 1, "stuck": 1}, "errors": ["kanban: boom"],
                    "items": [kanban(1, "stuck"), kanban(2, "answered")]})
        out = pkg._summary()
        self.assertIn("Not listed here: 1 stuck, 1 answered", out)
        self.assertIn("(source error: kanban: boom)", out)
        self.assertNotIn("Nothing is blocked on you.", out)
        self.assertNotIn("Card ", out.split("Not listed here:")[0])

    def test_all_clear_stays_all_clear_without_other_cards(self):
        with_queue({"count": 0, "counts": {"asks": 0, "answered": 0, "stuck": 0}, "errors": [], "items": []})
        self.assertEqual(pkg._summary(), "Nothing is blocked on you.")
        self.assertEqual(pkg.slash_unbl0ck(platform="telegram"), "Nothing is blocked on you.")

    def test_queues_without_counts_behave_as_before(self):
        with_queue({"count": 1, "errors": [], "items": [kanban(1)]})
        self.assertEqual(pkg.slash_unbl0ck(platform="telegram"),
                         "1 thing(s) waiting on you. Open UNBL0CK in Hermes Desktop or run hermes unbl0ck on the machine to see them.")


class SurfaceMatrixDocs(unittest.TestCase):
    SURFACES = ("Desktop", "CLI and local slash", "CLI --json", "Remote slash", "Pocket pack")
    DIMENSIONS = ("Visibility", "Context", "Action routing", "Completion", "Recovery", "Intentional limits")

    def test_matrix_lists_every_surface_and_dimension(self):
        text = (ROOT / "docs/operator-guide.md").read_text()
        block = text.split("<!-- surface-matrix:start -->")[1].split("<!-- surface-matrix:end -->")[0]
        header = [c.strip() for c in block.strip().splitlines()[0].strip("|").split("|")]
        self.assertEqual(header[1:], list(self.DIMENSIONS))
        rows = {}
        for line in block.strip().splitlines()[2:]:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rows[cells[0]] = cells[1:]
        self.assertEqual(set(rows), set(self.SURFACES))
        for surface, cells in rows.items():
            self.assertEqual(len(cells), len(self.DIMENSIONS), surface)
            self.assertTrue(all(len(c) > 3 for c in cells), surface)

    def test_remote_row_states_counts_only(self):
        text = (ROOT / "docs/operator-guide.md").read_text()
        row = next(l for l in text.splitlines() if l.startswith("| Remote slash"))
        self.assertIn("counts", row)
        self.assertIn("never", row)


DESKTOP = r"""
const q=JSON.parse(process.env.QUEUE);
env.useValue=key=>key==='queue'?{...q,at:Date.now()}:key===env.$undo?undo:false;
cursor=0;state=[];
const tree=Page({ctx:{rest:async()=>({})},refresh});
const ids=all(tree).filter(n=>n&&n.type===Item&&n.props&&n.props.item).map(n=>n.props.item.id);
console.log(JSON.stringify(ids));
"""


class DesktopSameQueue(unittest.TestCase):
    def test_desktop_shows_the_same_items_in_the_same_relative_order(self):
        import os
        import subprocess

        from test_operator_recovery import HARNESS

        q = make_queue()
        # Desktop shows work waiting on you; stuck and answered Kanban cards sit in their own groups.
        for path in (ROOT / "desktop/src/plugin.src.js", ROOT / "desktop/plugin.js"):
            with self.subTest(path=path.name):
                r = subprocess.run(["node", "-e", HARNESS + DESKTOP, str(path)], capture_output=True, text=True,
                                   env={**os.environ, "QUEUE": json.dumps(q)})
                self.assertEqual(r.returncode, 0, r.stderr)
                shown = json.loads(r.stdout.strip().splitlines()[-1])
                self.assertEqual(sorted(shown), sorted(i["id"] for i in q["items"]), "same identity set")
                asks = [i["id"] for i in q["items"] if i["tier"] not in ("stuck", "answered")]
                self.assertEqual([i for i in shown if i in asks], asks, "same relative order among asks")


if __name__ == "__main__":
    unittest.main()
