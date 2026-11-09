"""Regressions from the v0.4.1 adversarial audit: hostile names, forged timestamps, schema drift,
ghost waits, ordering, and who a gateway chat is with."""
import importlib.util, json, os, sqlite3, stat, tempfile, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("unbl0ck_needs_qa", ROOT / "needs.py")
needs = importlib.util.module_from_spec(spec); spec.loader.exec_module(needs)
SETTINGS = {"principal_name": "Simon"}
needs._settings = lambda: SETTINGS

ASK = "Want me to merge it?"
RID = "0123456789abcdef"


def pairing(home: Path, entries, platform="telegram", profile=None):
    d = (home / "profiles" / profile if profile else home) / "platforms" / "pairing"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{platform}-pending.json").write_text(json.dumps(entries))


def chat_db(path: Path, rows, legacy_messages=False, user_cols=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    extra = ", user_id TEXT, display_name TEXT" if user_cols else ""
    db.execute(f"CREATE TABLE sessions(id TEXT, title TEXT, source TEXT, chat_type TEXT, started_at REAL, last_activity_at REAL{extra})")
    mcols = "" if legacy_messages else ", display_kind TEXT, active INT"
    db.execute(f"CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL{mcols})")
    now = time.time()
    for sid, src, uid, name in rows:
        vals = (sid, "", src, "dm", now - 60, now - 30) + ((uid, name) if user_cols else ())
        db.execute(f"INSERT INTO sessions VALUES({','.join('?' * len(vals))})", vals)
        db.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES(?, 'assistant', ?, ?)", (sid, ASK, now - 30))
    db.commit(); db.close()


class Pairing(unittest.TestCase):
    def test_hostile_name_is_one_clean_line(self):
        with tempfile.TemporaryDirectory() as d:
            pairing(Path(d), {RID: {"user_name": "Eve‮\n\x1b[31mgnp.exe​ " + "x" * 200, "created_at": time.time(), "hash": "h", "salt": "s"}})
            it, = needs.pairing_items(Path(d))
            for bad in ("‮", "\n", "\x1b", "​"):
                self.assertNotIn(bad, it["title"])
            self.assertLess(len(it["title"]), 120)

    def test_future_timestamp_and_bool_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            pairing(Path(d), {"a": {"created_at": time.time() + 86400 * 365}, "b": {"created_at": True}})
            self.assertEqual(needs.pairing_items(Path(d)), [])

    def test_approve_command_only_for_hashed_request_ids(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            pairing(Path(d), {RID: {"user_name": "Ana", "created_at": now, "hash": "h", "salt": "s"},
                              "ABCD2345": {"user_name": "Old", "created_at": now}})
            items = {i["title"].split()[0]: i for i in needs.pairing_items(Path(d))}
            self.assertEqual(items["Ana"]["approve_command"], f"hermes pairing approve telegram {RID}")
            self.assertEqual(items["Old"]["approve_command"], "")   # a legacy key IS the code
            self.assertNotIn("ABCD2345", json.dumps(list(items.values())))
            pack = json.dumps(needs.pocket_pack({"items": list(items.values())}))
            self.assertNotIn(RID, pack)   # gadgets never get the id

    def test_profile_command_targets_the_profile(self):
        with tempfile.TemporaryDirectory() as d:
            pairing(Path(d), {RID: {"created_at": time.time(), "hash": "h", "salt": "s"}}, "discord", "ops")
            it, = needs.pairing_items(Path(d))
            self.assertEqual(it["approve_command"], f"hermes -p ops pairing approve discord {RID}")


class Chats(unittest.TestCase):
    def test_old_messages_schema_lists_chats_without_error_flood(self):
        with tempfile.TemporaryDirectory() as d:
            chat_db(Path(d) / "state.db", [(f"s{i}", "telegram", "1", "Me") for i in range(5)], legacy_messages=True)
            errors = []
            self.assertEqual(len(needs.chat_items(Path(d), errors=errors)), 5)
            self.assertEqual(errors, [])

    def test_home_path_with_uri_characters(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "we?ird #home%20"
            chat_db(home / "state.db", [("s", "cli", "1", "")])
            errors = []
            self.assertEqual(len(needs.chat_items(home, errors=errors)), 1, errors)

    def test_root_home_chat_has_a_human_name(self):
        with tempfile.TemporaryDirectory() as d:
            chat_db(Path(d) / "state.db", [("s", "cli", "1", "")])
            it, = needs.chat_items(Path(d))
            self.assertEqual(it["title"], "Your agent is asking you")

    def test_gateway_chat_says_who_it_is_with_and_owner_ids_filter(self):
        with tempfile.TemporaryDirectory() as d:
            chat_db(Path(d) / "state.db", [("me", "discord", "111", "simon"), ("them", "discord", "222", "Mallory‮"),
                                           ("local", "desktop", "999", "ignored")])
            items = {i["session_id"]: i for i in needs.chat_items(Path(d))}
            self.assertEqual(sorted(items), ["local", "me", "them"])
            self.assertEqual(items["them"]["chat_with"], "Mallory")
            self.assertEqual(items["local"]["chat_with"], "")
            self.assertEqual(items["me"]["chat_with"], "")   # "simon" is the principal: not worth saying
            SETTINGS["owner_user_ids"] = "111, 333"
            try:
                self.assertEqual(sorted(i["session_id"] for i in needs.chat_items(Path(d))), ["local", "me"])
            finally:
                SETTINGS.pop("owner_user_ids")

    def test_schema_without_user_columns(self):
        with tempfile.TemporaryDirectory() as d:
            chat_db(Path(d) / "state.db", [("s", "telegram", None, None)], user_cols=False)
            it, = needs.chat_items(Path(d))
            self.assertEqual(it["chat_with"], "")


class Waits(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory(); self.home = Path(self.d.name)

    def tearDown(self):
        self.d.cleanup()

    def test_wait_file_is_owner_only(self):
        needs.record_wait(self.home, request_id=RID, kind="sudo", prompt="password for simon")
        f = needs._waits_dir(self.home) / f"{RID}.json"
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)

    def test_malformed_and_ancient_waits_are_dropped_not_raised(self):
        w = needs._waits_dir(self.home)
        live = {"pid": os.getpid(), "proc_start": needs._proc_start(os.getpid())}
        (w / "aaaaaaaaaaaaaaaa.json").write_text(json.dumps({**live, "since": time.time()}))                       # no request_id
        (w / "bbbbbbbbbbbbbbbb.json").write_text(json.dumps({**live, "request_id": "../../x", "since": time.time()}))
        (w / "cccccccccccccccc.json").write_text(json.dumps({**live, "request_id": "cccccccccccccccc", "since": time.time() - 2 * 86400}))
        (w / "dddddddddddddddd.json").write_text(json.dumps({**live, "request_id": "dddddddddddddddd", "since": "soon"}))
        self.assertEqual(needs.wait_items(self.home), [])
        self.assertEqual(list(w.iterdir()), [])


class Ordering(unittest.TestCase):
    def test_urgent_sources_lead_whatever_the_board_priority(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            pairing(home, {RID: {"created_at": time.time(), "hash": "h", "salt": "s"}})
            chat_db(home / "state.db", [("s", "cli", "1", "")])
            needs.record_wait(home, request_id=RID, kind="clarify", prompt="Which?")
            card = {"id": "kanban:b:t", "version": "1", "source": "kanban", "priority": 540, "since": time.time(),
                    "title": "t", "ask": "a", "why": "w", "task_id": "t"}
            saved = needs.kanban_items
            needs.kanban_items = lambda boards=None, errors=None: [card]
            try:
                q = needs.build_queue(home)
            finally:
                needs.kanban_items = saved
            self.assertEqual([i["source"] for i in q["items"]], ["prompt", "pairing", "kanban", "chat"])
            self.assertEqual(needs.pocket_pack(q)["cards"][0]["id"], f"prompt:{RID}")


class Hidden(unittest.TestCase):
    def test_hide_rejects_oversized_and_caps_the_store(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            with self.assertRaises(ValueError):
                needs.hide("x" * 5000, "1", home)
            with self.assertRaises(ValueError):
                needs.hide("", "1", home)
            for i in range(needs._HIDDEN_KEEP + 5):
                needs.hide(f"chat:p:{i}", "1", home)
            hidden = needs.load_hidden(home)
            self.assertEqual(len(hidden), needs._HIDDEN_KEEP)
            self.assertIn(f"chat:p:{needs._HIDDEN_KEEP + 4}", hidden)
            self.assertNotIn("chat:p:0", hidden)


if __name__ == "__main__":
    unittest.main()
