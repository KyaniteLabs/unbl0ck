"""Round-4 findings on the Ask-detection branch (#40): bad settings and stored values, what the
chat reader will open, which profile name a shared database keeps, and what is read from
profile.yaml. Every test that builds the queue isolates HERMES_KANBAN_HOME."""

import os
import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

from r4c_common import ASK, HomeCase, chat_db, needs


class QueueCase(HomeCase):
    def setUp(self):
        super().setUp()
        kanban = self.base / "kanban"
        kanban.mkdir()
        p = patch.dict(os.environ, {"HERMES_HOME": str(self.home), "HERMES_KANBAN_HOME": str(kanban)})
        p.start()
        self.addCleanup(p.stop)


class WindowSetting(QueueCase):
    def test_digit_strings_that_cannot_convert_use_the_default(self):
        """11-001 (class M, invalid input): isdigit() accepts what int() rejects."""
        for raw in ("²", "9" * 5000, "٣", "１２", "1" * 7, " ٤ "):
            with self.subTest(raw=raw[:8]):
                self.settings["chat_window_days"] = raw
                self.assertEqual(needs.chat_window_days(), 14)

    def test_the_documented_range_is_one_to_ninety_and_anything_else_is_fourteen(self):
        """11-008 (class I): plugin.yaml promises 1 to 90 and 14 otherwise."""
        for raw, want in [(1, 1), (90, 90), ("90", 90), (91, 14), (500, 14), (0, 14), ("500", 14), (True, 14)]:
            with self.subTest(raw=raw):
                self.settings["chat_window_days"] = raw
                self.assertEqual(needs.chat_window_days(), want)

    def test_the_queue_builds_with_an_unconvertible_window_setting(self):
        """11-001: not_covered_notes() ran the same conversion outside the chat handler."""
        chat_db(self.home / "state.db", [("s1", "cli", "u", ASK, "dm")])
        for raw in ("²", "9" * 5000):
            with self.subTest(raw=raw[:4]):
                self.settings["chat_window_days"] = raw
                queue = needs.build_queue(self.home)
                self.assertEqual(queue["coverage"]["chat"]["window_days"], 14)
                self.assertEqual([i["session_id"] for i in queue["items"]], ["s1"])
                self.assertFalse(any(e.startswith("chat") for e in queue["errors"]), queue["errors"])

    def test_not_covered_notes_accepts_a_missing_chat_coverage_with_a_bad_setting(self):
        self.settings["chat_window_days"] = "²"
        self.assertTrue(needs.not_covered_notes(None))

    def test_infinite_numbers_in_other_inputs_do_not_raise(self):
        """Sweep (class M): int(inf) raises OverflowError, which three handlers did not catch."""
        inf = float("inf")
        self.assertEqual(needs.ask_history_turns({"ask_history_turns": inf}), needs.ASK_HISTORY_TURNS)
        data = {f"k{i}": {"at": inf if i == 0 else i, "version": "v"} for i in range(needs._BLUF_KEEP + 5)}
        self.assertIsInstance(needs._trim_blufs(data), dict)
        needs.wait_state(Path("w.json"), {"pid": inf, "request_id": "0123456789abcdef"})

    def test_an_infinite_stored_message_time_does_not_fail_the_chat_source(self):
        """Sweep (class M): int(ts) on a stored Infinity failed every chat in the profile."""
        chat_db(self.home / "state.db", [("a", "cli", "u", ASK, "dm"), ("b", "cli", "u", ASK, "dm")])
        db = sqlite3.connect(self.home / "state.db")
        db.execute("UPDATE messages SET timestamp = 1e999 WHERE session_id = 'a'")
        db.commit()
        db.close()
        errors: list[str] = []
        ids = sorted(i["session_id"] for i in needs.chat_items(self.home, errors=errors))
        self.assertEqual(errors, [])
        self.assertIn("b", ids)


class WhatIsOpened(QueueCase):
    def _run_with_deadline(self, fn, seconds=5.0):
        box = {}

        def work():
            box["value"] = fn()

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(seconds)
        return t, box

    def test_a_fifo_in_place_of_a_database_is_refused_not_opened(self):
        """11-002 (class M, blocking special file): opening a writerless FIFO blocks forever."""
        (self.home / "profiles" / "work").mkdir(parents=True)
        fifo = self.home / "profiles" / "work" / "state.db"
        os.mkfifo(fifo)

        def release():
            try:
                fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                os.close(fd)
            except OSError:
                pass

        self.addCleanup(release)
        t, box = self._run_with_deadline(lambda: needs.build_queue(self.home))
        self.assertFalse(t.is_alive(), "the queue build blocked on a FIFO")
        queue = box["value"]
        self.assertTrue(any("chat[work]" in e and "not a regular file" in e for e in queue["errors"]), queue["errors"])
        self.assertTrue(queue["coverage"]["chat"]["failed"])

    def test_a_fifo_as_the_default_database_is_refused_too(self):
        os.mkfifo(self.home / "state.db")
        self.addCleanup(lambda: os.path.exists(self.home / "state.db") and self._poke(self.home / "state.db"))
        errors: list[str] = []
        t, box = self._run_with_deadline(lambda: needs._state_dbs(self.home, errors))
        self.assertFalse(t.is_alive())
        self.assertEqual(box["value"], [])
        self.assertTrue(any("chat[default]" in e for e in errors), errors)

    @staticmethod
    def _poke(path):
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass

    def test_a_database_that_cannot_be_inspected_is_a_failed_source(self):
        """11-003 (class G): only genuine absence is an optional missing source."""
        chat_db(self.home / "profiles" / "work" / "state.db")
        real = os.lstat

        def deny(path, *a, **k):
            if str(path).endswith(os.path.join("work", "state.db")):
                raise PermissionError(13, "denied")
            return real(path, *a, **k)

        with patch.object(needs.os, "lstat", deny):
            queue = needs.build_queue(self.home)
        self.assertTrue(any("chat[work]" in e and "could not be inspected" in e for e in queue["errors"]), queue["errors"])
        self.assertTrue(queue["coverage"]["chat"]["failed"])
        self.assertNotIn("PermissionError: denied", " ".join(queue["errors"]))  # no path or message echoed

    def test_a_missing_database_is_not_an_error(self):
        (self.home / "profiles" / "empty").mkdir(parents=True)
        (self.home / "profiles" / "afile").write_text("x")
        errors: list[str] = []
        self.assertEqual(needs._state_dbs(self.home, errors), [])
        self.assertEqual(errors, [])


