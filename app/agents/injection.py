"""Deterministic prompt-injection screener for untrusted email text.

Flags are advisory: they are stored, shown to the reviewer and lower the confidence score. They never
grant or block anything by themselves. Rules favour precision over recall so ordinary business email
(“please approve this PO”, “when I call the API it returns 500”) is not flagged; phrases that are
common in legitimate mail are only flagged when AI-directed context appears nearby.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass
from typing import Literal

from app.core.text import HIDDEN_TEXT_MARKER, find_hidden_characters, strip_default_ignorable
from app.schemas.email import InjectionFlag

Severity = Literal["low", "medium", "high"]

MAX_EXCERPT_CHARS = 200
_CONTEXT_WINDOW = 160
_EXCERPT_PAD = 60

# Words that indicate a phrase is addressed to an AI system or tries to move secrets.
_AI_CONTEXT = re.compile(
    r"\b(?:ai|a\.i\.|assistant|chat\s?bot|bot|llm|language\s+model|ai\s+model|gpt|copilot|nuvexa|automation|"
    r"automated\s+system|agent\s+instructions|you\s+must|you\s+are\s+required|system\s+prompt|api[\s_-]?keys?|"
    r"access\s+tokens?|secrets?|credentials|passwords?|without\s+(?:telling|informing|notifying))\b",
    re.IGNORECASE,
)
_BYPASS_CONTEXT = re.compile(
    r"\b(?:no\s+(?:further\s+)?(?:review|approval|checks?)|human|manual|ai|bot|automation|automatically|"
    r"no\s+need|(?:don'?t|do\s+not)\s+(?:need|check|review|ask)|not\s+(?:needed|required|necessary)|skip|bypass|"
    r"without\s+(?:review|approval))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class InjectionRule:
    name: str
    severity: Severity
    pattern: re.Pattern[str]
    # When set, the rule only fires if this pattern also matches near the hit (± _CONTEXT_WINDOW chars).
    context: re.Pattern[str] | None = None

    def search(self, text: str) -> re.Match[str] | None:
        for match in self.pattern.finditer(text):
            if self.context is None:
                return match
            window = text[max(0, match.start() - _CONTEXT_WINDOW) : match.end() + _CONTEXT_WINDOW]
            # The context must be found outside the matched phrase itself.
            outside = window.replace(match.group(0), " ")
            if self.context.search(outside):
                return match
        return None


def _rx(pattern: str, flags: int = re.IGNORECASE) -> re.Pattern[str]:
    return re.compile(pattern, flags)


_NOUNS = r"(?:instructions?|prompts?|directives?|rules|guidelines|guardrails|programming|system\s+prompt|context)"

RULES: tuple[InjectionRule, ...] = (
    InjectionRule(
        "instruction_override",
        "high",
        _rx(
            r"\b(?:ignore|disregard|forget|override|bypass)\s+(?:(?:all|any|every|of|the|these|those)\s+){0,3}"
            r"(?:(?:previous|prior|above|earlier|preceding|former|original|system)\s+" + _NOUNS + r"|"
            r"your\s+(?:(?:previous|prior|original|current|system)\s+)?" + _NOUNS + r"|"
            r"(?:all|any)\s+(?:(?:of\s+)?(?:the|your)\s+)?" + _NOUNS + r")"
        ),
    ),
    InjectionRule(
        "role_hijack",
        "high",
        _rx(
            r"\byou\s+are\s+(?:now|no\s+longer)\s+(?:(?:a|an|the|in|my)\s+)?(?:[\w-]+\s+){0,3}?"
            r"(?:ai|assistant|chat\s?bot|bot|llm|dan|jailbroken|unrestricted|unfiltered|developer\s+mode|"
            r"god\s+mode|admin\s+mode)\b"
            r"|\b(?:pretend\s+(?:to\s+be|you\s+are)|act\s+as(?:\s+if\s+you\s+(?:are|were))?|behave\s+as)\s+"
            r"(?:an?\s+|the\s+)?(?:(?:different|new|unrestricted|unfiltered|jailbroken|evil|rogue)\s+)?"
            r"(?:ai(?:\s+assistant)?|chat\s?bot|llm|(?:large\s+)?language\s+model|gpt|dan)\b"
            r"|\b(?:new|updated|revised|real|actual|true|override)\s+system\s+(?:prompt|instructions?|message|role)\b"
            r"|\b(?:developer|dan|god|jailbreak)\s+mode\b"
        ),
    ),
    InjectionRule(
        "fake_role_tag",
        "high",
        re.compile(
            r"<\|\s*(?:im_start|im_end|system|user|assistant|endoftext)\s*\|>"
            r"|\[/?(?:INST|SYS)\]|<</?SYS>>|</?(?:system|assistant)>"
            # Forged data-block delimiters (`<<<END_EMAIL_DATA_{nonce}>>>`); prompts.neutralize defangs them.
            # Linear on runs of "<" and of whitespace: starts only at the first "<" of a run and never backtracks.
            r"|(?<!<)<{2,}+\s*+/?\s*+(?:END[\s_-]*+)?(?:EMAIL|ANALYSIS)[\s_-]*+DATA"
            r"|\b(?:END_)?(?:EMAIL|ANALYSIS)_DATA_[0-9a-f]{8,}\b"
            r"|^[ \t>]*#{2,}\s*(?:system|assistant)\s*(?::|$)"
            r"|(?-i:^[ \t>]*\[?(?:SYSTEM|ASSISTANT|DEVELOPER)\]?\s*:)"
            r"|^[ \t>]*\[?(?:system|assistant|developer)\]?\s*:\s*(?=(?:you|ignore|disregard|override|new|from\s+now|"
            r"always|never|do\s+not|don'?t|mark|approve|classify|set|assign|respond|reply|print|reveal|forward|"
            r"call|send|execute|run)\b)",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    InjectionRule(
        "approval_bypass",
        "high",
        _rx(
            r"\bauto[-\s]?approv\w*"
            r"|\b(?:approve|execute)\s+(?:[\w-]+\s+){0,4}?(?:automatically|without\s+(?:any\s+)?(?:human\s+|manual\s+)?"
            r"(?:review|approval|confirmation|sign-?off|checks?))\b"
            r"|\bautomatically\s+approve\b"
            r"|\b(?:skip|bypass|circumvent)\s+(?:the\s+|any\s+|all\s+)?(?:human\s+|manual\s+)?(?:review|approval)s?\b"
            r"(?!\s+(?:meeting|call|session|board|cycle))"
            r"|\bno\s+need\s+for\s+(?:a\s+|any\s+)?(?:human|manual)\b"
            r"|\bno\s+(?:human|manual)\s+(?:review|approval|check|oversight)\s+(?:is\s+)?(?:needed|required|necessary)\b"
            r"|\bwithout\s+(?:any\s+)?(?:human|manual)\s+(?:review|approval|oversight|confirmation)\b"
        ),
    ),
    InjectionRule(
        "approval_claim",
        "medium",
        _rx(r"\b(?:already|pre-?)\s*(?:been\s+)?approved\b"),
        context=_BYPASS_CONTEXT,
    ),
    InjectionRule(
        "secret_exfiltration",
        "high",
        _rx(
            r"\b(?:reveal|print|show|display|output|repeat|leak|dump|disclose|expose|tell\s+me|give\s+me|"
            r"send\s+me|share)\s+(?:[\w-]+\s+){0,3}?"
            r"(?:system\s+prompt|(?:initial|hidden|original|internal|developer)\s+(?:prompt|instructions|message)|"
            r"your\s+(?:instructions|prompt|rules|configuration)|api[\s_-]?keys?|access[\s_-]?tokens?|"
            r"bearer\s+tokens?|secret\s+keys?|client[\s_-]?secrets?|private\s+keys?|connection\s+strings?|"
            r"env(?:ironment)?\s+variables|(?:admin|root|database|service\s+account)\s+passwords?)\b"
            r"(?!\s*(?:reset|change|expir|polic|manager|requirement|rotation))"
        ),
    ),
    InjectionRule(
        "tool_steering",
        "high",
        _rx(
            r"\bpropose_(?:azure_devops_work_item|email_reply|follow_up_task|no_action)\b|\b(?:call|invoke)\s+"
            r"(?:the\s+)?(?:\w+\s+)?(?:tool|function)\s+(?:named\s+)?[`'\"]?\w+_\w+"
        ),
    ),
    InjectionRule(
        "tool_steering_external",
        "medium",
        _rx(
            r"\b(?:call|invoke|hit|trigger)\s+(?:the|this|your|an?)\s+(?:[\w-]+\s+)?(?:api|endpoint|webhook)\b"
            r"|\b(?:send|forward)\s+(?:an?\s+|this\s+|the\s+|all\s+(?:the\s+)?)?(?:e-?mails?|cop(?:y|ies)|messages?|"
            r"conversation|thread|data)\s+(?:[\w-]+\s+){0,3}?to\b"
            r"|\b(?:POST|PUT|PATCH|DELETE)\s+(?:(?:it|this|them|the\s+\w+)\s+)?(?:to\s+)?https?://"
            r"|\b(?:upload|exfiltrate)\s+(?:[\w-]+\s+){0,4}?to\s+https?://"
        ),
        context=_AI_CONTEXT,
    ),
    InjectionRule(
        "priority_manipulation",
        "medium",
        _rx(
            # `\s++` is possessive: a long whitespace run would otherwise backtrack quadratically into the `\s*`.
            r"\b(?:mark|set|flag|classify|label|tag)\s++(?:this|it|the\s+(?:email|ticket|issue|request|item|"
            r"work\s+item|message))?\s*(?:email\s+|ticket\s+|issue\s+)?(?:as|to)\s+(?:an?\s+)?"
            r"(?:critical|p[01]\b|sev(?:erity)?[\s-]*[01]\b|priority[\s-]*(?:1|one|critical|highest)\b|highest|"
            r"top\s+priority)"
            r"|\bset\s+(?:the\s+)?(?:priority|severity)\s+(?:to|=|as)\s+(?:1\b|critical|highest|p[01]\b|sev[\s-]*[01]\b)"
        ),
    ),
    InjectionRule(
        "hidden_text",
        "medium",
        _rx(
            r"<!--|display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:\.0+)?(?:px|pt|em|rem)?\b"
            r"|opacity\s*:\s*0(?:\.0+)?\b|\b(?:hidden|invisible)\s+(?:text|instructions?|prompt|message)\b"
            # Left by ingestion where invisible-character tricks were removed (`app.core.text`). The tricks themselves
            # are found on the raw text by `find_hidden_characters` (see `screen_for_injection`).
            r"|" + re.escape(HIDDEN_TEXT_MARKER)
        ),
    ),
)
_HIDDEN_TEXT = next(rule for rule in RULES if rule.name == "hidden_text")

_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")
_URL_SEPARATOR = re.compile(r"://")
_DIGIT, _UPPER, _LOWER = re.compile(r"[0-9]"), re.compile(r"[A-Z]"), re.compile(r"[a-z]")
_BIDI_OVERRIDE = re.compile("[\u202d\u202e]")
_TAG_CHARACTER = re.compile("[\U000e0000-\U000e007f]")
_WS = re.compile(r"\s+")


def _encoded_payload(text: str) -> re.Match[str] | None:
    """First base64-like run (120+ chars, mixed case and digits) that is not part of a URL. Linear time."""
    urls: list[int] | None = None
    token_start = scanned = 0
    for match in _BASE64_RUN.finditer(text):
        start = match.start()
        # The token around the run starts after the last space/newline. Only the gap since the previous run is
        # searched (runs contain neither), so the scan stays linear however many runs the text has.
        separator = max(text.rfind(" ", scanned, start), text.rfind("\n", scanned, start))
        if separator >= 0:
            token_start = separator + 1
        scanned = match.end()
        if urls is None:
            urls = [m.start() for m in _URL_SEPARATOR.finditer(text)]
        nearest = bisect_left(urls, token_start)
        if nearest < len(urls) and urls[nearest] + 3 <= start:
            continue  # part of a URL (e.g. a signed link), not a payload
        run = match.group(0)
        if _DIGIT.search(run) and _UPPER.search(run) and _LOWER.search(run):
            return match
    return None


def _hidden_characters(text: str) -> InjectionFlag | None:
    match = find_hidden_characters(text)
    if match is None:
        return None
    found = match.group(0)
    if _BIDI_OVERRIDE.search(found):
        kind = "Bidirectional override"
    elif _TAG_CHARACTER.search(found):
        kind = "Unicode tag characters"
    else:
        kind = f"{len(found)} consecutive invisible characters"
    # The excerpt names the trick and shows the visible text around it, never the invisible characters themselves
    # (a bidi override in an excerpt would reorder the reviewer's view). At most ~50 + 2 * _EXCERPT_PAD characters.
    context = strip_default_ignorable(text[max(0, match.start() - _EXCERPT_PAD) : match.end() + _EXCERPT_PAD])
    excerpt = f"{kind} near: {_WS.sub(' ', context).strip()}"
    return InjectionFlag(rule=_HIDDEN_TEXT.name, severity=_HIDDEN_TEXT.severity, excerpt=excerpt)


def _excerpt(text: str, start: int, end: int) -> str:
    snippet = text[max(0, start - _EXCERPT_PAD) : end + _EXCERPT_PAD]
    snippet = _WS.sub(" ", snippet).strip()
    if len(snippet) > MAX_EXCERPT_CHARS:
        snippet = snippet[: MAX_EXCERPT_CHARS - 1] + "…"
    return snippet


def screen_for_injection(text: str) -> list[InjectionFlag]:
    """Return at most one flag per rule for `text` (untrusted email text: subject, body, header fields).

    The rules see the text without default-ignorable characters, so invisible marks between the letters of a
    phrase cannot hide it; `hidden_text` additionally reports the invisible-character tricks found in `text`.
    """
    if not text:
        return []
    view = strip_default_ignorable(text)
    flags: list[InjectionFlag] = []
    for rule in RULES:
        match = rule.search(view)
        if match is not None:
            flags.append(
                InjectionFlag(
                    rule=rule.name, severity=rule.severity, excerpt=_excerpt(view, match.start(), match.end())
                )
            )
        elif rule is _HIDDEN_TEXT and (hidden := _hidden_characters(text)) is not None:
            flags.append(hidden)
    payload = _encoded_payload(view)
    if payload is not None:
        start = payload.start()
        excerpt = _WS.sub(" ", view[start : start + 80]).strip() + "…"
        flags.append(InjectionFlag(rule="encoded_payload", severity="medium", excerpt=excerpt))
    return flags


def merge_flags(*groups: list[InjectionFlag]) -> list[InjectionFlag]:
    """Combine flag lists, keeping the first flag per rule."""
    seen: dict[str, InjectionFlag] = {}
    for group in groups:
        for flag in group:
            seen.setdefault(flag.rule, flag)
    return list(seen.values())
