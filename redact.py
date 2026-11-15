"""Second-line secret redaction for text that UNBL0CK sends to a model or shows in the queue.

What Ask sends is chosen field by field in needs.py (``_ask_card_payload``); this module only hides
secrets, the home folder and credential-shaped values inside text those fields carry, and inside
JSON found in that text. Pure functions: no I/O, no Hermes imports.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

_ASK_SECRET_KEY = (
    r"[a-z0-9_.-]{0,64}(?P<secret_word>password|passwd|passphrase|pass|pwd|pw|secret|token|"
    r"api(?:[_-]|\s)?key|access(?:[_-]|\s)?key|private(?:[_-]|\s)?key|credential|"
    r"authentication|authorization|cookie|oauth|auth|storepass)"
    r"[a-z0-9_.-]{0,32}"
)
# A key whose value is a short numeric code (``otp``, ``login_pin``); secret only with digits as its value.
_ASK_CODE_KEY = r"(?:[a-z0-9]+[_.-]){0,3}(?:bearer|t?otp|pin|passcode|session[_-]?id|signing[_-]?key)"
_ASK_CREDENTIAL_NAME = rf"(?:{_ASK_SECRET_KEY}|{_ASK_CODE_KEY})"


# Last resort for a single-quoted value no other reading closes: the shell reads a backslash inside single
# quotes literally. The rest of its shell word stays for the later passes, and _ASK_GLUED takes what is left.
_SQ = r"'[^']*'"
_ASK_GLUED = re.compile(r"""\[redacted\][^\s'"@;|&<>()]+""")


def _ask_every_flag(span: str, flag: str, bare: str, single: str = r"'(?:\\.|[^'\\])*'") -> Callable[[str], str]:
    """Each *flag* in a *span* (a command to the end of its line, and the whitespace that ends it) has its
    value redacted, not only the first. A quoted value may run on past the span, as the shell reads it."""
    command = re.compile(rf"(?i){span}")
    value = re.compile(rf"""(?i)({flag})(?:"(?:\\.|[^"\\])*"|{single}|{bare}|{_SQ})""")

    def redact(text: str) -> str:
        at = 0
        while found := command.search(text, at):
            at, end = found.start(), found.end()
            while (hit := value.search(text, at)) and hit.start() <= end:
                text = text[: hit.end(1)] + "[redacted]" + text[hit.end() :]
                at = hit.end(1) + len("[redacted]")
                end = max(at, end - (hit.end() - at))
            at = max(at, end)
        return text

    return redact


_ASK_SECRET_PATTERNS = (
    # URL userinfo: scheme://user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]*:[^/\s@]+@"), r"\1[redacted]@"),
    _ask_every_flag(r"(?:proxy-)?authorization\s*:\s*digest\b[^\r\n]*", r"\bnonce\s*[=:]\s*", r"[^\s,;]+"),
    _ask_every_flag(r"(?:proxy-)?authorization\s*:\s*digest\b[^\r\n]*", r"\bresponse\s*[=:]\s*", r"[^\s,;]+"),
    (
        re.compile(
            r"(?i)((?:proxy-)?authorization\s*:\s*)"
            r"(?:bearer|basic|token|apikey|digest)\s+[^\s,'\";]+"
        ),
        r"\1[redacted]",
    ),
    _ask_every_flag(r"\bcurl\b[^\r\n]*", r"\s(?:--user|-u)(?:=|\s+)?", r"""[^:\s'"]+:[^\s'"]+""", _SQ),
    _ask_every_flag(r"\bcurl\b[^\r\n]*", r"\s(?:--cookie|-b)(?:=|\s+)?", r"""[^\s'"]+"""),
    _ask_every_flag(
        r"\b(?:mysql|mysqldump|mariadb|sshpass|docker\s+login)\b[^;\r\n]*", r"\s-(?-i:p)(?:=|\s+)?", r"""[^\s'"]+"""
    ),
    _ask_every_flag(r"\bkeytool\b[^\r\n]*", r"\s-storepass(?:=|[ \t]+)", r"[^\s;]+"),
    (
        re.compile(
            r"(?i)(?P<prefix>\bIDENTIFIED\s+BY\s+)"
            r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;]+|" + _SQ + r")"
        ),
        r"\g<prefix>[redacted]",
    ),
    (
        re.compile(
            r"""(?i)\b(password|passwd)\s+is\s+(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,'";]+|""" + _SQ + r""")"""
        ),
        r"\1 is [redacted]",
    ),
    # JWTs and home-directory shapes of any user (the current user's own home is handled in _ask_redact)
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
        "[redacted]",
    ),
    (re.compile(r"(?<![\w.])/(?:Users|home)/[^/\s\"'`]+(?=/|\s|$|[\"'`)\],;:])"), "~"),
    (re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s\"'`]+"), "~"),
    # Well-known token shapes
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "[redacted]"),
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"), "[redacted]"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"), "[redacted]"),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"), "[redacted]"),
    (re.compile(r"\bnpm_[A-Za-z0-9]{16,}\b"), "[redacted]"),
    (re.compile(r"\bhf_[A-Za-z0-9]{8,}\b"), "[redacted]"),
    (re.compile(r"\bSG\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{8,})*"), "[redacted]"),
    (re.compile(r"\bdop_v1_[A-Za-z0-9_-]{8,}\b"), "[redacted]"),
    (re.compile(r"\bwhsec_[A-Za-z0-9_-]{8,}\b"), "[redacted]"),
    (
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"),
        "[redacted]",
    ),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[redacted]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted]"),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
        ),
        "[redacted]",
    ),
)

