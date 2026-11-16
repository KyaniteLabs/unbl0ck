"""The plugin's shipped Python modules, read from the tree so a module split out of needs.py is
covered by every check that walks them, without anyone editing a list."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def plugin_modules() -> list[Path]:
    """Every Python module Hermes can load from the plugin: the top level and dashboard/."""
    return sorted(ROOT.glob("*.py")) + sorted((ROOT / "dashboard").glob("*.py"))
