"""Readable text from exported mail and saved web pages: an inert HTML-to-text reader that keeps
only what a browser would show, and the body parts of a saved message. Pure: reads no files or
settings; ``needs`` opens the export and applies its own read limit.
"""

from __future__ import annotations

import collections
import html
import html.parser
import re
import unicodedata
from typing import Any

_HTML_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
# Elements whose content is never page text. <head> is not one: text in it starts the body.
_HTML_DROP = frozenset(
    "script style title noscript template iframe frame frameset object embed applet svg math canvas "
    "audio video select".split()
)
_HTML_FOREIGN = frozenset(("svg", "math"))  # a self-closing tag really is empty only here
_HTML_BLOCK = frozenset(
    "p div section article header footer main nav aside blockquote pre table tr ul ol dl dt dd h1 h2 h3 h4 h5 h6 "
    "form fieldset figure figcaption address hr center".split()
)
_HTML_CLOSES_P = frozenset(
    "address article aside blockquote center details dialog dir div dl fieldset figcaption figure footer "
    "form h1 h2 h3 h4 h5 h6 header hgroup hr main menu nav ol p pre section table ul li dd dt".split()
)
_HTML_TABLE_PARTS = frozenset("tr tbody thead tfoot caption col colgroup".split())
_HTML_TABLE_ROOTS = _HTML_TABLE_PARTS | {"td", "th"}
_HTML_RUBY = frozenset("rb rt rtc rp".split())
# HTML's optional end tags: an open element of each key ends when one of these start tags arrives,
# unless an element between them stops the search (table parts look only as far as the table, the
# others stop at any scope element).
_HTML_ENDED_BY = {
    "p": _HTML_CLOSES_P, "li": frozenset(("li",)), "dt": frozenset(("dt", "dd")), "dd": frozenset(("dt", "dd")),
    "td": _HTML_TABLE_ROOTS, "th": _HTML_TABLE_ROOTS, "caption": _HTML_TABLE_ROOTS, "tr": _HTML_TABLE_PARTS,
    "tbody": _HTML_TABLE_PARTS - {"tr"}, "thead": _HTML_TABLE_PARTS - {"tr"}, "tfoot": _HTML_TABLE_PARTS - {"tr"},
    **{t: _HTML_RUBY for t in _HTML_RUBY},
}
_HTML_ENDS = {t: frozenset(k for k, v in _HTML_ENDED_BY.items() if t in v) for t in frozenset().union(*_HTML_ENDED_BY.values())}
_HTML_TABLE_SCOPE = frozenset(("table", "template", "html"))
_HTML_SCOPE = frozenset("table td th caption html button object marquee applet template ul ol dl".split())
# Text placed directly in these is moved in front of the table by HTML (foster parenting).
_HTML_NO_TEXT = frozenset("table tbody thead tfoot tr colgroup".split())
# An element HTML may reopen after another element's end closed it.
_HTML_FORMATTING = frozenset("a b big code em font i nobr s small strike strong tt u".split())
_HTML_MAX_OPEN = 1000  # open elements tracked; deeper nesting ends the readable text, and is reported
_HTML_FLUSH = "\n</unbl0ck-flush>"  # an end tag no element has: flushes held-back text, changes nothing


class _HtmlStop(Exception):
    """Raised inside the converter to end it early; the result is reported as incomplete."""


