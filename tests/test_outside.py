"""Outside items stay bounded, read-only at the source, and private on gateways."""

import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "unbl0ck_outside_tests",
    ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
needs = plugin._needs()


class OutsideFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.folder = self.home / "exports"
        self.folder.mkdir()
        self.settings = {}
        for name, value in (
            ("hermes_home", lambda: self.home),
            ("_settings", lambda: self.settings),
            ("kanban_items", lambda **kw: []),
            ("chat_items", lambda *a, **kw: []),
            ("wait_items", lambda *a, **kw: []),
            ("pairing_items", lambda *a, **kw: []),
        ):
            p = patch.object(needs, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {"UNBL0CK_OUTSIDE_DIRS": ""})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(setattr, plugin, "_PLUGIN_CTX", plugin._PLUGIN_CTX)
        self.tools = {}

        def register_tool(name, toolset, schema, handler, **kwargs):
            self.tools[name] = (toolset, schema, handler)

        ctx = types.SimpleNamespace(
            register_tool=register_tool,
            register_command=lambda *a, **kw: None,
            register_cli_command=lambda *a, **kw: None,
        )
        plugin.register(ctx)

    def file(self, **changes):
        args = {
            "kind": "draft",
            "title": "Reply to customer",
            "body": "Ready for you to send.",
            "act_where": "Outlook → Drafts",
            **changes,
        }
        self.assertIn(
            "unbl0ck_file", self.tools, "register(ctx) must expose the filing tool"
        )
        return json.loads(
            self.tools["unbl0ck_file"][2](args, profile="work", session_id="session-1")
        )

    def watch(self):
        self.settings["outside_dirs"] = str(self.folder)

    def record(self, result):
        return (
            needs.data_dir(self.home)
            / "outside"
            / (result["id"].split(":")[-1] + ".json")
        )


