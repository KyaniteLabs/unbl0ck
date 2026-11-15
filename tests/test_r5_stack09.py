"""Round-5 regression tests for the surface matrix (issue #47).

Each test reproduces a round-5 review finding with real files and was seen to fail against the code
before its fix. Siblings are loaded by file path so the tests also run under a shadowing ``tests``
package."""

import importlib.util
import os
import sys
import time
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent


def _sibling(name):
    spec = importlib.util.spec_from_file_location(f"ub_r5s09_{name}", _HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


outside = _sibling("test_outside")
needs = outside.needs

# File names that happen to contain the wording of a queue notice.
NOTICE_LIKE_NAMES = (
    "could not remove.txt",
    "retention cleanup.txt",
    "invalid done_at.txt",
    "hidden files are not listed.txt",
    "folder limit, showing the newest.txt",
    "filed entries; only the first.txt",
    "agent-filed items are waiting (limit.txt",
)


@unittest.skipIf(os.geteuid() == 0, "a root user can read a mode-000 file")
class NoticeWordsInNamesAreStillReadFailures(outside.OutsideFixture):
    """R5-needs-032 (class G): a file name or exception text is not a notice."""

    def make(self, name, age=0):
        f = self.folder / name
        f.write_text("Review this")
        t = time.time() - age
        os.utime(f, (t, t))
        return f

    def test_an_unreadable_file_named_like_a_notice_is_an_unread_source(self):
        for name in NOTICE_LIKE_NAMES:
            with self.subTest(name=name):
                for old in self.folder.iterdir():
                    old.unlink()
                self.watch()
                bad = self.make(name)
                bad.chmod(0)
                try:
                    q = needs.build_queue()
                finally:
                    bad.chmod(0o600)
                self.assertTrue(any(name in e for e in q["errors"]), q["errors"])
                pack = needs.pocket_pack(q)
                self.assertGreaterEqual(pack["omitted"]["unread_sources"], 1, q["errors"])
                self.assertFalse(pack["complete"], "an empty pack must not claim to be complete")

    def test_the_real_notices_are_still_not_unread_sources(self):
        notices = [
            "outside: could not remove 2 expired done record(s); their text stays until a later cleanup succeeds",
            "outside: retention cleanup stopped early; some filed records were not checked",
            "outside: retention cleanup failed: OSError",
            "outside: retention cleanup did not run: OSError; it retries on the next poll",
            "outside: odd: name.json: invalid done_at; kept for repair",
            "outside: 200 agent-filed items are waiting (limit 200); agents cannot file more until you finish some",
            "prompt: abc.json: OSError; could not remove stale record",
        ]
        self.assertEqual(needs.unread_sources(notices), 0)
        cuts = [
            "outside: exports: folder limit, showing the newest 200; 3 older files are waiting. Finish or hide items to bring them into view",
            "outside: more than 5000 filed entries; only the first 5000 are read",
            "outside: 4 hidden files are not listed (at most the newest 200 per folder are shown)",
        ]
        self.assertEqual(needs.unread_sources(cuts), 0)
        self.assertTrue(all(needs.listing_cut([c]) for c in cuts))
        self.assertFalse(needs.listing_cut(notices))

    def test_exception_text_with_notice_words_is_a_read_failure(self):
        errors = [
            "outside: a.txt: OSError: could not remove",
            "kanban[main]: OperationalError: retention cleanup stopped",
            "chat[p]: OSError: invalid done_at; kept for repair",
        ]
        self.assertEqual(needs.unread_sources(errors), 3)
        self.assertFalse(needs.listing_cut(["outside: a.txt: OSError: hidden files are not listed"]))


if __name__ == "__main__":
    unittest.main()