_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|\Z)", re.S)
# CSS whitespace is only space, tab, LF, CR and FF, and CSS lowercases ASCII only: any other
# character is part of a token, so Unicode spaces or case folds cannot turn a value into a keyword.
_CSS_WS = " \t\n\r\f"
_CSS_LOWER = {c: c + 32 for c in range(65, 91)}
_CSS_DECL = re.compile(r"([-\w]+)[ \t\n\r\f]*:[ \t\n\r\f]*([^;{}]*)")
_CSS_ZERO = re.compile(r"^[+-]?(?:0+(?:\.0*)?|\.0+)(?:[a-z]+|%)?$")
_CSS_ABS_LENGTH = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:px|pt|pc|cm|mm|in|q|rem|vw|vh|vmin|vmax)$")
_CSS_REL_LENGTH = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:em|ex|ch|cap|ic|lh|rlh|%)$")
_CSS_FONT_KEYWORDS = frozenset(
    "xx-small x-small small medium large x-large xx-large xxx-large math".split()
)
_CSS_INHERIT = frozenset(("inherit", "unset", "revert", "revert-layer"))
_CSS_IMPORTANT = re.compile(r"[ \t\n\r\f]*![ \t\n\r\f]*important[ \t\n\r\f]*\Z")
_CSS_SIZES = ("width", "height", "max-width", "max-height")
_CSS_OVERFLOW = ("overflow", "overflow-x", "overflow-y")
# The properties that can hide an element, per way it was hidden; a stylesheet setting one of them
# can show it again (only with !important against an inline style).
_CSS_CAUSES = {
    "drop": (), "hidden": ("display",), "display": ("display",), "opacity": ("opacity",),
    "clip": _CSS_OVERFLOW + _CSS_SIZES, "visibility": ("visibility",), "font-size": ("font-size",),
}
_CSS_INLINE_WINS = frozenset(("display", "opacity", "clip"))
_CSS_HIDING = frozenset(p for props in _CSS_CAUSES.values() for p in props)
_CSS_BIG_INT = re.compile(
    r"&#(?:[0-9]{100,}|0*[1-9][0-9]{7,}|[xX](?:[0-9a-fA-F]{100,}|0*[1-9a-fA-F][0-9a-fA-F]{6,}));?"
)


def _css_declarations(block: str) -> dict[str, list[str]]:
    """``{property: [values]}`` for the hiding properties in one declaration block, in order, keeping
    only the ``!important`` values when a property has any. Property names are matched whole
    (``min-width`` is never ``width``); custom properties (``--x``) are not properties. Two different
    values for one property mean the block disagrees with itself, which is not resolved here."""
    found: dict[str, list[tuple[str, bool]]] = {}
    for piece in _CSS_COMMENT.sub("", block).split(";"):
        name, sep, value = piece.partition(":")
        name = _css_text(name)
        if sep and name in _CSS_HIDING:
            value = _css_text(value)
            found.setdefault(name, []).append((_CSS_IMPORTANT.sub("", value), bool(_CSS_IMPORTANT.search(value))))
    return {n: [v for v, imp in vs if imp] or [v for v, _ in vs] for n, vs in found.items()}


def _css_text(raw: str) -> str:
    return raw.strip(_CSS_WS).translate(_CSS_LOWER)


def _css_zero(value: str) -> bool:
    return bool(_CSS_ZERO.match(value))


def _css_vis_kind(value: str) -> str | None:
    if value in ("hidden", "collapse"):
        return "hide"
    if value in ("visible", "initial"):
        return "reset"
    return "keep" if value in _CSS_INHERIT else None


def _css_font_kind(value: str) -> str | None:
    if _css_zero(value):
        return "hide"
    if value == "initial" or value in _CSS_FONT_KEYWORDS or _CSS_ABS_LENGTH.match(value):
        return "reset"
    if value in _CSS_INHERIT or value in ("smaller", "larger") or _CSS_REL_LENGTH.match(value):
        return "keep"
    return None


# CSS Display 3: one keyword, or one word from each of outside, inside and list-item (list-item
# only with a flow inside); any other combination is invalid and does not override ``hidden``.
_CSS_DISPLAY_GROUPS = ({"block", "inline", "run-in"}, {"flow", "flow-root", "table", "flex", "grid", "ruby", "math"}, {"list-item"})
_CSS_DISPLAY_WORDS = frozenset(
    "contents inline-block inline-table inline-flex inline-grid table-row-group table-header-group "
    "table-footer-group table-row table-cell table-column-group table-column table-caption ruby-base "
    "ruby-text ruby-base-container ruby-text-container initial unset inherit".split()
).union(*_CSS_DISPLAY_GROUPS)


