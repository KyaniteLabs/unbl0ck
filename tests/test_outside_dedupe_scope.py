"""Filed dedupe keys are profile-scoped by default; sharing across profiles is explicit."""

import json
import unittest
from unittest.mock import patch

from test_outside import OutsideFixture, needs


class DedupeScope(OutsideFixture):
    def call(self, profile, **args):
        base = {"kind": "draft", "title": "T", "body": "B", "act_where": "Mail"}
        if args.get("action") == "resolve":
            base = {}
        kwargs = {"session_id": "s"}
        if profile is not None:
            kwargs["profile"] = profile
        return json.loads(self.tools["unbl0ck_file"][2]({**base, **args}, **kwargs))

    def records(self):
        d = needs.data_dir(self.home) / "outside"
        return [json.loads(f.read_text()) for f in d.glob("*.json")]

    def test_same_key_in_two_profiles_stays_two_items_by_default(self):
        a = self.call("work", dedupe_key="k", title="Work")
        b = self.call("home", dedupe_key="k", title="Home")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual((a["dedupe_scope"], b["dedupe_scope"]), ("profile", "profile"))
        self.assertEqual({r["title"] for r in self.records()}, {"Work", "Home"})
        # each profile updates only its own
        self.call("work", dedupe_key="k", title="Work 2")
        self.assertEqual({r["title"] for r in self.records()}, {"Work 2", "Home"})

    def test_profile_resolve_by_key_never_touches_another_profiles_item(self):
        self.call("work", dedupe_key="k", title="Work")
        home = self.call("home", dedupe_key="k", title="Home")
        out = self.call("work", action="resolve", dedupe_key="k")
        self.assertTrue(out["resolved"])
        self.assertEqual([r["title"] for r in self.records()], ["Home"])
        gone = self.call("work", action="resolve", dedupe_key="k")
        self.assertFalse(gone["ok"])
        self.assertEqual([r["id"] for r in self.records()], [home["id"]])

    def test_explicit_global_scope_shares_one_record_across_profiles(self):
        a = self.call("work", dedupe_key="shared", dedupe_scope="global", title="One")
        b = self.call("home", dedupe_key="shared", dedupe_scope="global", title="Two")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(a["dedupe_scope"], "global")
        (rec,) = self.records()
        self.assertEqual((rec["title"], rec["profile"], rec["dedupe_scope"]), ("Two", "home", "global"))
        self.assertTrue(self.call("work", action="resolve", dedupe_key="shared", dedupe_scope="global")["resolved"])
        self.assertEqual(self.records(), [])

    def test_global_and_profile_scopes_do_not_collide(self):
        g = self.call("work", dedupe_key="k", dedupe_scope="global")
        p = self.call("work", dedupe_key="k")
        self.assertNotEqual(g["id"], p["id"])
        self.assertEqual(len(self.records()), 2)

    def test_legacy_record_with_no_stored_profile_is_adopted_once(self):
        old = self.call("work", dedupe_key="k", title="Old")
        path = self.record(old)
        rec = json.loads(path.read_text())
        rec.pop("dedupe_scope")
        rec["profile"] = ""  # tool calls before profile resolution stored nothing
        path.write_text(json.dumps(rec))
        again = self.call("work", dedupe_key="k", title="Updated")
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["id"], old["id"])
        saved = json.loads(path.read_text())
        self.assertEqual((saved["profile"], saved["dedupe_scope"], saved["title"]), ("work", "profile", "Updated"))
        other = self.call("home", dedupe_key="k", title="Home")
        self.assertNotEqual(other["id"], old["id"])
        self.assertEqual({r["title"] for r in self.records()}, {"Updated", "Home"})

    def test_runtime_profile_adopts_a_profileless_legacy_key(self):
        old = self.call(None, dedupe_key="k", title="Old")
        path = self.record(old)
        rec = json.loads(path.read_text())
        rec.pop("dedupe_scope")
        rec["profile"] = ""
        path.write_text(json.dumps(rec))
        again = self.call(None, dedupe_key="k", title="Updated")
        self.assertEqual(again["id"], old["id"])
        self.assertEqual(json.loads(path.read_text())["profile"], "default")

    def test_two_profileless_legacy_records_are_not_guessed(self):
        first = self.call("work", dedupe_key="k", title="One")
        second = self.call("home", dedupe_key="k", title="Two")
        for item in (first, second):
            path = self.record(item)
            rec = json.loads(path.read_text())
            rec.pop("dedupe_scope")
            rec["profile"] = ""
            path.write_text(json.dumps(rec))
        out = self.call("work", dedupe_key="k", title="Nope")
        self.assertFalse(out["ok"])
        self.assertIn("legacy", out["error"])
        self.assertEqual({r["title"] for r in self.records()}, {"One", "Two"})

    # Written verbatim by the 0.6.0 implementation, which stored no profile for tool calls.
    BASE_RECORD = {
        "act_where": "Mail", "dedupe_key": "k", "id": "outside:filed:048e813bf75942aba82ac44877fcd4f0",
        "kind": "draft", "origin": "filed", "profile": "", "ref": "", "session_id": "s", "since": 1794724659.8,
        "source": "outside", "text": "B", "tier": "normal", "title": "T",
        "version": "e73f46f6212b1ce53f308a7eb2cff945b890511d623bb5e86956635b769095f7",
    }

    def seed_base_record(self):
        d = needs.data_dir(self.home) / "outside"
        d.mkdir(parents=True, exist_ok=True)
        (d / "048e813bf75942aba82ac44877fcd4f0.json").write_text(json.dumps(self.BASE_RECORD))
        return self.BASE_RECORD["id"], self.BASE_RECORD["version"]

    def test_ownerless_base_record_keeps_identity_and_done_state_on_an_unchanged_refile(self):
        item_id, version = self.seed_base_record()
        needs.done_outside(item_id, version)
        again = self.call("work", dedupe_key="k", title="T", body="B", act_where="Mail")
        self.assertEqual((again["id"], again["version"]), (item_id, version))
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(needs.build_queue()["items"], [])
        later = self.call("work", dedupe_key="k", title="T", body="B", act_where="Mail")
        self.assertEqual(later["version"], version)
        self.assertEqual(self.records()[0]["profile"], "work")

    def test_ownerless_base_record_keeps_not_for_me_and_resolves_by_key(self):
        item_id, version = self.seed_base_record()
        needs.hide(item_id, version)
        self.call("work", dedupe_key="k", title="T", body="B", act_where="Mail")
        self.assertEqual(needs.build_queue()["items"], [])
        gone = self.call("work", action="resolve", dedupe_key="k")
        self.assertTrue(gone["resolved"], gone)
        self.assertEqual(self.records(), [])

    def test_changed_content_on_an_ownerless_record_is_new_work(self):
        item_id, version = self.seed_base_record()
        needs.done_outside(item_id, version)
        changed = self.call("work", dedupe_key="k", title="T", body="Different", act_where="Mail")
        self.assertEqual(changed["id"], item_id)
        self.assertNotEqual(changed["version"], version)
        self.assertEqual(len(needs.build_queue()["items"]), 1)

    def test_legacy_record_is_adopted_by_its_own_profile_without_resurfacing(self):
        old = self.call("work", dedupe_key="k")
        path = self.record(old)
        rec = json.loads(path.read_text())
        del rec["dedupe_scope"]  # written before scopes existed
        path.write_text(json.dumps(rec))
        item = needs.build_queue()["items"][0]
        needs.done_outside(item["id"], item["version"])
        again = self.call("work", dedupe_key="k")  # identical claim, same profile
        self.assertEqual(again["id"], old["id"])
        self.assertEqual(again["version"], old["version"])
        self.assertEqual(needs.build_queue()["items"], [])  # still Done, not resurfaced
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["dedupe_scope"], "profile")

    def test_legacy_record_is_not_replaced_by_another_profile(self):
        old = self.call("work", dedupe_key="k", title="Work")
        path = self.record(old)
        rec = json.loads(path.read_text())
        del rec["dedupe_scope"]
        path.write_text(json.dumps(rec))
        other = self.call("home", dedupe_key="k", title="Home")
        self.assertNotEqual(other["id"], old["id"])
        self.assertEqual({r["title"] for r in self.records()}, {"Work", "Home"})
        # a legacy global key still updates in place for a global caller
        shared = self.call("home", dedupe_key="k", dedupe_scope="global", title="Shared")
        self.assertEqual(shared["id"], old["id"])

    def test_caller_without_a_profile_kwarg_uses_the_runtime_profile_not_global(self):
        a = self.call(None, dedupe_key="k")
        self.assertEqual(a["dedupe_scope"], "profile")
        self.assertEqual(self.records()[0]["profile"], "default")
        self.assertEqual(self.call(None, dedupe_key="k", title="Again")["id"], a["id"])
        other = self.call("work", dedupe_key="k")
        self.assertNotEqual(other["id"], a["id"])
        self.assertTrue(self.call(None, action="resolve", dedupe_key="k")["resolved"])
        self.assertEqual([r["profile"] for r in self.records()], ["work"])

    def test_native_dispatch_kwargs_without_profile_keep_two_profiles_apart(self):
        # Hermes passes task_id, session_id and user_task, never `profile`. The runtime
        # profile comes from the process' Hermes home, the way record_wait() resolves it.
        native = {"task_id": "t", "session_id": "s", "user_task": "do it"}
        args = {"kind": "draft", "title": "T", "body": "B", "act_where": "Mail", "dedupe_key": "k"}
        ids = {}
        for profile in ("work", "home"):
            ph = self.home / "profiles" / profile
            ph.mkdir(parents=True)
            with patch.dict("os.environ", {"HERMES_HOME": str(ph)}):
                out = json.loads(self.tools["unbl0ck_file"][2]({**args, "title": profile}, **native))
            self.assertTrue(out["ok"], out)
            ids[profile] = out["id"]
        self.assertNotEqual(ids["work"], ids["home"])
        self.assertEqual({(r["profile"], r["title"]) for r in self.records()}, {("work", "work"), ("home", "home")})
        ph = self.home / "profiles" / "work"
        with patch.dict("os.environ", {"HERMES_HOME": str(ph)}):
            out = json.loads(self.tools["unbl0ck_file"][2]({"action": "resolve", "dedupe_key": "k"}, **native))
        self.assertTrue(out["resolved"])
        self.assertEqual([r["profile"] for r in self.records()], ["home"])
        # an explicit dedupe_scope of profile now validates without a profile kwarg
        with patch.dict("os.environ", {"HERMES_HOME": str(ph)}):
            ok = json.loads(self.tools["unbl0ck_file"][2]({**args, "dedupe_scope": "profile"}, **native))
        self.assertTrue(ok["ok"], ok)

    def test_scope_validation(self):
        self.assertFalse(self.call("work", dedupe_key="k", dedupe_scope="everyone")["ok"])
        self.assertTrue(self.call(None, dedupe_key="k", dedupe_scope="profile")["ok"])  # runtime profile is known

    def test_resolve_by_id_is_unambiguous_across_profiles(self):
        a = self.call("work", dedupe_key="k")
        b = self.call("home", dedupe_key="k")
        self.assertTrue(self.call("home", action="resolve", id=a["id"], version=a["version"])["resolved"])
        self.assertEqual([r["id"] for r in self.records()], [b["id"]])

    def test_scope_is_provenance_on_the_queue_item(self):
        self.call("work", dedupe_key="k")
        (item,) = needs.build_queue()["items"]
        self.assertEqual((item["profile"], item["dedupe_scope"]), ("work", "profile"))

    def test_cap_counts_profile_scoped_items_and_updates_still_work(self):
        with patch.object(needs, "_OUTSIDE_LIMIT", 2):
            self.call("work", dedupe_key="k")
            self.call("home", dedupe_key="k")
            self.assertFalse(self.call("other", dedupe_key="k")["ok"])
            self.assertTrue(self.call("home", dedupe_key="k", body="new")["ok"])
            self.assertEqual(len(self.records()), 2)

    def test_update_then_resolve_race_resolves_the_updated_record_once(self):
        a = self.call("work", dedupe_key="k", body="one")
        b = self.call("work", dedupe_key="k", body="two")
        # the version from before the update no longer matches: the updated record survives
        self.assertFalse(self.call("work", action="resolve", id=a["id"], version=a["version"])["ok"])
        self.assertTrue(self.call("work", action="resolve", id=a["id"], version=b["version"])["resolved"])
        self.assertFalse(self.call("work", action="resolve", id=a["id"], version=b["version"])["ok"])
        # a late update after resolve files a fresh item rather than reviving a stale id
        fresh = self.call("work", dedupe_key="k", body="three")
        self.assertNotEqual(fresh["id"], a["id"])

    # Class sweep: resolve cleans its own dismissal marker; a long key is refused, not cut.

    def test_resolve_drops_the_not_for_me_marker_of_the_resolved_record(self):
        a = self.call("work", dedupe_key="k")
        needs.hide(a["id"], a["version"], self.home)
        self.assertIn(a["id"], needs.load_hidden(self.home))
        self.assertTrue(self.call("work", action="resolve", id=a["id"], version=a["version"])["resolved"])
        self.assertNotIn(a["id"], needs.load_hidden(self.home))

    def test_resolve_leaves_other_dismissals_alone(self):
        a = self.call("work", dedupe_key="k")
        needs.hide(a["id"], a["version"], self.home)
        needs.hide("outside:folder:" + "a" * 64, "v", self.home)
        self.call("work", action="resolve", id=a["id"], version=a["version"])
        self.assertEqual(list(needs.load_hidden(self.home)), ["outside:folder:" + "a" * 64])

    def test_resolve_with_a_corrupt_hidden_file_still_resolves_and_keeps_the_file(self):
        a = self.call("work", dedupe_key="k")
        path = needs._hidden_path(self.home)
        path.write_text("{not json")
        self.assertTrue(self.call("work", action="resolve", id=a["id"], version=a["version"])["resolved"])
        self.assertEqual(path.read_text(), "{not json")

    def test_two_long_keys_that_share_a_prefix_are_refused_not_merged(self):
        prefix = "k" * 120
        first = self.call("work", dedupe_key=prefix)
        self.assertTrue(first["ok"])
        second = self.call("work", dedupe_key=prefix + "-other", title="Other")
        self.assertFalse(second["ok"])
        self.assertIn("120", second["error"])
        self.assertEqual([r["title"] for r in self.records()], ["T"])


if __name__ == "__main__":
    unittest.main()