class SharedDatabaseKeepsItsName(QueueCase):
    def _two_names(self):
        chat_db(self.home / "profiles" / "work" / "state.db", [("s", "cli", "u", ASK, "dm")])
        return self.home / "profiles" / "work" / "state.db"

    def _alias(self, name="alias"):
        (self.home / "profiles" / name).mkdir(parents=True)
        os.link(self.home / "profiles" / "work" / "state.db", self.home / "profiles" / name / "state.db")

    def test_a_dismissal_survives_an_alphabetically_earlier_alias(self):
        """11-004 (class A, unstable identity): the alias must not rename the chat and resurface it."""
        self._two_names()
        first = needs.build_queue(self.home)
        item = first["items"][0]
        self.assertEqual(item["id"], "chat:work:s")
        needs.hide(item["id"], item["version"], self.home)
        self.assertEqual(needs.build_queue(self.home)["items"], [])
        self._alias("alias")
        after = needs.build_queue(self.home)
        self.assertEqual([i["id"] for i in after["items"]], [])
        self.assertEqual(after["hidden"], 1)

    def test_a_cached_summary_keeps_its_profile_too(self):
        self._two_names()
        item = needs.build_queue(self.home)["items"][0]
        needs.save_bluf(item["id"], item["version"], "Yes or no", "why", self.home)
        self._alias("alias")
        ids = [i["id"] for i in needs.build_queue(self.home)["items"]]
        self.assertEqual(ids, ["chat:work:s"])

    def test_without_any_state_the_first_listed_name_is_used_once(self):
        self._two_names()
        self._alias("alias")
        names = [n for n, _ in needs._state_dbs(self.home)]
        self.assertEqual(names, ["alias"])
        self.assertEqual(len(needs.chat_items(self.home)), 1)

    def test_two_different_databases_are_both_read(self):
        self._two_names()
        chat_db(self.home / "profiles" / "alias" / "state.db", [("t", "cli", "u", ASK, "dm")])
        self.assertEqual(sorted(i["id"] for i in needs.chat_items(self.home)), ["chat:alias:t", "chat:work:s"])


class ProfileYamlRead(HomeCase):
    TITLE = "ui_meta:\n  hermes-bots:\n    title: {t}\n"

    def setUp(self):
        super().setUp()
        needs._BOT_NAME_CACHE.clear()
        self.addCleanup(needs._BOT_NAME_CACHE.clear)
        self.prof = self.home / "profiles" / "work"
        self.prof.mkdir(parents=True)
        self.yaml = self.prof / "profile.yaml"
        self.yaml.write_text(self.TITLE.format(t="Small"))

    def _swap_after_lstat(self, swap):
        real = Path.lstat
        done = []

        def lstat(path, *a, **k):
            out = real(path, *a, **k)
            if path.name == "profile.yaml" and not done:
                done.append(1)
                swap()
            return out

        return patch.object(Path, "lstat", lstat)

    def test_a_link_swapped_in_after_the_check_is_not_read(self):
        """11-005 (class A, check/open race): the read is bound to the opened descriptor."""
        secret = self.base / "elsewhere.yaml"
        secret.write_text(self.TITLE.format(t="Leaked"))

        def swap():
            self.yaml.unlink()
            self.yaml.symlink_to(secret)

        with self._swap_after_lstat(swap):
            self.assertEqual(needs._bot_name(self.home, "work"), "work")
        self.assertNotIn(str(self.yaml), needs._BOT_NAME_CACHE)

    def test_an_oversized_file_swapped_in_after_the_check_is_not_read(self):
        def swap():
            self.yaml.write_text(self.TITLE.format(t="Big") + "# pad\n" * 20000)

        with self._swap_after_lstat(swap):
            self.assertEqual(needs._bot_name(self.home, "work"), "work")

    def test_a_file_swapped_after_the_check_is_cached_under_the_signature_it_was_read_with(self):
        other = self.prof / "other.yaml"
        other.write_text(self.TITLE.format(t="Replaced title that is longer"))

        def swap():
            os.replace(other, self.yaml)

        with self._swap_after_lstat(swap):
            self.assertEqual(needs._bot_name(self.home, "work"), "Replaced title that is longer")
        st = self.yaml.stat()
        sig = needs._BOT_NAME_CACHE[str(self.yaml)][0]
        self.assertEqual(sig, (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns))

    def test_a_plain_small_file_is_still_read(self):
        self.assertEqual(needs._bot_name(self.home, "work"), "Small")