_ASK_SECRET_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])" + _ASK_CREDENTIAL_NAME + r"(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
_ASK_NON_SECRET_AUTH_KEYS = frozenset(
    {
        "author",
        "authors",
        "authored",
        "authority",
        "auth_mode",
        "auth_method",
        "authentication_method",
        "oauth_scopes",
        "tokenizer",
        "max_tokens",
        "passed",
        "bypass",
    }
)
_ASK_SECRET_PLURALS = {
    "tokens": "token",
    "credentials": "credential",
    "cookies": "cookie",
    "secrets": "secret",
}
_ASK_SECRET_WORD_PRIORITY = {
    "password": 100,
    "passwd": 100,
    "passphrase": 100,
    "credential": 90,
    "storepass": 90,
    "private_key": 80,
    "api_key": 80,
    "access_key": 80,
    "secret_key": 80,
    "secret": 75,
    "token": 70,
    "authorization": 65,
    "authentication": 65,
    "cookie": 60,
    "oauth": 55,
    "auth": 50,
    "pwd": 25,
    "pass": 20,
    "pw": 10,
}
_ASK_GLUE_SECRET_WORDS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "credential",
        "authentication",
        "authorization",
        "cookie",
        "oauth",
        "token",
        "secret",
        "auth",
        "storepass",
    }
)
_ASK_SECRET_KEY_TERMINALS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "pass",
        "pwd",
        "pw",
        "secret",
        "secrets",
        "token",
        "tokens",
        "credential",
        "credentials",
        "authentication",
        "authorization",
        "auth",
        "cookie",
        "cookies",
        "oauth",
        "storepass",
    }
)
_ASK_CODE_KEY_TERMINALS = frozenset({"bearer", "otp", "totp", "pin", "passcode", "sessionid"})
_ASK_CODE_ASSIGNMENT = re.compile(
    rf"""(?i)(?<![A-Za-z0-9_.-])(?P<key>["']?{_ASK_CODE_KEY}["']?)"""
    r"""(?P<sep>[ \t]*[=:][ \t]*)(?P<value>"\d+"|'\d+'|\d+)(?![A-Za-z0-9_.-])"""
)
_ASK_SECRET_ASSIGNMENT = re.compile(
    rf"""(?ix)(?<![A-Za-z0-9.-])(?P<flag>--)?(?P<key_open>\\*["']?)(?P<key>{_ASK_CREDENTIAL_NAME})(?P<key_close>\\*["']?)(?![A-Za-z0-9_.-])"""
    r"""(?P<sep>[ \t]*(?:=>|[=:])[ \t]*(?:\r?\n[ \t]*)?|[ \t]+)"""
)

_ASK_AUTH_HEADER = re.compile(
    r"(?im)(?P<prefix>\b(?:proxy-)?authorization[ \t]*:[ \t]*)"
    r"(?P<value>[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)?)"
)
_ASK_AUTH_PROSE = re.compile(
    r"^(?:header\s+(?:missing|is|was|wasn't|doesn't)|flow\s+(?:is|was|looks|seems)|"
    r"pending\b|none\b|required\b|needs?\b|need\b|must\b|should\b|broken\b|"
    r"legal\s+review\b|(?:may|can|should)\s+(?:i|we|you)\b|"
    r"missing\b|failed\b|failing\b|invalid\b|granted\b|approved\b|tbd\b|tokens?\s+are\b|"
    r"the\s+\w+\s+(?:needs?|needs\s+to|is|was)\b|"
    r"[a-z][a-z'-]*\s+(?:must|should|needs?|requires?|is|was|can|may|will)\b)",
    re.IGNORECASE,
)
_ASK_BARE_AUTH_SCHEME_LINE = re.compile(
    r"(?im)(?P<indent>^[ \t]*)(?P<scheme>JWT|Api-Key|OAuth|SharedKey|AWS|Bearer|Key)"
    r"(?P<separator>:[ \t]*|[ \t]+)(?P<token>[^\s\r\n]+)(?P<tail>[^\r\n]*)"
)