def _css_display_shows(value: str) -> bool:
    """Whether an inline ``display`` value is a real one that overrides the ``hidden`` attribute."""
    words = re.split(r"[ \t\n\r\f]+", value)
    picks = [g.intersection(words) for g in _CSS_DISPLAY_GROUPS]
    if len(words) == 1:
        return words[0] in _CSS_DISPLAY_WORDS
    return len(set(words)) == len(words) == sum(map(len, picks)) and max(map(len, picks)) == 1 and not (
        picks[2] and picks[1] - {"flow", "flow-root"})


def _css_could_hide(name: str, value: str) -> bool:
    return (
        (name == "display" and value == "none")
        or (name == "visibility" and value in ("hidden", "collapse"))
        or (name in ("opacity", "font-size") + _CSS_SIZES and _css_zero(value))
    )


class _Region:
    """Text hidden by one element: how it was hidden, whether the converter doubts it, and whether
    the element is still open."""

    __slots__ = ("cause", "doubt", "open")

    def __init__(self, cause: str, doubt: bool = False) -> None:
        self.cause, self.doubt, self.open = cause, doubt, True


def _visibility_uncertain(region: _Region, sheet: dict[str, bool]) -> bool:
    """Whether the converter cannot be sure *region* hides its text. It is uncertain when its own
    markup was (a conflicting or unmodelled declaration, an end tag that may have been implied
    earlier, a formatting element HTML may reopen), or when a stylesheet sets a property that can
    show it again. Stylesheet rules are never matched to elements: uncertain text is kept, with a note."""
    if region.doubt:
        return True
    inline_wins = region.cause in _CSS_INLINE_WINS
    return any(p in sheet and (sheet[p] or not inline_wins) for p in _CSS_CAUSES[region.cause])


def _css_inherited(vals: list[str], parent: _Region | None, cause: str, kind) -> tuple[_Region | None, _Region | None]:
    """(region in force, region this element starts) for an inherited property (visibility,
    font-size) that a descendant can set back."""
    if not vals:
        return parent, None
    kinds = {kind(v) for v in vals}
    if len(kinds) > 1 or None in kinds:
        if parent is None and "hide" not in kinds:
            return None, None
        r = _Region(cause, doubt=True)
        return r, r
    (k,) = kinds
    if k == "hide":
        r = _Region(cause)
        return r, r
    return (None if k == "reset" else parent), None


class _Frame:
    __slots__ = ("tag", "gone", "vis", "font", "own", "bounds")

    def __init__(self, tag, gone, vis, font, own, bounds):
        self.tag, self.gone, self.vis, self.font, self.own, self.bounds = tag, gone, vis, font, own, bounds


