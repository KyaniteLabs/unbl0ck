"""Shared test hygiene.

Loading ``dashboard/plugin_api.py`` starts the Ask housekeeping timer at import. Many test files
load it and only one used to stop it, so each load left a live timer thread behind that kept
touching a profile directory after its fixture had been removed."""

import sys

import pytest


@pytest.fixture(autouse=True)
def _stop_ask_housekeeping_timers():
    yield
    for module in list(sys.modules.values()):
        table = getattr(module, "_ASK_JOBS", None)
        stop = getattr(table, "stop_background", None)
        if callable(stop):
            try:
                stop()
            except Exception:  # noqa: BLE001 - teardown must never fail a test
                pass