_ASK_SHORT_PASSWORD_FLAG = re.compile(
    r"(?P<prefix>(?<!\S)-p(?:=|[ \t]+))"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s;]+|" + _SQ + r")"
)
_ASK_AUTH_CREDENTIAL_MARKER = re.compile(
    r"(?i)\b(?:token|oauth|key|secret|password|credential|session)\b"
    r"(?:\s+(?:value\s+)?is\s+|\s*[=:]\s*|\s+(?!to\b|are\b|were\b|was\b)\S+)"
)
_ASK_AUTH_TRAILING_VALUE = re.compile(r"(?i)\bfailed\s+with\s+[A-Z0-9_-]+\s*$")
_ASK_BARE_CREDENTIAL_VALUE = re.compile(
    r"(?i)\b(?P<scheme>bearer|basic|token)(?P<space>[ \t]+)"
    r"(?P<token>[A-Za-z0-9._~+/=-]+)"
)


def _ask_cookie_prose(value: str) -> bool:
    value = value.strip().strip("\"' ")
    words = value.split()
    return bool(
        len(words) >= 3
        and not re.search(r"[=:;]", value)
        and re.search(
            r"\b(?:banner|copy|pending|review|notice|policy|consent|message|text|"
            r"wording|checkout|legal|the|a|an)\b",
            value,
            re.IGNORECASE,
        )
    )


_ASK_COOKIE_HEADER = re.compile(
    r"""(?im)(?P<prefix>^[ \t]*|(?:-H\s+['"]"))(?P<header>(?:set-?)?cookie)\s*:\s*(?P<value>[^\r\n'"]*)"""
)