class Outside(OutsideFixture):
    def test_tool_files_owner_only_item_in_queue(self):
        result = self.file(ref="draft-123")
        self.assertTrue(result["ok"])
        self.assertEqual(self.tools["unbl0ck_file"][0], "unbl0ck")
        self.assertEqual(
            self.tools["unbl0ck_file"][1]["parameters"]["properties"]["kind"]["enum"],
            ["draft", "notes", "approval", "other"],
        )
        q = needs.build_queue()
        self.assertEqual(q["counts"]["asks"], 1)
        (item,) = q["items"]
        self.assertEqual(
            (item["source"], item["origin"], item["kind"], item["tier"]),
            ("outside", "filed", "draft", "normal"),
        )
        self.assertEqual((item["profile"], item["session_id"]), ("work", "session-1"))
        self.assertEqual(
            (item["act_where"], item["ref"], item["ask"]),
            ("Outlook → Drafts", "draft-123", "Ready for you to send."),
        )
        self.assertEqual(stat.S_IMODE(self.record(result).stat().st_mode), 0o600)

    def test_dedupe_updates_in_place_and_unchanged_content_keeps_version(self):
        a = self.file(dedupe_key="customer")
        before = needs.build_queue()["items"][0]
        b = self.file(dedupe_key="customer", title="Revised reply")
        (after,) = needs.build_queue()["items"]
        self.assertEqual(a["id"], b["id"])
        self.assertNotEqual(before["version"], after["version"])
        self.assertEqual(after["title"], "Revised reply")
        self.file(dedupe_key="customer", title="Revised reply")
        self.assertEqual(needs.build_queue()["items"][0]["version"], after["version"])
        self.assertEqual(len(list(self.record(a).parent.glob("*.json"))), 1)

    def test_limits_truncate_text_and_mark_body(self):
        self.file(
            title="t" * 200,
            body="b" * 9000,
            act_where="a" * 150,
            ref="r" * 400,
            dedupe_key="d" * 150,
        )
        (it,) = needs.build_queue()["items"]
        self.assertEqual(len(it["title"]), 160)
        self.assertEqual(len(it["text"]), 8000)
        self.assertTrue(it["text"].endswith("[truncated]"))
        self.assertEqual(len(it["act_where"]), 120)
        self.assertEqual(len(it["ref"]), 300)
        self.assertEqual(
            len(
                json.loads(
                    next(
                        (needs.data_dir(self.home) / "outside").glob("*.json")
                    ).read_text()
                )["dedupe_key"]
            ),
            120,
        )

    def test_cap_refuses_201st_but_allows_updates(self):
        for n in range(200):
            self.assertTrue(self.file(dedupe_key=str(n))["ok"])
        result = self.file()
        self.assertIn("200", result["error"])
        self.assertTrue(self.file(dedupe_key="0", title="Changed at cap")["ok"])
        self.assertEqual(needs.build_queue()["count"], 200)

    def test_resolve_by_id_or_key_removes_only_owned_records(self):
        for key in ("one", "two"):
            result = self.file(dedupe_key=key)
            args = {
                "action": "resolve",
                **({"id": result["id"]} if key == "one" else {"dedupe_key": key}),
            }
            reply = json.loads(self.tools["unbl0ck_file"][2](args))
            self.assertTrue(reply["ok"])
            self.assertFalse(self.record(result).exists())
        self.assertEqual(needs.build_queue()["count"], 0)

    def test_invalid_inputs_and_path_traversal_are_rejected(self):
        for changes in (
            {"kind": "send"},
            {"action": "send"},
            {"title": ""},
            {"body": []},
            {"action": "resolve", "id": "../../exports/note.txt"},
            {"action": "resolve"},
        ):
            with self.subTest(changes=changes):
                self.assertIn("error", self.file(**changes))
        self.assertEqual(needs.build_queue()["count"], 0)

    def test_handler_accepts_missing_context(self):
        result = json.loads(
            self.tools["unbl0ck_file"][2](
                {
                    "kind": "other",
                    "title": "Read this",
                    "body": "",
                    "act_where": "Notes",
                }
            )
        )
        self.assertTrue(result["ok"])

    def test_file_requires_body_but_allows_empty_text(self):
        args = {"kind": "notes", "title": "Notes", "act_where": "Plaud app"}
        self.assertIn("error", json.loads(self.tools["unbl0ck_file"][2](args)))
        self.assertTrue(
            json.loads(self.tools["unbl0ck_file"][2]({**args, "body": ""}))["ok"]
        )

    def test_hidden_filed_item_resurfaces_on_update(self):
        self.file(dedupe_key="note")
        (it,) = needs.build_queue()["items"]
        needs.hide(it["id"], it["version"], self.home)
        self.assertEqual(needs.build_queue()["count"], 0)
        self.file(dedupe_key="note", body="New information")
        (after,) = needs.build_queue()["items"]
        self.assertEqual(after["id"], it["id"])
        self.assertNotEqual(after["version"], it["version"])

    def test_eml_uses_subject_to_and_first_plain_part(self):
        self.watch()
        msg = EmailMessage()
        msg["Subject"] = "Draft café"
        msg["To"] = "Reader <reader@example.com>"
        msg.set_content("Plain draft café")
        msg.add_alternative("<script>bad()</script>", subtype="html")
        msg.add_attachment(
            "Second plain part must not replace the body", filename="extra.txt"
        )
        (self.folder / "draft.eml").write_bytes(msg.as_bytes())
        (it,) = needs.build_queue()["items"]
        self.assertEqual(
            (it["source"], it["kind"], it["origin"], it["title"]),
            ("outside", "draft", "folder", "Draft café"),
        )
        self.assertEqual(it["to"], "Reader <reader@example.com>")
        self.assertIn("Plain draft café", it["text"])
        self.assertNotIn("<script>", json.dumps(it))
        self.assertNotIn("Second plain part", it["text"])
        self.assertEqual(it["path"], "draft.eml")

    def test_eml_decodes_declared_latin_1_charset(self):
        self.watch()
        raw = (
            b"From: sender@example.test\r\nSubject: Lunch\r\n"
            b'Content-Type: text/plain; charset="iso-8859-1"\r\n'
            b"Content-Transfer-Encoding: 8bit\r\n\r\n"
            b"Reuni\xf3n ma\xf1ana en el caf\xe9\r\n"
        )
        (self.folder / "draft.eml").write_bytes(raw)
        (it,) = needs.build_queue()["items"]
        self.assertEqual(it["text"].strip(), "Reuni\u00f3n ma\u00f1ana en el caf\u00e9")

    def test_html_only_mail_never_displays_html(self):
        self.watch()
        msg = EmailMessage()
        msg.set_content(
            "<img src='https://example.com/tracker'>secret HTML", subtype="html"
        )
        (self.folder / "html.eml").write_bytes(msg.as_bytes())
        (it,) = needs.build_queue()["items"]
        self.assertNotIn("secret HTML", it["text"])
        self.assertNotIn("<img", it["text"])

    def test_notes_use_first_nonempty_line_and_decode_replacement(self):
        self.watch()
        (self.folder / "summary.md").write_text(
            "\n  \n# Call summary\nFollow up tomorrow."
        )
        (self.folder / "transcript.txt").write_bytes(
            b"\nCall transcript\nInvalid: \xff"
        )
        items = {i["path"]: i for i in needs.build_queue()["items"]}
        self.assertEqual(items["summary.md"]["title"], "# Call summary")
        self.assertEqual(items["transcript.txt"]["title"], "Call transcript")
        self.assertIn("\ufffd", items["transcript.txt"]["text"])
        self.assertTrue(all(i["kind"] == "notes" for i in items.values()))

    def test_symlinks_dotfiles_directories_and_unsupported_files_are_skipped(self):
        self.watch()
        (self.home / "private.txt").write_text("Private")
        (self.folder / "valid.txt").write_text("Valid")
        (self.folder / "escape.txt").symlink_to(self.home / "private.txt")
        (self.folder / "link.txt").symlink_to(self.folder / "valid.txt")
        (self.folder / ".hidden.txt").write_text("Hidden")
        (self.folder / "ignored.html").write_text("Ignored")
        (self.folder / "subdir.txt").mkdir()
        (self.folder / "subdir.txt" / "nested.txt").write_text("Nested")
        self.assertEqual(
            [i["path"] for i in needs.build_queue()["items"]], ["valid.txt"]
        )

    def test_resolved_path_escaping_folder_is_skipped(self):
        self.watch()
        candidate = self.folder / "note.txt"
        candidate.write_text("Never read")
        resolve = Path.resolve

        def escaped(path, *args, **kwargs):
            return (
                self.home / "escaped.txt"
                if path == candidate
                else resolve(path, *args, **kwargs)
            )

        with patch.object(Path, "resolve", escaped):
            self.assertEqual(needs.build_queue()["items"], [])

    def test_read_is_capped_at_256_kib(self):
        self.watch()
        (self.folder / "big.txt").write_bytes(
            b"a" * (256 * 1024) + b"DO NOT READ THIS TAIL"
        )
        (it,) = needs.build_queue()["items"]
        self.assertNotIn("DO NOT READ", it["text"])
        self.assertLessEqual(len(it["text"]), 256 * 1024)

    def test_only_newest_200_files_per_folder_are_read(self):
        self.watch()
        for n in range(205):
            f = self.folder / f"{n}.txt"
            f.write_text(f"Note {n}")
            os.utime(f, ns=(1_000_000_000 + n, 1_000_000_000 + n))
        items = needs.build_queue()["items"]
        self.assertEqual(len(items), 200)
        self.assertEqual(items[0]["path"], "204.txt")
        self.assertNotIn("4.txt", [i["path"] for i in items])

    def test_hidden_folder_item_returns_only_after_change(self):
        self.watch()
        f = self.folder / "note.txt"
        f.write_text("Before")
        (it,) = needs.build_queue()["items"]
        self.assertEqual(it["version"], f"{f.stat().st_mtime_ns}:{f.stat().st_size}")
        needs.hide(it["id"], it["version"], self.home)
        self.assertEqual(needs.build_queue()["count"], 0)
        f.write_text("After, a longer note")
        (after,) = needs.build_queue()["items"]
        self.assertEqual(after["id"], it["id"])
        self.assertNotEqual(after["version"], it["version"])

    def test_outside_excerpt_keeps_the_start_of_a_watched_note(self):
        self.watch()
        opening = "Quarterly vendor renewal: please approve the 12k spend."
        source_text = opening + (" Supporting details follow. " * 40)
        source_text += (
            " CONFIDENTIALITY NOTICE: this e-mail is intended only for the addressee."
        )
        (self.folder / "renewal.txt").write_text(source_text)
        (queued,) = needs.build_queue()["items"]
        self.assertTrue(queued["excerpt"].startswith(opening), queued["excerpt"])
        self.assertNotIn("CONFIDENTIALITY NOTICE", queued["excerpt"])

    def test_missing_or_unreadable_folder_reports_error_and_keeps_other_items(self):
        self.file()
        self.settings["outside_dirs"] = str(self.home / "missing")
        self.assertTrue(
            any(e.startswith("outside:") for e in needs.build_queue()["errors"])
        )
        self.watch()
        iterdir = Path.iterdir

        def unreadable(path):
            if path == self.folder:
                raise PermissionError("denied")
            return iterdir(path)

        with patch.object(Path, "iterdir", unreadable):
            q = needs.build_queue()
        self.assertEqual(q["count"], 1)
        self.assertTrue(any(e.startswith("outside:") for e in q["errors"]))

    def test_env_fallback_tilde_multiple_dirs_and_duplicate_dirs(self):
        (self.folder / "note.txt").write_text("One")
        other = self.home / "more"
        other.mkdir()
        (other / "note.md").write_text("Two")
        with patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "UNBL0CK_OUTSIDE_DIRS": f"~/exports, {other}, {self.folder}",
            },
        ):
            self.assertEqual(needs.build_queue()["count"], 2)

    def test_default_is_off_and_relative_dir_is_an_error(self):
        (self.folder / "note.txt").write_text("Off")
        self.assertEqual(needs.build_queue()["items"], [])
        self.settings["outside_dirs"] = "relative/exports"
        self.assertTrue(
            any(e.startswith("outside:") for e in needs.build_queue()["errors"])
        )

    def test_source_failure_is_isolated(self):
        with patch.object(
            needs, "outside_items", side_effect=RuntimeError("broken outside store")
        ):
            q = needs.build_queue()
        self.assertIn("outside: RuntimeError: broken outside store", q["errors"])

    def test_order_places_outside_between_kanban_and_chat(self):
        self.file()

        def item(source):
            return {
                "id": source + ":one",
                "version": "1",
                "source": source,
                "title": "Title",
                "since": 0,
            }

        with (
            patch.object(needs, "kanban_items", return_value=[item("kanban")]),
            patch.object(needs, "chat_items", return_value=[item("chat")]),
        ):
            self.assertEqual(
                [i["source"] for i in needs.build_queue()["items"]],
                ["kanban", "outside", "chat"],
            )

    def test_cli_includes_outside_and_gateway_remains_count_only(self):
        self.watch()
        f = self.folder / "secret.txt"
        f.write_text("PRIVATE NOTE TITLE\nPRIVATE BODY")
        local = plugin.slash_unbl0ck(platform="cli")
        self.assertIn("PRIVATE NOTE TITLE", local)
        self.assertIn("outside", local)
        for platform in ("telegram", "discord", None):
            remote = plugin.slash_unbl0ck(platform=platform)
            self.assertIn("1 thing(s)", remote)
            for private in ("PRIVATE", str(self.folder), "secret.txt"):
                self.assertNotIn(private, remote)
        out = io.StringIO()
        with redirect_stdout(out):
            plugin._cli(types.SimpleNamespace(json=True, pack=False))
        self.assertEqual(json.loads(out.getvalue())["items"][0]["source"], "outside")

    def test_pocket_pack_names_the_external_place_to_act(self):
        self.file()
        (card,) = needs.pocket_pack(needs.build_queue())["cards"]
        self.assertIn("Outlook → Drafts", card["options"][0]["send"])

    def test_bluf_gets_title_and_bounded_text_and_cache_tracks_version(self):
        self.file(kind="notes", body="Call details " * 1000, dedupe_key="call")
        (it,) = needs.build_queue()["items"]
        payload = needs.bluf_messages(it)[1]["content"]
        self.assertIn("Call details", payload)
        self.assertIn("Reply to customer", payload)
        self.assertLess(len(payload), 6500)
        needs.save_bluf(
            it["id"], it["version"], "Read the follow-up", "Notes", self.home
        )
        self.assertEqual(needs.build_queue()["items"][0]["bluf"], "Read the follow-up")
        self.file(kind="notes", body="Changed call", dedupe_key="call")
        self.assertNotIn("bluf", needs.build_queue()["items"][0])