class _InertText(html.parser.HTMLParser):
    """HTML to plain text without interpreting any of it: nothing is fetched, no attribute value
    is kept (links, image sources and alt text never reach the output), and script, style, frames
    and embedding contribute no text. Text an element's ``hidden`` attribute or inline style hides
    is recorded with what hid it; ``_html_convert`` drops it only when that is certain
    (``_visibility_uncertain``), and otherwise keeps it with a note."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[tuple[str, tuple[_Region, ...]]] = []
        self.stack: list[_Frame] = []
        self.open_count: collections.Counter[str] = collections.Counter()
        self.sheet: dict[str, bool] = {}  # hiding property a stylesheet sets -> with !important
        self.sheet_hides = False  # a stylesheet declares something that hides

    @staticmethod
    def _causes(f: _Frame | None) -> tuple[_Region, ...]:
        return tuple(r for r in (f.gone, f.vis, f.font) if r) if f else ()

    def _emit(self, text: str, causes: tuple[_Region, ...] | None = None) -> None:
        self.out.append((text, self._causes(self.stack[-1] if self.stack else None) if causes is None else causes))

    def _pop(self, i: int, by_start: bool = False) -> None:
        """Close the open element at *i* and everything inside it. A hidden element closed by
        anything but its own end tag is uncertain when HTML may have ended it earlier (an optional
        end tag implied by a start tag) or may reopen it (a formatting element)."""
        for f in self.stack[i:]:
            self.open_count[f.tag] -= 1
            implied = by_start or f is not self.stack[i]
            doubt = implied and (f.tag in _HTML_FORMATTING or (by_start and f.tag in _HTML_ENDED_BY))
            for r in f.own:
                r.open, r.doubt = False, r.doubt or doubt
        del self.stack[i:]

    def _nearest(self, tag: str) -> int:
        return max(i for i, f in enumerate(self.stack) if f.tag == tag)

    def _implied_end(self, tag: str) -> None:
        top, cut = self.stack[-1], None
        for root in _HTML_ENDS.get(tag, ()):
            if self.open_count[root]:
                i = self._nearest(root)
                k = 0 if root in _HTML_TABLE_ROOTS else 1
                if top.bounds[k] == self.stack[i].bounds[k]:  # nothing between stops the search
                    cut = i if cut is None else min(cut, i)
        if cut is not None:
            self._pop(cut, by_start=True)

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if self.stack:
            self._implied_end(tag)
        parent = self.stack[-1] if self.stack else None
        if tag in _HTML_VOID:
            if tag == "br":
                self._emit("\n")
            return
        if len(self.stack) >= _HTML_MAX_OPEN:
            raise _HtmlStop
        gone, vis, font = (parent.gone, parent.vis, parent.font) if parent else (None, None, None)
        own: tuple[_Region, ...] = ()
        if tag in _HTML_DROP:  # inert whatever its ancestors' visibility, never borrowing their doubt
            gone = _Region("drop")
            own = (gone,)
        elif gone is None:
            a = {k: (v or "") for k, v in attrs}
            d = _css_declarations(a.get("style", ""))
            disp = d.get("display", [])
            clip = any(v in ("hidden", "clip", "scroll", "auto") for p in _CSS_OVERFLOW for v in d.get(p, ()))
            cause = (
                "display" if "none" in disp
                else "hidden" if "hidden" in a and not (disp and all(map(_css_display_shows, disp)))
                else "opacity" if any(_css_zero(v) for v in d.get("opacity", ()))
                else "clip" if clip and any(_css_zero(v) for p in _CSS_SIZES for v in d.get(p, ()))
                else None
            )
            if cause:
                # ``hidden`` beside a display value the converter does not know is a doubt, not a show.
                doubt = cause == "hidden" and bool(disp)
                gone = _Region(cause, doubt or any(len(set(d.get(p, ()))) > 1 for p in _CSS_CAUSES[cause]))
                own = (gone,)
            else:
                vis, v_own = _css_inherited(d.get("visibility", []), vis, "visibility", _css_vis_kind)
                font, f_own = _css_inherited(d.get("font-size", []), font, "font-size", _css_font_kind)
                own = tuple(r for r in (v_own, f_own) if r)
        pb = parent.bounds if parent else (0, 0)
        bounds = (pb[0] + (tag in _HTML_TABLE_SCOPE), pb[1] + (tag in _HTML_SCOPE))
        frame = _Frame(tag, gone, vis, font, own, bounds)
        self.stack.append(frame)
        self.open_count[tag] += 1
        if tag == "li":
            self._emit("\n- ")
        elif tag in _HTML_BLOCK:
            self._emit("\n")
        elif tag in ("td", "th"):
            self._emit(" ")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        # A trailing slash empties only void and foreign elements; anywhere else HTML ignores it.
        self.handle_starttag(tag, attrs)
        if tag in _HTML_FOREIGN and self.stack and self.stack[-1].tag == tag:
            self._pop(len(self.stack) - 1)

    def handle_endtag(self, tag: str) -> None:
        if not self.open_count[tag]:
            return
        causes = self._causes(self.stack[-1])
        self._pop(self._nearest(tag))
        if tag in _HTML_BLOCK:
            self._emit("\n", causes)

    def handle_data(self, data: str) -> None:
        top = self.stack[-1] if self.stack else None
        if top and top.tag == "style" and not self.open_count["template"]:
            for name, value in _CSS_DECL.findall(_CSS_COMMENT.sub("", data)):
                name, value = _css_text(name), _css_text(value)
                if name in _CSS_HIDING:
                    important = bool(_CSS_IMPORTANT.search(value))
                    self.sheet[name] = self.sheet.get(name, False) or important
                    self.sheet_hides |= _css_could_hide(name, _CSS_IMPORTANT.sub("", value))
        if top and top.tag in _HTML_NO_TEXT and data.strip() and self.open_count["table"]:
            i = self._nearest("table")
            self._emit(data, self._causes(self.stack[i - 1] if i else None))  # shown before the table
            return
        self._emit(data)


def html_to_text(markup: str, limit: int) -> str:
    """Readable, inert text from HTML, at most *limit* characters. Never raises."""
    return _html_convert(markup, limit)[0]


def _html_convert(markup: str, limit: int) -> tuple[str, list[str]]:
    """Inert text, at most *limit* characters, and a note for each way it is incomplete or
    uncertain: text left out because it sat in an element that is hidden (by its ``hidden``
    attribute or inline style) or never closed, markup the reader could not finish, text past
    *limit*, and hidden-looking text kept because whether it is hidden could not be decided."""
    markup = _CSS_BIG_INT.sub("�", markup.replace("\x00", ""))
    notes: list[str] = []
    parser = _InertText()
    incomplete = False
    try:
        # The trailing tag flushes text the parser holds back. What it cannot flush (an unterminated
        # comment, tag or quoted attribute) stays in rawdata and is reported, never shown.
        parser.feed(markup + _HTML_FLUSH)
        if parser.rawdata.strip():
            incomplete = True
    except _HtmlStop:
        incomplete = True
    except Exception:  # noqa: BLE001 - hostile or broken markup: keep what was read, and say so
        incomplete = True
    sheet = parser.sheet
    swallowed = any(r.open and not _visibility_uncertain(r, sheet) for f in parser.stack for r in f.own)
    uncertain = parser.sheet_hides
    kept: list[str] = []
    for piece, causes in parser.out:
        hiding = [r for r in causes if not _visibility_uncertain(r, sheet)]
        if not hiding:
            kept.append(piece)
            uncertain = uncertain or bool(causes and piece.strip())
        elif piece.strip() and any(r.cause != "drop" for r in hiding):
            swallowed = True
    # Format characters can hide text and are dropped, except the joiners words and emoji need.
    text = "".join(ch for ch in "".join(kept) if ch in "‌‍" or unicodedata.category(ch) != "Cf")
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in text.split("\n")]
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if swallowed:
        notes.append(_HTML_SWALLOWED)
    if incomplete:
        notes.append(_HTML_INCOMPLETE)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit]
        notes.append(_HTML_CUT)
    if uncertain:
        notes.append(_HTML_STYLE_NOTE)
    return cleaned, notes


_HTML_NOTE = "(Converted from HTML; formatting, images and links are not shown.)\n\n"
_NO_BODY = "(No readable text body; this message has only attachments or parts that are not read.)"
_HTML_SWALLOWED = (
    "(Part of this HTML body is not shown: it sits inside an element that is hidden or never closed.)"
)
_HTML_INCOMPLETE = (
    "(Part of this HTML body is not shown: it is nested too deeply, is malformed or could not be read.)"
)
_HTML_CUT = "(Part of this HTML body is not shown: it is longer than the converter reads.)"
_HTML_STYLE_NOTE = (
    "(Some styles or markup in this HTML body were not evaluated, so text they hide may appear here.)"
)
# Headers a mail client writes when it saves an unsent message, and headers a receiving server adds.
_DRAFT_MARKERS = (("x-unsent", lambda v: v.strip() == "1"), ("x-mozilla-draft-info", lambda v: True),
                  ("x-uniform-type-identifier", lambda v: "mail-draft" in v.lower()))
_RECEIVED_MARKERS = ("received", "return-path", "delivered-to", "x-received")


def _decode_part(part: Any) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeError):
        return payload.decode("utf-8", errors="replace")


def _has_visible_text(text: str) -> bool:
    """Whether *text* shows anything: whitespace, control and format characters (zero-width
    spaces, byte-order marks, NULs, soft hyphens) alone are not a message."""
    return any(not ch.isspace() and unicodedata.category(ch) not in ("Cc", "Cf", "Zs", "Zl", "Zp") for ch in text)
