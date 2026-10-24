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


def html_to_text(markup: str) -> str:
    """Convert HTML to readable plain text; drops scripts, styles and comments."""
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return normalize_text(html.unescape(parser.text()))


def normalize_text(value: str) -> str:
    """NFKC-normalize, remove invisible/control characters, and collapse excessive blank lines."""
    value = unicodedata.normalize("NFKC", value)
    value = _INVISIBLE_RE.sub("", value)
    value = "".join(ch for ch in value if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _TRAILING_WS_RE.sub("\n", value)
    return _MULTI_BLANK_RE.sub("\n\n", value).strip()


def truncate(value: str, max_chars: int, marker: str = "\n[…truncated…]") -> tuple[str, bool]:
    if len(value) <= max_chars:
        return value, False
    return value[: max(0, max_chars - len(marker))] + marker, True
