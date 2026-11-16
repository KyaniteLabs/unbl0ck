"""Gate-phrase grammar: when a matched approval or decision phrase is negated, glued into a
longer token or otherwise not a request for the person. Pure: takes text and compiled patterns,
reads no settings; ``needs._patterns`` builds the patterns.
"""

from __future__ import annotations

import re

# Negation words that turn a gate phrase into "not a gate" ("no new Ada approval
# requested", "not a principal gate", "approval is not needed").
_NEGATION = re.compile(
    r"\b(?:no|not|never|without|nothing|none|neither|nor|"
    r"isn['’]?t|aren['’]?t|wasn['’]?t|weren['’]?t|"
    r"don['’]?t|doesn['’]?t|didn['’]?t|won['’]?t|"
    r"can['’]?t|cannot|no longer|no new)\b",
    re.IGNORECASE,
)
_PERMISSION_BEFORE_GATE = re.compile(
    r"(?:\b(?:no problem|no issue|nothing stops us)"
    r"(?:\s+(?:shipping|merging|releasing|deploying|proceeding|continuing))?"
    r"|\bno reason to (?:hold|wait|pause|stop)"
    r"|\bfree to (?:ship|merge|release|deploy|proceed|launch|publish|roll out)"
    r"|\bsafe to (?:ship|merge|release|deploy|proceed|launch|publish|roll out)"
    r"|\bok to (?:ship|merge|release|deploy|proceed|launch|publish|roll out)"
    r"|\b(?:can|may)\s+(?:ship|merge|release|deploy|proceed|continue|launch|publish|roll out|go ahead|move forward)"
    r"|\b(?:we|i|they|you|the team|team)\s+(?:can|may)\s+"
    r"(?:(?:now|today|safely|easily|quickly)\s+)?"
    r"(?:ship|merge|release|deploy|proceed|continue|launch|publish|roll out|go ahead|move forward|do|make|finish|complete)"
    r"|\b(?:we|i|they)\s+will\s+(?:(?:safely|easily|quickly)\s+)?"
    r"(?:ship|merge|release|deploy|proceed|continue|launch|publish|roll out|go ahead|move forward)"
    r"|\bwill\s+(?:ship|merge|release|deploy|proceed|continue|launch|publish|roll out|go ahead|move forward)\b"
    r"|\b(?:going ahead|moving forward)\b"
    r"|\bthis is (?:fine|okay|ok|safe)\b"
    r"|\bthis\s+can\s+go\s+out\b"
    r"|\b(?:we|i|they)\s+(?:ship|merge|release|deploy|proceed|continue|launch|publish|roll out)\b"
    r"|\b(?:we|i|they)\s+(?:(?:am|is|are|was|were)\s+)?"
    r"(?:proceeding|continuing|continued|went ahead|proceeded|shipping|merging|releasing|"
    r"deploying|launching|publishing|rolling out|going ahead|moving forward|done|fixed(?: it)?|resolved|"
    r"shipped|works|worked|merged|released|deployed)"
    r"|\b(?:completed|finished|landed)\b"
    r"|\b(?:is|are|was|were)\s+(?:done|fixed|resolved)"
    r"|\b(?:continuing|proceeding|continued|proceeded|done|fixed(?: it)?|resolved|shipped|works|worked|merged|released|deployed)"
    r"|^\s*(?:ship|merge|release|deploy|proceed))",
    re.IGNORECASE,
)
_NEGATED_PERMISSION = re.compile(
    r"\b(?:(?:nothing(?!\s+stops?\s+us\b))|no\s+one|nobody|none(?:\s+of\s+\w+)?|"
    r"not|never|cannot|can't|won't|don't|doesn't|didn't|shouldn't|"
    r"may\s+not|can\s+not|will\s+not|do\s+not|does\s+not|did\s+not|should\s+not)\b"
    r"(?:\s+\w+){0,8}\s+(?:proceed\w*|continu\w*|ship\w*|merg\w*|releas\w*|"
    r"deploy\w*|done|fix\w*|resolv\w*|go\s+out)\b"
    r"|\bno\s+(?:new\s+)?(?:deployments?|changes?|releases?|merges?)\s+"
    r"(?:can|may|will)\s+(?:proceed|go\s+out|be\s+(?:shipped|merged|released|deployed))\b"
    r"|\bno(?:\s+\w+){0,5}\s+(?:can|may|will)\s+be\s+"
    r"(?:shipped|merged|released|deployed|published|rolled\s+out)\b"
    r"|\bnothing(?:\s+\w+){0,3}\s+(?:i|we|they)\s+can\s+do\b"
    r"|\b(?:no|little)\s+(?:\w+\s+){0,2}(?:progress|headway|movement|work)\b"
    r"|\b(?:ship|merge|release|deploy|proceed|continue|launch|publish)\s+nothing\b"
    r"|\b(?:not|no longer)\s+(?:moving forward|going ahead)\b"
    r"|\b(?:can|may|will|do)\s+(?:do\s+)?nothing\b",
    re.IGNORECASE,
)
_GATE_PROHIBITION = re.compile(
    r"\b(?:(?:is|are|was|were)\s+(?:not\s+allowed|blocked|forbidden|prohibited|not\s+permitted)|"
    r"(?:would\s+)?violate(?:s|d)?\s+policy|breaks\s+policy)\b",
    re.IGNORECASE,
)
_HISTORICAL_HOLD_UNTIL = re.compile(
    r"\b(?:was|were|had been)\s+(?:held|paused|blocked|waiting|pending)\b.{0,40}\buntil\b",
    re.IGNORECASE,
)
_HISTORICAL_GATE_MENTION = re.compile(
    r"\b(?:unrelated to\b|decision already (?:landed|made|arrived)\b|"
    r"approved already\b|valid until\b)",
    re.IGNORECASE,
)
_HISTORICAL_SCHEDULE_EXPIRY = re.compile(
    r"\b(?:cron|job|schedule)\b.{0,80}\bexpires?\b", re.IGNORECASE
)
_NON_BLOCKING_ONCE_PLAN = re.compile(
    r"\b(?:(?:we|i|they)\s+will|(?:ok|okay)\s+to)\s+"
    r"(?:ship|merge|release|deploy|publish)\b.{0,40}\bonce\s+"
    r"(?:[A-Z][\w'-]*|the\s+[A-Z][\w'-]*)\b.{0,20}"
    r"(?:approves?|signs? off|confirms?)\b",
    re.IGNORECASE,
)
_CONDITIONAL_GATE = re.compile(r"\b(?:without|until)\b", re.IGNORECASE)
# Characters that make a name part of a path, slug, env var or email instead of a
# standalone word: /home/ada/..., ada/fleet, Ada-reserved, ada@example.com.
_TOKEN_GLUE = "/\\-_@"