def _ask_redact_cookie_headers(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if _ask_cookie_prose(
            match.group("value")
        ) and not _ASK_AUTH_CREDENTIAL_MARKER.search(match.group("value")):
            return match.group(0)
        return f"{match.group('prefix')}{match.group('header')}: [redacted]"

    return _ASK_COOKIE_HEADER.sub(replace, text)


def _ask_secret_word(key: str) -> str | None:
    original = str(key).strip().strip("\"'").removeprefix("--")
    original = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", original)
    original = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", original)
    normalized = re.sub(r"[^a-z0-9]+", "_", original.casefold()).strip("_")
    if normalized in _ASK_NON_SECRET_AUTH_KEYS:
        return None
    normalized = normalized.removeprefix("author_")
    parts = normalized.split("_")
    found: list[str] = []
    for index, part in enumerate(parts):
        if part in _ASK_SECRET_PLURALS:
            found.append(_ASK_SECRET_PLURALS[part])
        if (
            part in {"api", "access", "private", "secret"}
            and index + 1 < len(parts)
            and parts[index + 1] == "key"
        ):
            found.append(f"{part}_key")
        for word in _ASK_SECRET_WORD_PRIORITY:
            if part == word or (
                word in _ASK_GLUE_SECRET_WORDS
                and (
                    part.endswith(word) or re.fullmatch(re.escape(word) + r"\d+", part)
                )
            ):
                found.append(word)
    if not found:
        return None
    return max(found, key=lambda word: _ASK_SECRET_WORD_PRIORITY[word])


def _ask_secret_key_is_exact(key: str) -> bool:
    original = str(key).strip().strip("\"'").removeprefix("--")
    original = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", original)
    original = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", original)
    normalized = re.sub(r"[^a-z0-9]+", "_", original.casefold()).strip("_")
    if normalized in _ASK_SECRET_KEY_TERMINALS | {
        "api_key",
        "access_key",
        "private_key",
        "secret_key",
        "session_id",
    }:
        return True
    parts = normalized.split("_")
    if not parts:
        return False
    terminal = parts[-1]
    if terminal in _ASK_SECRET_KEY_TERMINALS | _ASK_CODE_KEY_TERMINALS:
        return True
    if re.search(
        r"(?:password|passwd|passphrase|credential|authentication|authorization|cookie|oauth|token|secret|auth|storepass)\d+$",
        terminal,
    ):
        return True
    if terminal.endswith(tuple(_ASK_GLUE_SECRET_WORDS)):
        return True
    return (
        terminal == "key"
        and len(parts) > 1
        and parts[-2] in {"api", "access", "private", "secret", "signing"}
    )


def _ask_names_credential(name: str) -> bool:
    """Whether *name* could hold a credential, so its value goes to ``_ask_is_secret_key``."""
    return _ask_secret_word(name) is not None or _ask_secret_key_is_exact(name)


def _ask_harmless_secret_value(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    if not isinstance(value, str):
        return False
    value = value.strip().strip("\"' ").casefold()
    return value in {"true", "false", "yes", "no", "n/a", "na", "none"} or bool(
        re.fullmatch(
            r"\d+(?:\s+of\s+\d+|\s+(?:fail(?:ed|ures?)?|pass(?:ed)?)\s*:\s*\d+)?",
            value,
        )
    )


def _ask_numeric_credential(key: str, value: Any) -> bool:
    """A number or digit-only string under an exact credential key (``password=123456``) is the
    credential itself. Counts under other keys (``password_attempts: 3``) are not."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        digits = True
    elif isinstance(value, str):
        digits = bool(re.fullmatch(r"\d+", value.strip().strip("\"' ")))
    else:
        return False
    if str(key).strip().strip("\"' ").casefold().endswith("tokens"):
        return False  # a count of tokens (``tokens: 12``), not a token
    return digits and _ask_secret_key_is_exact(key)


def _ask_path_field_value(key: str, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    original = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", str(key))
    normalized = re.sub(r"[^a-z0-9]+", "_", original.casefold()).strip("_")
    if not normalized.endswith(("_path", "_file", "_dir")):
        return False
    value = value.strip().strip("\"'")
    return bool(
        re.fullmatch(
            r"(?:[A-Za-z]:)?/?[A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)+",
            value,
        )
    )


def _ask_auth_value_is_prose(value: str) -> bool:
    value = value.strip().strip("\"'` ")
    if _ASK_AUTH_CREDENTIAL_MARKER.search(value) or _ASK_AUTH_TRAILING_VALUE.search(
        value
    ):
        return False
    return bool(_ASK_AUTH_PROSE.match(value))


def _ask_secret_value_shape(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    value = value.strip().strip("\"'")
    if len(value) < 8 or any(char.isspace() for char in value):
        return False
    has_upper = any(char.isupper() for char in value)
    has_digit = any(char.isdigit() for char in value)
    has_symbol = any(not char.isalnum() for char in value)
    return (
        (has_digit and has_upper)
        or (len(value) >= 16 and has_digit and has_symbol)
        or (len(value) >= 12 and has_upper and has_symbol)
    )


def _ask_is_secret_key(key: str, value: Any = None) -> bool:
    word = _ask_secret_word(key)
    if word is None:  # a numeric code under a code key (``otp: 123456``), never a short count
        return _ask_numeric_credential(key, value) and len(str(value).strip("\"' ")) >= 4
    if _ask_harmless_secret_value(value) and not _ask_numeric_credential(key, value):
        return False
    if _ask_path_field_value(key, value):
        return False
    if (
        word in {"auth", "authentication", "authorization", "oauth", "cookie"}
        and isinstance(value, str)
        and _ask_auth_value_is_prose(value)
    ):
        return False
    if (
        word == "cookie"
        and isinstance(value, str)
        and _ask_cookie_prose(value)
        and not _ASK_AUTH_CREDENTIAL_MARKER.search(value)
    ):
        return False
    if (
        word == "token"
        and isinstance(value, str)
        and re.match(
            r"(?i)^the\s+(?:design|api)\s+token\b", value.strip().strip("\"' ")
        )
    ):
        return False
    if (
        word == "secret"
        and isinstance(value, str)
        and re.match(r"(?i)^none\b", value.strip().strip("\"' "))
    ):
        return False
    return _ask_secret_key_is_exact(key) or _ask_secret_value_shape(value)


def _ask_auth_redaction(value: str) -> str | None:
    """Redact Authorization values unless they read like ordinary status prose."""
    value = value.strip().strip("\"'")
    if _ask_auth_value_is_prose(value):
        return None
    return "[redacted]" if value else None


def _ask_redact_authorization_headers(text: str) -> str:
    def line(match: re.Match[str]) -> str:
        raw_value = match.group("value")
        redacted = _ask_auth_redaction(raw_value)
        if redacted is None:
            return match.group(0)
        suffix = ""
        value = raw_value.rstrip()
        if value.endswith(('"', "'", "`")):
            suffix = value[-1]
        return f"{match.group('prefix')}{redacted}{suffix}"

    text = _ASK_AUTH_HEADER.sub(line, text)

    def bare_scheme(match: re.Match[str]) -> str:
        if not (
            _ask_secret_value_shape(match.group("token"))
            or (
                len(match.group("token")) == 1
                and match.group("token").isupper()
                and not match.group("tail").strip()
            )
        ):
            return match.group(0)
        return (
            f"{match.group('indent')}{match.group('scheme')}"
            f"{match.group('separator')}[redacted]{match.group('tail')}"
        )

    return _ASK_BARE_AUTH_SCHEME_LINE.sub(bare_scheme, text)


def _ask_redact_numeric_codes(text: str) -> str:
    def code(match: re.Match[str]) -> str:
        if not _ask_is_secret_key(match.group("key"), match.group("value")):
            return match.group(0)
        quote = match.group("value")[0] if match.group("value")[0] in "\"'" else ""
        return f"{match.group('key')}{match.group('sep')}{quote}[redacted]{quote}"

    return _ASK_CODE_ASSIGNMENT.sub(code, text)


def _ask_redact_bare_credential_values(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group("token")
        if not _ask_secret_value_shape(token):
            return match.group(0)
        return f"{match.group('scheme')}{match.group('space')}[redacted]"

    return _ASK_BARE_CREDENTIAL_VALUE.sub(replace, text)


_ASK_YAML_FIELD = re.compile(
    r"^(?P<indent>[ \t]*)(?P<dash>-\s*)?(?P<explicit>\?[ \t]*)?(?P<quote>[\"']?)"
    r"(?P<key>[A-Za-z0-9_.-]+)(?P=quote)\s*:\s*(?P<value>[^\r\n]*)"
)
_ASK_YAML_EXPLICIT_KEY = re.compile(r"(?P<prefix>[ \t]*(?:-[ \t]+)?)\?(?:[ \t]+|\Z)(?P<key>.*)")
_ASK_YAML_COMMENT = re.compile(r"(?:^|[ \t])#.*")
_ASK_YAML_BLOCK = re.compile(
    r"^(?P<style>[|>](?:[+-][1-9]?|[1-9][+-]?)?)(?P<tail>[ \t]*(?:#.*)?)$"
)


def _ask_yaml_indent(match: re.Match[str]) -> int:
    return len(match.group("indent")) + len(match.group("dash") or "")


def _ask_yaml_scalar_end(
    lines: list[str], index: int, indent: int, explicit_indent: int | None = None, plain: bool = False
) -> tuple[int, str]:
    """End of the block nested under ``lines[index]`` (blank lines, and comments when *plain*,
    included) and its first value."""
    end, first_value = index + 1, ""
    for at in range(index + 1, len(lines)):
        body = _ASK_YAML_COMMENT.sub("", lines[at].rstrip("\r\n")) if plain else lines[at].rstrip("\r\n")
        if body.strip():
            if len(body) - len(body.lstrip(" \t")) < (explicit_indent or indent + 1):
                break
            end, first_value = at + 1, first_value or body.strip()
    return end, first_value


def _ask_flow_secret_values(text: str) -> str:
    quoted_value = r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^,}]+|""" + _SQ
    name_first = re.compile(
        rf"""(?ix)(?P<before>["']?(?:name|key)["']?\s*:\s*["']?)(?P<name>{_ASK_CREDENTIAL_NAME})"""
        rf"""(?P<after>["']?\s*,\s*["']?(?:value|val|default)["']?\s*:\s*)(?P<value>{quoted_value})"""
    )
    value_first = re.compile(
        rf"""(?ix)(?P<before>["']?(?:value|val|default)["']?\s*:\s*)(?P<value>{quoted_value})"""
        rf"""(?P<between>\s*,\s*["']?(?:name|key)["']?\s*:\s*["']?)(?P<name>{_ASK_CREDENTIAL_NAME})(?P<endquote>["']?)"""
    )

    def replacement(raw: str) -> str:
        quote = raw[0] if raw[:1] in ('"', "'") else ""
        return f"{quote}[redacted]{quote}"

    def first(match: re.Match[str]) -> str:
        if not _ask_is_secret_key(match.group("name"), match.group("value")):
            return match.group(0)
        return f"{match.group('before')}{match.group('name')}{match.group('after')}{replacement(match.group('value'))}"

    def last(match: re.Match[str]) -> str:
        if not _ask_is_secret_key(match.group("name"), match.group("value")):
            return match.group(0)
        return f"{match.group('before')}{replacement(match.group('value'))}{match.group('between')}{match.group('name')}{match.group('endquote')}"

    return value_first.sub(last, name_first.sub(first, text))


def _ask_yaml_explicit_keys(raw: list[str]) -> tuple[list[str], list[str]]:
    """Fold each one-word explicit key (``? key``, its comments and continuation lines, then
    ``: value``) into one entry; return the entries and the ``key: value`` line each matches as."""
    lines: list[str] = []
    views: list[str] = []
    i = 0
    while i < len(raw):
        head = _ASK_YAML_EXPLICIT_KEY.match(raw[i].rstrip("\r\n"))
        if head:
            col, words, j = len(head["prefix"]), _ASK_YAML_COMMENT.sub("", head["key"]).split(), i + 1
            while j < len(raw):
                body = raw[j].rstrip("\r\n")
                text = _ASK_YAML_COMMENT.sub("", body)
                if text.strip() and len(text) - len(text.lstrip(" \t")) <= col:
                    break
                words += text.split()
                j += 1
            value = raw[j][col:] if j < len(raw) and not raw[j][:col].strip() else ""
            key = re.sub(r"^([\"'])(.*)\1$", r"\2", " ".join(words).replace("\\ ", ""))  # YAML line folding
            if key and not re.search(r"\s", key) and re.match(r":(?:[ \t]|$)", value):
                lines.append("".join(raw[i : j + 1]))
                views.append(f"{head['prefix']}? {key}{value}")
                i = j + 1
                continue
        lines.append(raw[i])
        views.append(raw[i])
        i += 1
    return lines, views


def _ask_redact_yaml(text: str) -> str:
    lines, views = _ask_yaml_explicit_keys(_ask_flow_secret_values(text).splitlines(keepends=True))
    fields = [_ASK_YAML_FIELD.match(view.rstrip("\r\n")) for view in views]
    paired_values: dict[int, str] = {}
    for index, field in enumerate(fields):
        name = field["value"].strip().strip("\"'") if field and field["key"].casefold() in {"name", "key"} else ""
        if not _ask_names_credential(name):
            continue
        # The nearest value field within eight lines, unless another name or list item comes first.
        for step in (1, -1):
            near = (c for c in range(index + step, index + 9 * step, step) if 0 <= c < len(fields) and fields[c])
            stop = next((c for c in near if fields[c]["key"].casefold() in {"value", "val", "default"} or (
                _ask_yaml_indent(fields[c]) <= _ask_yaml_indent(field) and (fields[c]["dash"] or fields[c]["key"].casefold() in {"name", "key"}))), None)
            if stop is not None and fields[stop]["key"].casefold() in {"value", "val", "default"}:
                paired_values[stop] = name
                break

    out: list[str] = []
    index = 0
    while index < len(lines):
        field = fields[index]
        key, raw_value = (field["key"], field["value"]) if field else ("", "")
        paired_name = paired_values.get(index)
        secret_name = key if paired_name is None else paired_name
        direct_secret = _ask_names_credential(key)
        if (
            not (direct_secret or paired_name is not None)
            or (direct_secret and not field.group("explicit") and raw_value.lstrip().startswith(('"', "'")))
            or (
                paired_name is None
                and _ask_secret_word(key) in {"auth", "authentication", "authorization", "oauth", "cookie"}
                and _ask_auth_value_is_prose(raw_value)
            )
        ):
            out.append(lines[index])
            index += 1
            continue
        scalar = _ASK_YAML_BLOCK.fullmatch(raw_value.strip())
        value = _ASK_YAML_COMMENT.sub("", raw_value).strip()  # a comment alone leaves the value below
        collection = value == ""
        end = index + 1
        if scalar or collection:
            explicit = re.search(r"[1-9]", scalar.group("style")) if scalar else None
            explicit_indent = _ask_yaml_indent(field) + int(explicit.group()) if explicit else None
            end, value = _ask_yaml_scalar_end(lines, index, _ask_yaml_indent(field), explicit_indent, collection)
            collection = collection and end > index + 1
        if (field.group("quote") and not field.group("explicit") and paired_name is None and not scalar and not collection) or not (
            _ask_is_secret_key(secret_name, value)
        ):
            out.extend(lines[index:end])
            index = end
            continue
        body = lines[index].rstrip("\r\n")
        cut = field.start("value") + len(lines[index]) - len(views[index])  # entry and view share the value line
        head, newline = body[:cut], lines[index][len(body) :]
        tail = scalar.group("tail") if scalar else ""
        start = len(raw_value) - len(raw_value.lstrip())
        if not scalar and not collection and raw_value[start : start + 1] in {"[", "{"}:
            close = _ask_json_container_end(raw_value, start)
            if close is None:  # an unclosed container may continue on the lines below
                end, _ = _ask_yaml_scalar_end(lines, index, _ask_yaml_indent(field))
            elif not re.match(r"\s*,\s*[\"']?[A-Za-z0-9_.-]+[\"']?\s*:", raw_value[close:]):
                tail = raw_value[close:]
        out.append(f"{head}[redacted]{tail}{newline}")
        index = end
    return "".join(out)


def _ask_json_container_end(text: str, start: int) -> int | None:
    """Find one JSON container's end without recursing through its nesting."""
    if start >= len(text) or text[start] not in "[{(":
        return None
    expected = [{"[": "]", "{": "}", "(": ")"}[text[start]]]
    quote: str | None = None
    escaped = False
    for i in range(start + 1, len(text)):
        char = text[i]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "[":
            expected.append("]")
        elif char == "{":
            expected.append("}")
        elif char == "(":
            expected.append(")")
        elif char in "]})":
            if not expected or char != expected[-1]:
                return None
            expected.pop()
            if not expected:
                return i + 1
    return None


def _ask_quoted_end(text: str, start: int) -> int:
    quote = text[start]
    escaped = False
    for i in range(start + 1, len(text)):
        char = text[i]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            return i + 1
    return len(text)


def _ask_assignment_value_end(text: str, start: int) -> int:
    if start >= len(text):
        return start
    if text[start] == "\\":
        quote_start = start
        while quote_start < len(text) and text[quote_start] == "\\":
            quote_start += 1
        if quote_start < len(text) and text[quote_start] in "\"'":
            quote = text[quote_start]
            for end in range(quote_start + 1, len(text)):
                if text[end] != quote:
                    continue
                slashes = end - 1
                while slashes >= start and text[slashes] == "\\":
                    slashes -= 1
                if end - slashes > 1:
                    return end + 1
        line_end = text.find("\n", start)
        return line_end if line_end >= 0 else len(text)
    if text[start] in "\"'":
        return _ask_quoted_end(text, start)
    if text[start] in "[{(":
        end = _ask_json_container_end(text, start)
        if end is not None:
            return end
    end = start
    while end < len(text) and text[end] not in " \t\r\n,;}]":
        end += 1
    return end


def _ask_redact_assignments(text: str) -> str:
    out: list[str] = []
    cursor = 0
    for match in _ASK_SECRET_ASSIGNMENT.finditer(text):
        if match.start() < cursor:
            continue
        key = (match.group("flag") or "") + match.group("key")
        leading_colon = match.start() > 0 and text[match.start() - 1] == ":"
        if (
            not match.group("flag")
            and not leading_colon
            and not re.search(r"[=:]", match.group("sep"))
        ):
            continue
        start = match.end()
        end = _ask_assignment_value_end(text, start)
        if text[start : start + 1] in "[{(" and end > start:
            line_end = text.find("\n", end)
            suffix = text[end : line_end if line_end >= 0 else len(text)]
            if re.match(r"[ \t]*,[ \t]*[\"']?[A-Za-z0-9_.-]+[\"']?[ \t]*:", suffix):
                end = line_end if line_end >= 0 else len(text)
                suffix = ""
            extra = re.match(
                r"[ \t]+(?P<value>[A-Za-z0-9_.-]{1,32}(?:=[^ \t,;]+)?)", suffix
            )
            if extra and (
                "=" in extra.group("value")
                or re.search(r"[0-9_-]", extra.group("value"))
            ):
                end += extra.end()
        if end < start:
            continue
        raw = text[start:end]
        line_end = text.find("\n", start)
        prose_value = text[start : line_end if line_end >= 0 else len(text)].strip()
        colon = match.group("sep").lstrip().startswith(":")
        key_value = prose_value if colon and not match.group("flag") else raw
        if not _ask_is_secret_key(key, key_value):
            continue
        word = _ask_secret_word(key)
        value = raw.strip().strip("\"' ")
        if value.casefold() == "[redacted]":
            continue
        if (
            word in {"authorization", "authentication", "auth", "oauth"}
            and colon
            and not match.group("flag")
            and _ask_auth_value_is_prose(prose_value)
        ):
            continue
        if (
            word == "cookie"
            and colon
            and _ask_cookie_prose(prose_value)
            and not _ASK_AUTH_CREDENTIAL_MARKER.search(prose_value)
        ):
            continue
        out.append(text[cursor:start])
        if end == start:
            out.append("[redacted]")
            cursor = end
            continue
        quote = raw[0] if raw[:1] in ('"', "'") else ""
        out.append(f"{quote}[redacted]{quote}")
        cursor = end
    if not out:
        return text
    out.append(text[cursor:])
    return "".join(out)


_ASK_HTTP_BASIC_AUTH = re.compile(
    r"(?i)(?P<prefix>\bHTTPBasicAuth\(\s*)"
    r"(?P<user>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^,)]*|" + _SQ + r")"
    r"(?P<between>\s*,\s*)"
    r"(?P<password>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^)]*|" + _SQ + r")"
    r"(?P<suffix>\s*\))"
)


def _ask_redact_http_basic_auth(text: str) -> str:
    return _ASK_HTTP_BASIC_AUTH.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('user')}"
            f"{match.group('between')}[redacted]{match.group('suffix')}"
        ),
        text,
    )


