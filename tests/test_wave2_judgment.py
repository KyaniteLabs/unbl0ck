"""Wave 2: sound is off for a fresh profile (U9) and org role words live only in
one config default (U8)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import needs

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "desktop" / "src" / "plugin.src.js"
NODE = shutil.which("node")


def _sfx_block():
    s = SRC.read_text()
    a = s.index("let sfxOnNow")
    return s[a : s.index("// ── X55 draw_ascii_field")]


def _run_sfx(stored):
    harness = f"""
let made = 0
globalThis.window = {{ AudioContext: function () {{ made++; this.state = 'running'; this.createBuffer = () => ({{ copyToChannel() {{}} }}) }} }}
const store = {json.dumps(stored)}
globalThis.localStorage = {{ getItem: k => (k in store ? store[k] : null), setItem: (k, v) => {{ store[k] = v }} }}
const atom = v => ({{ set() {{}}, get: () => v }})
const SFX_SPECS = {{ ok: () => new Float32Array(4) }}, SFX_RATE = 22050
{_sfx_block()}
sfx.boot(); sfx.boot()
const afterGesture = made
setSfx(true); sfx.boot()
console.log(JSON.stringify({{ afterGesture, afterToggle: made }}))
"""
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", harness],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_u9_fresh_profile_creates_no_audio_context_until_the_toggle():
    r = _run_sfx({})
    assert r["afterGesture"] == 0 and r["afterToggle"] == 1


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_u9_an_existing_on_preference_is_respected():
    assert _run_sfx({"unbl0ck.sfx": "on"})["afterGesture"] == 1


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_u9_an_existing_off_preference_stays_off():
    assert _run_sfx({"unbl0ck.sfx": "off"})["afterGesture"] == 0


def test_u9_source_and_build_default_to_off():
    for f in (SRC, ROOT / "desktop" / "plugin.js"):
        t = f.read_text()
        assert "let sfxOnNow = false" in t and "=== 'on'" in t


# --- U8
ROLE_WORD = re.compile(r"\bCEO\b", re.IGNORECASE)
SHIPPED = [
    "needs.py",
    "jobs.py",
    "__init__.py",
    "plugin.yaml",
    "README.md",
    "dashboard/plugin_api.py",
    "desktop/src/plugin.src.js",
    "desktop/plugin.js",
]


def test_u8_role_words_appear_only_in_the_config_default():
    hits = []
    for rel in SHIPPED:
        p = ROOT / rel
        if not p.exists():
            continue
        for n, line in enumerate(p.read_text().splitlines(), 1):
            if ROLE_WORD.search(line) and "role-words default" not in line:
                hits.append(f"{rel}:{n}")
    assert hits == []


def test_u8_default_behaviour_is_unchanged():
    assert needs.kanban_needs_you("CEO-gated: repo publication", None)


def test_u8_role_words_are_configurable():
    pats = needs._patterns(["Ada"])
    assert pats["_GATE"].search("Boss approval needed") is None
    s = {"role_words": "Boss, Founder"}
    assert needs.role_words(s) == ["Boss", "Founder"]
    needs._PATTERN_CACHE.clear()
    pats = needs._patterns(["Ada"], role_words=["Boss", "Founder"])
    assert pats["_GATE"].search("Boss-gated: repo publication")
    needs._PATTERN_CACHE.clear()


def test_u8_pocket_authority_is_neutral_and_configurable(monkeypatch):
    assert needs.pocket_authority({}) == "owner"
    assert needs.pocket_authority({"pocket_authority": "ceo"}) == "ceo"


def test_conditional_gates_distinguish_permissions_from_blockers(monkeypatch):
    monkeypatch.setenv("UNBL0CK_PRINCIPAL", "Ada")
    monkeypatch.setattr(needs, "_settings", dict)
    needs._PATTERN_CACHE.clear()

    non_asks = [
        "We have no blocker so ship without CEO approval",
        "There is no risk so we ship without CEO sign-off",
        "There is no risk, so we merge without CEO approval",
        "No blockers remain, so we deploy without CEO sign-off",
        "We ship without CEO approval",
        "We merge without CEO approval",
        "Not a breaking change so merge without CEO approval",
        "I can do this without CEO approval",
        "Free to merge until CEO review on Friday",
        "There is no problem shipping without CEO approval",
        "Nothing stops us merging without Ada's approval",
        "No reason to hold until CEO approval",
        "It was held until CEO approval, which arrived Monday; now blocked on CI",
        "Free to merge until Ada approves",
        "No reason to hold until the CEO signs off",
        "We can ship without CEO approval",
        "We may merge without Ada's approval",
        "We went ahead without CEO approval",
        "We are proceeding without CEO sign-off",
        "We shipped without Ada's approval",
        "Continuing without CEO approval",
        "Fixed it without needing CEO approval",
        "Safe to merge without CEO approval",
        "Resolved without CEO sign-off",
        "Blocked on CI. Retried twice without CEO approval being needed",
        "We can ship it without CEO approval",
        "I went ahead with the deploy without waiting for CEO approval",
        "Proceeding with the merge without CEO approval, as agreed",
        "We shipped v2 without needing Ada's sign-off",
        "Will proceed without CEO approval",
        "The team can proceed with the rollout without CEO approval",
        "We are shipping today without CEO approval",
        "I can do this without CEO approval",
        "This can go out without CEO approval",
        "We can now release without CEO approval",
        "Going ahead without CEO approval",
        "Moving forward without Ada's review",
        "This is fine without CEO approval",
        "Landed the fix without Ada's sign-off",
        "No reason to wait for CEO approval",
        "Blocked on runner capacity until tonight, unrelated to the CEO decision last week",
        "Blocked on the vendor until they reply; the CEO decision already landed",
        "Cron job runs every hour until CEO approval expires Friday",
        "Approved already: valid until CEO review next quarter",
        "We can deploy today without CEO approval.",
        "We can safely deploy without CEO approval",
        "It is safe to ship this without CEO approval",
        "Tests pass, so we can release v2 without CEO approval",
        "We are free to deploy now without CEO approval",
        "No new failures we can deploy without CEO approval.",
        "No downtime expected so we will deploy without CEO approval",
        "Ada's approval isn't needed",
        "CEO approval wasn't required",
        "No issue shipping without Ada's sign-off",
    ]
    asks = [
        "Stuck without CEO approval",
        "Stalled without Ada's approval",
        "Deploy frozen without CEO approval",
        "Release is stopped without Ada's sign-off",
        "Without CEO approval this cannot ship",
        "Impossible to continue without CEO approval",
        "Merge is forbidden without CEO approval",
        "Too risky to ship without Ada's approval",
        "We may not merge without CEO approval",
        "We cannot ship until Ada approves",
        "We are not done without CEO approval",
        "There's nothing I can do without Ada's approval",
        "Nothing more I can do until Ada replies",
        "Not safe to merge without CEO approval",
        "It is never safe to deploy without Ada's sign-off",
        "It would not be safe to release without CEO approval",
        "Nothing ships until Ada approves",
        "Blocked until Ada signs off",
        "Paused until Ada decides",
        "Blocked until the CEO signs off",
        "Waiting until the CEO approves",
        "Nothing is done until Ada approves",
        "Nothing gets merged until the CEO signs off",
        "No one can merge until Ada approves",
        "Nothing may be deployed without CEO approval",
        "Nothing can ship without CEO approval",
        "No release can go out without Ada's sign-off",
        "We may not be able to ship without CEO approval",
        "I can do nothing more without Ada's decision",
        "The work is done but nothing can merge until Ada signs off",
        "We aren't proceeding without CEO approval",
        "Deployment isn't proceeding until Ada approves",
        "We aren't done without CEO approval",
        "May I deploy without CEO approval?",
        "Can we merge without Ada's sign-off?",
        "Should we proceed without CEO approval?",
        "Is proceeding allowed without CEO approval",
        "Halted. May I merge without CEO approval",
        "The team asks if they can merge without Ada's sign-off",
        "Question: can we proceed without CEO approval",
        "We are done until Ada approves the budget",
        "Unsure if we can proceed without CEO approval",
        "Is it OK if I continue without CEO approval?",
        "Who decides if we can deploy without CEO approval",
        "No deployments can proceed without CEO approval",
        "No changes can be deployed without CEO approval",
    ]
    for reason in non_asks:
        assert needs.kanban_needs_you(reason, "needs_input") is None, reason
    for reason in asks:
        assert needs.kanban_needs_you(reason, "needs_input"), reason
    needs._PATTERN_CACHE.clear()


def test_round10_approval_preconditions_remain_actionable(monkeypatch):
    monkeypatch.setenv("UNBL0CK_PRINCIPAL", "Ada")
    monkeypatch.setattr(needs, "_settings", dict)
    needs._PATTERN_CACHE.clear()
    asks = [
        "Safe to deploy after Ada's approval",
        "Safe to deploy only with Ada's approval",
        "OK to merge pending Ada's sign-off",
        "Free to release once we get Ada's approval",
        "Safe to deploy after Ada's approval; please confirm",
        "Needs Ada's approval before the cron job expires tonight",
        "We can make no progress without Ada's approval",
        "I will keep retrying until Ada approves",
        "We ship nothing until Ada approves",
        "Not moving forward until Ada approves",
        "Unsafe to proceed; safe to ship only with CEO approval",
        "No artifacts can be released without CEO approval",
        "No production changes can be deployed without CEO approval",
        "Needs Ada's approval; we will deploy once Bob approves",
    ]
    controls = [
        "We can deploy today without CEO approval",
        "No new failures we can deploy without CEO approval",
        "Free to merge until Ada approves",
        "No reason to hold until CEO approval",
        "Cron job runs every hour until CEO approval expires Friday",
        "Approved already: valid until CEO review next quarter",
        "We will deploy once Ada approves",
    ]
    for reason in asks:
        assert needs.kanban_needs_you(reason, "needs_input"), reason
    for reason in controls:
        assert needs.kanban_needs_you(reason, "needs_input") is None, reason
    needs._PATTERN_CACHE.clear()