# A clause ends at sentence or clause punctuation; negation never reaches across it
# ("Cannot proceed: needs <role word> approval" is an ask).
_CLAUSE_END = re.compile(r"[,.;:!?\n]|\s[-–—]\s|\s+(?:and|but|or|so)\s+", re.IGNORECASE)
# After the phrase, only "... is not needed/required" undoes it; any other negation
# there ("Needs your approval. Not urgent.") belongs to something else.
_POST_NEGATION = re.compile(
    r"^\s*(?:is|are|was|were)?\s*(?:not|no longer|n['’]t)\s+(?:needed|required|necessary|wanted)\b",
    re.IGNORECASE,
)
_NEGATED_NAMED_GATE = re.compile(
    r"\b(?:[A-Z][\w'-]*['’]?s?\s+)?(?:approval|decision|sign[- ]?off|review|input)\b"
    r".{0,30}\b(?:isn't|wasn't|aren't|weren't|is\s+not|was\s+not|are\s+not|"
    r"were\s+not|not|no\s+longer)\s+(?:needed|required|necessary|wanted)\b",
    re.IGNORECASE,
)
_DENIAL_BEFORE_UNTIL = re.compile(r"\b(?:no|not)\b.{0,40}\bneeded\b", re.IGNORECASE)
_NOT_WAITING_UNTIL = re.compile(r"\b(?:not|no longer)\s+waiting\b", re.IGNORECASE)
_WITHOUT_UNNEEDED = re.compile(
    r"\bbeing\s+(?:needed|required|necessary|wanted)\b", re.IGNORECASE
)
_QUESTION_OR_UNCERTAIN = re.compile(
    r"(?:^\s*|[.!?;]\s*)(?:(?:may|can|could|should|would|will|do|does|did|is|are|was|were)\s+"
    r"(?:i|we|you|they|he|she|it|this|that)\b|what|who|when|where|why|how|which)\b|"
    r"(?:^|[.!?;]\s*)(?:is|are|was|were)\s+(?:proceeding|shipping|merging|deploying|"
    r"releasing|continuing)\s+(?:allowed|possible|safe|okay|ok|permitted)\b|"
    r"\b(?:whether|unsure|not sure|tell me|decide|asks?\s+if)\b|^\s*question\s*:",
    re.IGNORECASE,
)
_PERMISSION_STATUS_TAIL = re.compile(
    r"\b(?:is|are|was|were|not|never|impossible|blocked|forbidden|prohibited|frozen|"
    r"stuck|paused|pending|waiting|waits?|stopped|halted|deferred|queued|unable|refusing|"
    r"held|hold|parked|idle|on\s+hold|too\s+risky)\b",
    re.IGNORECASE,
)