def _ask_redact_short_password_flags(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        value = match.group("value").strip().strip("\"'")
        if re.fullmatch(r"\d+:\d+", value):
            return match.group(0)
        return f"{match.group('prefix')}[redacted]"

    return _ASK_SHORT_PASSWORD_FLAG.sub(replace, text)


def _ask_withhold_unscanned(rest: str) -> str:
    """The scan budget ran out: text that could hold a credential is withheld, not copied."""
    def risky(chunk: str) -> bool:
        return bool(_ASK_SECRET_KEY_RE.search(chunk) or "\\u" in chunk)

    out: list[str] = []
    i = 0
    scans = 0
    while i < len(rest):
        if rest[i] in "{[":
            scans += 1
            if scans > 256:  # still cheap and still closed: the remainder is withheld if it could matter
                out.append("[redacted: too much structured text to scan]" if risky(rest[i:]) else rest[i:])
                break
            end = _ask_json_container_end(rest, i)
            if end is None and risky(rest[i:]):
                # A container that cannot be delimited (an escaped or unclosed JSON string) cannot
                # be judged piece by piece: the remainder is withheld rather than copied.
                out.append("[redacted: too much structured text to scan]")
                break
            if end is not None and risky(rest[i:end]):
                out.append("[redacted]")
                i = end
                continue
        out.append(rest[i])
        i += 1
    return "".join(out)


def _ask_redact_embedded_json(text: str) -> str:
    """Redact valid JSON objects embedded in prose or escaped JSON log strings."""
    # A key spelled with escapes (``"\\u0070assword"``) has no literal credential word, so
    # any escape sequence also sends the text through the decoder.
    if not _ASK_SECRET_KEY_RE.search(text) and "\\u" not in text:
        return text
    decoder = json.JSONDecoder()
    out: list[str] = []
    i = 0
    failures = 0  # decodes that found nothing; successes advance and cost only their own length
    while i < len(text):
        if text[i] == '"' and text[i + 1 : i + 2] in ("{", "["):
            if failures > 128:
                out.append(_ask_withhold_unscanned(text[i:]))
                break
            try:
                value, end = decoder.raw_decode(text, i)
            except RecursionError:
                out.append(json.dumps("[redacted]"))
                i = _ask_quoted_end(text, i)
                continue
            except ValueError:
                value = None
                failures += 1
            if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
                try:
                    embedded = json.loads(value)
                except RecursionError:
                    out.append(json.dumps("[redacted]"))
                    i = end
                    continue
                except (ValueError, TypeError):
                    embedded = None
                if isinstance(embedded, (dict, list)):
                    try:
                        safe = _ask_redact_value(embedded)
                    except RecursionError:
                        out.append(json.dumps("[redacted]"))
                    else:
                        out.append(
                            json.dumps(
                                json.dumps(safe, ensure_ascii=False), ensure_ascii=False
                            )
                        )
                    i = end
                    continue
            failures += 1
        if text[i] in "{[":
            if failures > 128:
                out.append(_ask_withhold_unscanned(text[i:]))
                break
            try:
                value, end = decoder.raw_decode(text, i)
            except RecursionError:
                out.append("[redacted]")
                i = _ask_json_container_end(text, i) or len(text)
                continue
            except ValueError:
                value = None
                failures += 1
            if isinstance(value, (dict, list)):
                try:
                    safe = _ask_redact_value(value)
                except RecursionError:
                    out.append("[redacted]")
                else:
                    out.append(json.dumps(safe, ensure_ascii=False))
                i = end
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _ask_redact(text: str) -> str:
    home = os.path.expanduser("~")
    if home and home not in ("/", "~"):
        text = text.replace(home, "~")
    for rule in _ASK_SECRET_PATTERNS:
        text = rule(text) if callable(rule) else rule[0].sub(rule[1], text)
    text = _ask_redact_http_basic_auth(text)
    text = _ask_redact_short_password_flags(text)
    text = _ask_redact_cookie_headers(text)
    text = _ask_redact_authorization_headers(text)
    text = _ask_redact_bare_credential_values(text)
    text = _ask_redact_yaml(text)
    text = _ask_redact_embedded_json(text)
    text = _ask_redact_assignments(text)
    return _ask_redact_numeric_codes(_ASK_GLUED.sub("[redacted]", text))


def _ask_workspace_path(value: Any) -> str:
    """The ``workspace.path`` of a card or JSON object, trimmed, or "" when it has none worth hiding."""
    workspace = value.get("workspace") if isinstance(value, dict) else None
    path = workspace.get("path") if isinstance(workspace, dict) else None
    path = path.strip().rstrip("/") if isinstance(path, str) else ""
    return path if len(path) > 1 else ""


def _ask_redact_value(value: Any, scrub: str | None = None, _key: str = "") -> Any:
    """*value* with every string redacted and every credential-named field's value replaced: the
    second line for JSON found inside text. An object's own workspace path is dropped and replaced
    wherever else it appears. What Ask sends is chosen by :func:`_ask_card_payload`."""
    scrub = _ask_workspace_path(value) if scrub is None else scrub
    if isinstance(value, dict):
        named = [
            v for k, v in value.items()
            if str(k).casefold() in {"name", "key"} and isinstance(v, str) and _ask_names_credential(v)
        ]
        return {
            _ask_redact_value(k, scrub) if isinstance(k, str) else k: "[redacted]"
            if _ask_is_secret_key(str(k), v)
            or str(k).casefold() in {"value", "val", "default"}
            and any(_ask_secret_word(n) is not None or _ask_is_secret_key(n, v) for n in named)
            else _ask_redact_value(v, scrub, str(k))
            for k, v in value.items()
            if not (_key == "workspace" and k == "path")
        }
    if isinstance(value, (list, tuple)):
        return [_ask_redact_value(v, scrub, _key) for v in value]
    return _ask_redact(value.replace(scrub, "[workspace]") if scrub else value) if isinstance(value, str) else value
