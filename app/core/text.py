"""Text normalization for untrusted email content (stdlib only)."""

from __future__ import annotations

import html
import re
import unicodedata
from html.parser import HTMLParser

_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "blockquote", "hr"}
)
_SKIP_TAGS = frozenset({"script", "style", "head", "title", "noscript", "template", "svg", "object", "iframe"})
# Zero-width and bidi control characters are a common way to hide instructions from human reviewers.
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")

# Unicode `Default_Ignorable_Code_Point` (DerivedCoreProperties.txt): rendered as nothing, so they can split a
# phrase without a visible trace. `normalize_text` already drops the format (Cf) and unassigned ones; the marks
# and fillers among them (variation selectors, U+034F, U+17B4/5, U+180B-F, Hangul fillers) survive it.
_DEFAULT_IGNORABLE = (
    "\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e\u2060-\u206f"
    "\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff"
)
_DEFAULT_IGNORABLE_RE = re.compile(f"[{_DEFAULT_IGNORABLE}]")
# Invisible-character tricks: bidi overrides (reversed text), Unicode tag characters (invisible ASCII) and runs of
# 3+ default-ignorable characters. Subdivision-flag emoji (black flag + tag letters + cancel tag) are legitimate
# tag sequences and are skipped. Each branch is linear: the run is possessive and the flag branch is bounded.
_FLAG_TAG_SEQUENCE = "\U0001f3f4[\U000e0030-\U000e0039\U000e0061-\U000e007a]{2,6}\U000e007f"
_HIDDEN_TRICK_RE = re.compile(
    f"(?P<flag>{_FLAG_TAG_SEQUENCE})|[{_DEFAULT_IGNORABLE}]{{3,}}+|[\u202d\u202e\U000e0000-\U000e007f]"
)
# Visible stand-in left at ingestion where such characters were removed, so the analysis can still report them.
HIDDEN_TEXT_MARKER = "[hidden characters removed]"
_REPEATED_MARKER_RE = re.compile(f"(?:{re.escape(HIDDEN_TEXT_MARKER)})+")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(markup: str, *, mark_hidden: bool = False) -> str:
    """Convert HTML to readable plain text; drops scripts, styles and comments."""
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return normalize_text(html.unescape(parser.text()), mark_hidden=mark_hidden)


def normalize_text(value: str, *, mark_hidden: bool = False) -> str:
    """NFKC-normalize, remove invisible/control characters, and collapse excessive blank lines.

    With `mark_hidden`, every invisible-character trick (see `find_hidden_characters`) is replaced by
    `HIDDEN_TEXT_MARKER` instead of disappearing silently, so the signal survives storage of the clean text.
    """
    # Line endings first: "\r" is a control character and would otherwise be dropped, joining lines.
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if mark_hidden:
        # Other controls go first, so invisible characters they separate are marked as the one run they become.
        value = mark_hidden_characters(_drop_controls(value, keep_ignorable=True))
    value = _drop_controls(_INVISIBLE_RE.sub("", value))
    # NFKC after the removals: removing a character can join a base letter and a combining mark (or Hangul jamo)
    # that NFKC composes, so normalizing first made a second pass change the text. NFKC never produces a control,
    # format or line-break character, so nothing has to be removed after it.
    value = unicodedata.normalize("NFKC", value)
    value = _TRAILING_WS_RE.sub("\n", value)
    return _MULTI_BLANK_RE.sub("\n\n", value).strip()


def _drop_controls(value: str, *, keep_ignorable: bool = False) -> str:
    """Remove control/format/unassigned characters except newline and tab (and, optionally, default-ignorables)."""
    return "".join(
        ch
        for ch in value
        if ch in "\n\t"
        or unicodedata.category(ch)[0] != "C"
        or (keep_ignorable and _DEFAULT_IGNORABLE_RE.match(ch) is not None)
    )


def strip_default_ignorable(value: str) -> str:
    """Remove every default-ignorable code point (the injection screener's view of the text)."""
    return _DEFAULT_IGNORABLE_RE.sub("", value)


def find_hidden_characters(value: str) -> re.Match[str] | None:
    """First invisible-character trick in `value` (bidi override, tag characters, run of 3+ invisibles)."""
    for match in _HIDDEN_TRICK_RE.finditer(value):
        if match.lastgroup != "flag":
            return match
    return None


def mark_hidden_characters(value: str) -> str:
    """Replace each invisible-character trick with `HIDDEN_TEXT_MARKER` (adjacent markers collapse into one)."""
    marked = _HIDDEN_TRICK_RE.sub(lambda m: m.group(0) if m.lastgroup == "flag" else HIDDEN_TEXT_MARKER, value)
    return _REPEATED_MARKER_RE.sub(HIDDEN_TEXT_MARKER, marked) if marked != value else value


def truncate(value: str, max_chars: int, marker: str = "\n[…truncated…]") -> tuple[str, bool]:
    if len(value) <= max_chars:
        return value, False
    return value[: max(0, max_chars - len(marker))] + marker, True