def _match_is_negated(text: str, match: re.Match[str]) -> bool:
    """Does a negation govern this gate phrase within its clause?"""
    head = text[: match.start()]
    cuts = list(_CLAUSE_END.finditer(head))
    clause = head[cuts[-1].end() :] if cuts else head
    near = " ".join(clause.split()[-3:])
    if match.group().lower().startswith("until "):
        clause += match.group()
    conditional = None
    # Search the whole clause, but only let a nearby without/until phrase
    # govern this gate (including a short adjective or article in between).
    conditional_matches = list(_CONDITIONAL_GATE.finditer(clause))
    if conditional_matches:
        candidate = conditional_matches[-1]
        words_between = clause[candidate.end() :].split()
        if len(words_between) <= 6:
            conditional = candidate
    before_conditional = clause[: conditional.start()] if conditional else clause
    permission_clause = re.split(
        r"\b(?:yet|while|although|whereas)\b", before_conditional, flags=re.IGNORECASE
    )[-1]
    permission_text = permission_clause.strip()
    permission_match = _PERMISSION_BEFORE_GATE.search(permission_text)
    if permission_match:
        remainder = permission_text[permission_match.end() :].split()
        # Objects and short time adverbs commonly follow the permission verb.
        # A much longer tail is more likely a separate status clause.
        if len(remainder) > 6 or _PERMISSION_STATUS_TAIL.search(" ".join(remainder)):
            permission_match = None
    if permission_match and (
        text.rstrip().endswith("?") or _QUESTION_OR_UNCERTAIN.search(text)
    ):
        permission_match = None
    if (
        permission_match
        and conditional
        and conditional.group().lower() == "until"
        and re.search(
            r"\b(?:done|fixed|resolved)\b", permission_match.group(), re.IGNORECASE
        )
    ):
        permission_match = None
    if permission_match and re.match(
        r"^\s*is\s+it\s+safe\s+to\b", permission_clause, re.IGNORECASE
    ):
        permission_match = None
    after = text[match.end() :]
    end = _CLAUSE_END.search(after)
    after_clause = after[: end.start()] if end else after
    permission = bool(
        conditional
        and permission_match
        and not _NEGATED_PERMISSION.search(before_conditional)
        and not _GATE_PROHIBITION.search(after_clause)
    )
    after = after[: end.start()] if end else after
    historical_hold = bool(
        conditional
        and conditional.group().lower() == "until"
        and _HISTORICAL_HOLD_UNTIL.search(clause)
    )
    denial = bool(
        conditional
        and (
            permission
            or historical_hold
            or (
                conditional.group().lower() == "without"
                and _WITHOUT_UNNEEDED.search(after)
            )
            or (
                conditional.group().lower() == "until"
                and _DENIAL_BEFORE_UNTIL.search(before_conditional)
            )
            or (
                conditional.group().lower() == "until"
                and _NOT_WAITING_UNTIL.search(before_conditional)
            )
        )
    )
    gate_condition = bool(conditional and not denial)
    no_reason_to_wait = bool(
        permission_match
        and re.match(
            r"(?i)^no reason to (?:hold|wait|pause|stop)\b", permission_match.group()
        )
    )
    gate_context = f"{clause} {match.group()} {after_clause}"
    normalized_context = " ".join(gate_context.split())
    if _NON_BLOCKING_ONCE_PLAN.search(normalized_context):
        return True
    if _HISTORICAL_GATE_MENTION.search(normalized_context):
        return True
    schedule_expiry = _HISTORICAL_SCHEDULE_EXPIRY.search(normalized_context)
    gate_offset = len(" ".join(clause.split())) + 1
    if (
        schedule_expiry
        and schedule_expiry.start() < gate_offset < schedule_expiry.end()
    ):
        return True
    return bool(
        denial
        or no_reason_to_wait
        or (_NEGATION.search(near) and not gate_condition)
        or _POST_NEGATION.search(after)
    )


def _match_is_standalone(text: str, match: re.Match[str]) -> bool:
    before = text[match.start() - 1] if match.start() else ""
    after = text[match.end()] if match.end() < len(text) else ""
    return not (before and before in _TOKEN_GLUE) and not (
        after and after in _TOKEN_GLUE
    )


def _any_gate(reason: str, pats: dict[str, re.Pattern[str]]) -> bool:
    return any(
        not _match_is_negated(reason, m) and _match_is_standalone(reason, m)
        for m in pats["_GATE"].finditer(reason)
    )


def _any_name(reason: str, pats: dict[str, re.Pattern[str]]) -> bool:
    negated_gates = list(_NEGATED_NAMED_GATE.finditer(reason))
    return any(
        not _match_is_negated(reason, m)
        and _match_is_standalone(reason, m)
        and not any(
            g.start() <= m.start() and m.end() <= g.end() for g in negated_gates
        )
        for m in pats["_NAMES"].finditer(reason)
    )
