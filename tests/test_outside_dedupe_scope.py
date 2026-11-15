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
        self.assertTrue(self.call("home", action="resolve", id=a["id"])["resolved"])
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
        self.call("work", dedupe_key="k", body="two")
        self.assertTrue(self.call("work", action="resolve", id=a["id"])["resolved"])
        self.assertFalse(self.call("work", action="resolve", id=a["id"])["ok"])
        # a late update after resolve files a fresh item rather than reviving a stale id
        fresh = self.call("work", dedupe_key="k", body="three")
        self.assertNotEqual(fresh["id"], a["id"])


if __name__ == "__main__":
    unittest.main()
