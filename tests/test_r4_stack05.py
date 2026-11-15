"""Round-4 review findings on the filed-dedupe-scope branch (#46): key matching only trusts
records that are ours and readable, never guesses among several, and global keys resolve with
their scope."""

import json
import re
from unittest.mock import patch

from test_outside import ROOT, OutsideFixture, needs, plugin

LEGACY = {
    "act_where": "Mail", "dedupe_key": "k", "kind": "draft", "origin": "filed", "profile": "",
    "ref": "", "session_id": "s", "since": 1794724659.8, "source": "outside", "text": "B",
    "tier": "normal", "title": "T", "version": "v",
}


class Base(OutsideFixture):
    def file_as(self, profile, **args):
        base = {"kind": "draft", "title": "T", "body": "B", "act_where": "Mail"}
        return needs.file_outside({**base, **args}, self.home, profile=profile)

    def resolve_as(self, profile, **args):
        return needs.file_outside({"action": "resolve", **args}, self.home, profile=profile)

    @property
    def outside(self):
        d = needs.data_dir(self.home) / "outside"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def seed(self, name, **changes):
        rec = {**LEGACY, "id": f"outside:filed:{name}", **changes}
        (self.outside / f"{name}.json").write_text(json.dumps(rec))
        return rec


class GlobalKeysResolveWithTheirScope(Base):
    def test_a_global_key_is_not_found_by_a_profile_scoped_resolve(self):
        """05-001 (class I): the documented key-based clearing must name the scope."""
        self.file_as("work", dedupe_key="k", dedupe_scope="global")
        with self.assertRaises(LookupError):
            self.resolve_as("work", dedupe_key="k")
        self.assertTrue(self.resolve_as("work", dedupe_key="k", dedupe_scope="global")["resolved"])

    def test_readme_and_schema_tell_agents_to_pass_the_scope_when_resolving(self):
        readme = (ROOT / "README.md").read_text()
        line = next(x for x in readme.splitlines() if "dedupe_scope" in x and "resolve" in x)
        self.assertRegex(line, r"dedupe_scope: \\?\"global\\?\"` on the resolve call")
        schema = plugin.FILE_SCHEMA["parameters"]["properties"]["dedupe_scope"]["description"]
        self.assertIn("resolve", schema)


class UnreadableRecordsAreNotAbsent(Base):
    def test_an_unreadable_record_blocks_refiling_its_key(self):
        """05-002 (class G): a record that cannot be read may hold the key."""
        first = self.file_as("work", dedupe_key="k")
        path = self.outside / (first["id"].rsplit(":", 1)[1] + ".json")
        good = path.read_text()
        path.write_text("{not json")
        with self.assertRaises(RuntimeError):
            self.file_as("work", dedupe_key="k", body="again")
        with self.assertRaises(RuntimeError):
            self.resolve_as("work", dedupe_key="k")
        self.assertEqual(len(list(self.outside.glob("*.json"))), 1)
        path.write_text(good)
        again = self.file_as("work", dedupe_key="k", body="again")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(list(self.outside.glob("*.json"))), 1)


class OnlyOurRecordsMatch(Base):
    def test_a_stray_json_file_with_the_key_is_never_the_match(self):
        """05-003 (class M, record identity): the filename and id must be ours."""
        (self.outside / "backup.json").write_text(
            json.dumps({"dedupe_key": "k", "profile": "work", "dedupe_scope": "profile"})
        )
        out = self.file_as("work", dedupe_key="k")
        self.assertNotIn("backup", out["id"])
        self.assertTrue((self.outside / "backup.json").exists())
        self.assertTrue(self.resolve_as("work", dedupe_key="k")["resolved"])
        self.assertTrue((self.outside / "backup.json").exists())

    def test_a_stray_file_alone_is_not_resolvable_by_key(self):
        (self.outside / "backup.json").write_text(
            json.dumps({"dedupe_key": "k", "profile": "work", "dedupe_scope": "profile"})
        )
        with self.assertRaises(LookupError):
            self.resolve_as("work", dedupe_key="k")
        self.assertTrue((self.outside / "backup.json").exists())

    def test_a_record_whose_id_names_another_file_is_never_the_match(self):
        name = "a" * 32
        self.seed(name, id="outside:filed:" + "b" * 32, dedupe_scope="global")
        out = self.file_as("work", dedupe_key="k", dedupe_scope="global")
        self.assertNotEqual(out["id"], "outside:filed:" + name)
        self.assertEqual(len(list(self.outside.glob("*.json"))), 2)


class AmbiguousKeysAreRefused(Base):
    def test_two_legacy_records_with_one_global_key_refuse_file_and_resolve(self):
        """05-004 (class M, ambiguity): never overwrite or delete whichever came first."""
        self.seed("a" * 32)
        self.seed("b" * 32)
        before = sorted(p.read_text() for p in self.outside.glob("*.json"))
        for call in (
            lambda: self.file_as("work", dedupe_key="k", dedupe_scope="global", title="New"),
            lambda: self.resolve_as("work", dedupe_key="k", dedupe_scope="global"),
            lambda: self.file_as(None, dedupe_key="k", title="New"),
        ):
            with self.assertRaises(ValueError) as caught:
                call()
            self.assertIn("2 ", str(caught.exception))
        self.assertEqual(sorted(p.read_text() for p in self.outside.glob("*.json")), before)

    def test_two_records_in_one_profile_scope_refuse_file_and_resolve(self):
        for name in ("c" * 32, "d" * 32):
            self.seed(name, profile="work", dedupe_scope="profile")
        before = sorted(p.read_text() for p in self.outside.glob("*.json"))
        with self.assertRaises(ValueError):
            self.file_as("work", dedupe_key="k", title="New")
        with self.assertRaises(ValueError):
            self.resolve_as("work", dedupe_key="k")
        self.assertEqual(sorted(p.read_text() for p in self.outside.glob("*.json")), before)

    def test_one_match_per_scope_still_works(self):
        a = self.file_as("work", dedupe_key="k")
        b = self.file_as("home", dedupe_key="k")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(self.file_as("work", dedupe_key="k", body="x")["id"], a["id"])


class ResolveSurvivesAMarkerCleanupFailure(Base):
    def test_a_failed_hidden_write_after_the_record_is_gone_still_reports_resolved(self):
        """01-072 (class G): the record is deleted; the item must not be reported unresolved."""
        a = self.file_as("work", dedupe_key="k")
        needs.hide(a["id"], a["version"], self.home)
        real = needs._write_json
        hidden = needs._hidden_path(self.home)

        def failing(path, data, **kw):
            if path == hidden:
                raise OSError("disk full")
            return real(path, data, **kw)

        with patch.object(needs, "_write_json", failing):
            out = self.resolve_as("work", id=a["id"], version=a["version"])
        self.assertTrue(out["resolved"])
        self.assertFalse((self.outside / (a["id"].rsplit(":", 1)[1] + ".json")).exists())
        # the stale marker is harmless and a retry reports the record gone, not an error loop
        with self.assertRaises(LookupError):
            self.resolve_as("work", id=a["id"], version=a["version"])
