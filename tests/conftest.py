"""Shared test helpers."""

import contextlib
import os
from unittest.mock import patch


def ask_gen(table, job):
    """The generation token of ``job`` in an AskJobs table (None when it is not there)."""
    return next((st["gen"] for (_d, j), st in table._jobs.items() if j == job), None)


@contextlib.contextmanager
def checks_see_a_regular_file(names, regular):
    """Every no-follow stat of a path named in ``names`` reports ``regular`` (a real small file),
    as if the entry was swapped for a link or FIFO right after the check: only a reader that
    judges what it actually opened refuses it."""
    real_lstat, real_stat, fake = os.lstat, os.stat, os.lstat(regular)

    def named(p):
        return os.path.basename(os.fsdecode(p)) in names if isinstance(p, (str, bytes, os.PathLike)) else False

    def lstat(p, *a, **k):
        return fake if named(p) else real_lstat(p, *a, **k)

    def stat_(p, *a, follow_symlinks=True, **k):
        if not follow_symlinks and named(p):
            return fake
        return real_stat(p, *a, follow_symlinks=follow_symlinks, **k)

    with patch.object(os, "lstat", lstat), patch.object(os, "stat", stat_):
        yield