try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:
    FastAPI = None


@unittest.skipIf(FastAPI is None, "fastapi not installed")
class OutsideAPI(OutsideFixture):
    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location(
            "unbl0ck_outside_api_tests", ROOT / "dashboard/plugin_api.py"
        )
        self.api = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.api
        spec.loader.exec_module(self.api)
        self.api._needs = lambda: needs
        self.api._loaded_ctx = lambda: None
        app = FastAPI()
        app.include_router(self.api.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_done_deletes_filed_record(self):
        result = self.file()
        (it,) = needs.build_queue()["items"]
        reply = self.client.post(
            "/outside/done", json={"id": it["id"], "version": it["version"]}
        )
        self.assertEqual(reply.status_code, 200)
        self.assertFalse(self.record(result).exists())
        self.assertEqual(needs.build_queue()["count"], 0)

    def test_queue_sends_only_an_excerpt_and_full_text_is_fetched_lazily(self):
        self.watch()
        source_text = "First line. " + ("Long note text. " * 5000)
        f = self.folder / "large.txt"
        f.write_text(source_text)
        (item,) = needs.build_queue()["items"]
        response = self.client.get("/queue")
        self.assertEqual(response.status_code, 200)
        (queued,) = response.json()["items"]
        self.assertNotIn("text", queued)
        self.assertLessEqual(len(queued["ask"]), 480)
        self.assertEqual(queued["ask"], item["excerpt"])
        full = self.client.get(
            "/outside/text", params={"id": item["id"], "version": item["version"]}
        )
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.json()["text"], source_text)
        f.write_text("Changed note")
        stale = self.client.get(
            "/outside/text", params={"id": item["id"], "version": item["version"]}
        )
        self.assertEqual(stale.status_code, 409)

    def test_done_folder_hides_until_change_without_touching_file(self):
        self.watch()
        f = self.folder / "call.txt"
        f.write_text("Read call notes")
        before = f.read_bytes(), f.stat().st_mtime_ns
        (it,) = needs.build_queue()["items"]
        reply = self.client.post(
            "/outside/done", json={"id": it["id"], "version": it["version"]}
        )
        self.assertEqual(reply.status_code, 200)
        self.assertEqual((f.read_bytes(), f.stat().st_mtime_ns), before)
        self.assertEqual(needs.build_queue()["count"], 0)
        f.write_text("Updated call notes")
        self.assertEqual(needs.build_queue()["count"], 1)

    def test_done_refuses_stale_filed_version_and_invalid_id(self):
        self.file(dedupe_key="draft")
        (it,) = needs.build_queue()["items"]
        self.file(dedupe_key="draft", title="Changed draft")
        self.assertEqual(
            self.client.post(
                "/outside/done", json={"id": it["id"], "version": it["version"]}
            ).status_code,
            409,
        )
        self.assertEqual(needs.build_queue()["count"], 1)
        self.assertEqual(
            self.client.post(
                "/outside/done", json={"id": "../../file", "version": "1"}
            ).status_code,
            400,
        )

    def test_outside_summaries_use_existing_path_and_respect_disabled_setting(self):
        self.file()
        queued = []
        self.api._loaded_ctx = lambda: types.SimpleNamespace(llm=object())
        self.api._queue_bluf = lambda n, ctx, it: queued.append(it["source"])
        self.assertEqual(self.client.get("/queue").status_code, 200)
        self.assertEqual(queued, ["outside"])
        queued.clear()
        self.settings["bluf_enabled"] = False
        self.client.get("/queue")
        self.assertEqual(queued, [])


if __name__ == "__main__":
    unittest.main()
