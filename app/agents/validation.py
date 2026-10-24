"""Deterministic validation of model output: grounding, formats, limits and heuristic confidence.

Bad model content never raises here; it is corrected (dropped, nulled, truncated) and recorded as a
human-readable warning that the reviewer sees.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from pydantic import validate_email

from app.core.timeutil import ensure_utc
from app.domain.enums import ConfidenceBucket, EmailCategory
from app.schemas.ai import (
    ClassificationOutput,
    EvidenceSnippet,
    ExtractionOutput,
    ProposedField,
    ValidatedAnalysis,
    ValidatedExtraction,
)
from app.schemas.email import EmailContext
from app.schemas.settings import ConfidenceThresholds

CONFIDENCE_METHOD = "heuristic-v1"
UNGROUNDED_PENALTY, UNGROUNDED_CAP = 0.15, 0.30
WARNING_PENALTY, WARNING_CAP = 0.05, 0.20
NEEDS_MORE_INFO_PENALTY = 0.10
INJECTION_PENALTY = 0.25

MAX_RATIONALE_CHARS = 600
MAX_LIST_ITEMS = 10
MAX_LIST_ITEM_CHARS = 300
MAX_EVIDENCE = 12
MAX_EVIDENCE_QUOTE_CHARS = 300
MAX_REFERENCES = 20
MAX_URL_CHARS = 2048
MAX_REFERENCE_ID_CHARS = 100
MAX_PROPOSED_FIELDS = 10
MAX_FIELD_VALUE_CHARS = 4000
# Relative deadlines ("by Friday", "end of month") are accepted only within this window after receipt.
RELATIVE_DEADLINE_WINDOW = timedelta(days=120)

_WS_RE = re.compile(r"\s+")
_FIELD_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
_HTTP_URL_RE = re.compile(r"^https?://[^\s<>\"'`]+$", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:(?!\d)")
_TOKEN_RE = re.compile(r"[\w][\w'&-]*")
_QUOTE_CHARS = "\"'`" + "".join(map(chr, (0x201C, 0x201D, 0x2018, 0x2019, 0xAB, 0xBB)))
_ELLIPSIS = ("...", chr(0x2026))
_NAME_STOPWORDS = frozenset(
    {"the", "inc", "ltd", "llc", "gmbh", "corp", "co", "plc", "team", "group", "company", "mr", "mrs", "ms", "dr"}
)

_MONTH_NAME = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_MONTH_DAY_RE = re.compile(_MONTH_NAME + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4})\b)?", re.IGNORECASE)
_DAY_MONTH_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH_NAME + r"(?:,?\s+(\d{4})\b)?", re.IGNORECASE
)
_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4}|\d{2})\b")
_RELATIVE_DATE_RE = re.compile(
    r"\b(?:today|tonight|tomorrow|eod|eow|cob|end\s+of\s+(?:the\s+)?(?:day|week|month|quarter|year|sprint)|"
    r"(?:this|next)\s+(?:week|month|quarter)|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"within\s+\d+\s+(?:hours?|days?|weeks?)|in\s+\d+\s+(?:days?|weeks?))\b",
    re.IGNORECASE,
)


def _month_number(name: str) -> int:
    return ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec").index(
        name[:3].lower()
    ) + 1


def _norm(value: str) -> str:
    return _WS_RE.sub(" ", value).strip().casefold()


@dataclass
class _Text:
    """Searchable views of the untrusted email."""

    content: str  # subject + body, verbatim
    content_norm: str
    people_norm: str  # content + sender + recipients (for requester/organization grounding)

    @classmethod
    def of(cls, email: EmailContext) -> _Text:
        content = f"{email.subject}\n{email.body_text}"
        people = " ".join([content, email.sender_name or "", email.sender_address or "", *email.to, *email.cc])
        return cls(content=content, content_norm=_norm(content), people_norm=_norm(people))


@dataclass
class _Warnings:
    grounding: list[str] = field(default_factory=list)  # ungrounded evidence (penalized separately)
    other: list[str] = field(default_factory=list)

    def add(self, message: str) -> None:
        if message not in self.other:
            self.other.append(message)

    @property
    def all(self) -> list[str]:
        return [*self.grounding, *self.other]


def compute_confidence(
    self_estimate: float, *, ungrounded: int, warnings: int, needs_more_info: bool, injection_detected: bool
) -> float:
    """Heuristic confidence (`heuristic-v1`, not a calibrated probability).

    score = self_estimate
            - 0.15 x ungrounded evidence snippets (max 0.30)
            - 0.05 x extraction/validation warnings (max 0.20)
            - 0.10 if more information is required
            - 0.25 if prompt-injection indicators were detected
    clamped to [0, 1].
    """
    base = self_estimate if math.isfinite(self_estimate) else 0.0
    score = min(1.0, max(0.0, base))
    score -= min(UNGROUNDED_CAP, UNGROUNDED_PENALTY * max(0, ungrounded))
    score -= min(WARNING_CAP, WARNING_PENALTY * max(0, warnings))
    if needs_more_info:
        score -= NEEDS_MORE_INFO_PENALTY
    if injection_detected:
        score -= INJECTION_PENALTY
    return round(min(1.0, max(0.0, score)), 4)


def confidence_bucket(score: float, thresholds: ConfidenceThresholds) -> ConfidenceBucket:
    if score >= thresholds.high:
        return ConfidenceBucket.HIGH
    if score >= thresholds.medium:
        return ConfidenceBucket.MEDIUM
    return ConfidenceBucket.LOW


def _clip(value: str, limit: int, label: str, warnings: _Warnings) -> str:
    if len(value) > limit:
        warnings.add(f"{label} was longer than {limit} characters and was truncated.")
        return value[: limit - 1].rstrip() + chr(0x2026)
    return value


def _clean_list(items: list[str], label: str, warnings: _Warnings) -> list[str]:
    cleaned = [_WS_RE.sub(" ", item).strip() for item in items]
    cleaned = [item for item in cleaned if item]
    if len(cleaned) > MAX_LIST_ITEMS:
        warnings.add(f"Only the first {MAX_LIST_ITEMS} {label} entries were kept.")
        cleaned = cleaned[:MAX_LIST_ITEMS]
    return [_clip(item, MAX_LIST_ITEM_CHARS, f"A {label} entry", warnings) for item in cleaned]


def _validate_classification(
    output: ClassificationOutput, enabled: list[EmailCategory], warnings: _Warnings
) -> ClassificationOutput:
    category = output.category
    if category is not EmailCategory.OTHER and category not in enabled:
        warnings.add(f"Category '{category.value}' is not enabled for this tenant; using 'other'.")
        category = EmailCategory.OTHER
    confidence = output.confidence
    if not math.isfinite(confidence):
        warnings.add("Model confidence was not a number; using 0.")
        confidence = 0.0
    elif not 0.0 <= confidence <= 1.0:
        warnings.add("Model confidence was outside 0..1 and was clamped.")
        confidence = min(1.0, max(0.0, confidence))
    rationale = _WS_RE.sub(" ", output.rationale).strip()
    if not rationale:
        warnings.add("The model did not provide a rationale.")
    rationale = _clip(rationale, MAX_RATIONALE_CHARS, "The rationale", warnings)
    return output.model_copy(
        update={
            "category": category,
            "confidence": confidence,
            "rationale": rationale,
            "missing_information": _clean_list(output.missing_information, "missing-information", warnings),
            "extraction_warnings": _clean_list(output.extraction_warnings, "model warning", warnings),
        }
    )


def _strip_quote(quote: str) -> str:
    quote = quote.strip()
    for _ in range(2):
        for marker in _ELLIPSIS:
            quote = quote.removeprefix(marker).removesuffix(marker).strip()
        quote = quote.strip(_QUOTE_CHARS).strip()
    return quote


def _ground_evidence(evidence: list[EvidenceSnippet], text: _Text, warnings: _Warnings) -> list[EvidenceSnippet]:
    kept: list[EvidenceSnippet] = []
    seen: set[tuple[str, str]] = set()
    for snippet in evidence:
        field_name = _WS_RE.sub(" ", snippet.field).strip()[:64] or "general"
        quote = _strip_quote(snippet.quote)
        if not quote or _norm(quote) not in text.content_norm:
            warnings.grounding.append(f"Dropped evidence for '{field_name}': quote not found verbatim in the email.")
            continue
        key = (field_name, _norm(quote))
        if key in seen:
            continue
        seen.add(key)
        if len(quote) > MAX_EVIDENCE_QUOTE_CHARS:
            quote = quote[:MAX_EVIDENCE_QUOTE_CHARS]  # a prefix of a verbatim quote is still verbatim
        kept.append(EvidenceSnippet(field=field_name, quote=quote))
    if len(kept) > MAX_EVIDENCE:
        warnings.add(f"Only the first {MAX_EVIDENCE} evidence quotes were kept.")
        kept = kept[:MAX_EVIDENCE]
    return kept


def _split_references(references: list[str], text: _Text, warnings: _Warnings) -> tuple[list[str], list[str]]:
    urls: list[str] = []
    ids: list[str] = []
    dropped = 0
    for raw in references:
        ref = raw.strip().strip(_QUOTE_CHARS).strip().rstrip(".,;:)]>")
        if not ref:
            continue
        if _HTTP_URL_RE.match(ref):
            if len(ref) <= MAX_URL_CHARS and ref in text.content:
                if ref not in urls:
                    urls.append(ref)
                continue
        elif not _SCHEME_RE.match(ref) and "\n" not in ref and len(ref) <= MAX_REFERENCE_ID_CHARS:
            pattern = r"(?<![\w])" + re.escape(ref) + r"(?![\w])"
            if re.search(pattern, text.content):
                if ref not in ids:
                    ids.append(ref)
                continue
        dropped += 1
    if dropped:
        warnings.add(f"Dropped {dropped} reference(s) that do not appear verbatim in the email or are not http(s).")
    if len(urls) + len(ids) > MAX_REFERENCES:
        warnings.add(f"Only the first {MAX_REFERENCES} references were kept.")
        urls = urls[:MAX_REFERENCES]
        ids = ids[: max(0, MAX_REFERENCES - len(urls))]
    return urls, ids


def _explicit_dates(content: str) -> list[tuple[int | None, int, int]]:
    """(year or None, month, day) for every explicit date mentioned in the email."""
    found: list[tuple[int | None, int, int]] = []
    for y, m, d in _ISO_DATE_RE.findall(content):
        found.append((int(y), int(m), int(d)))
    for month, d, y in _MONTH_DAY_RE.findall(content):
        found.append((int(y) if y else None, _month_number(month), int(d)))
    for d, month, y in _DAY_MONTH_RE.findall(content):
        found.append((int(y) if y else None, _month_number(month), int(d)))
    for a, b, y in _NUMERIC_DATE_RE.findall(content):
        year = int(y) + 2000 if len(y) == 2 else int(y)
        found.extend([(year, int(a), int(b)), (year, int(b), int(a))])  # both m/d and d/m readings
    return found


def _deadline_supported(deadline: datetime, email: EmailContext, text: _Text) -> bool:
    target: date = deadline.date()
    for year, month, day in _explicit_dates(text.content):
        if month == target.month and day == target.day and year in (None, target.year):
            return True
    if _RELATIVE_DATE_RE.search(text.content):
        received = email.received_at.date()
        return received - timedelta(days=1) <= target <= received + RELATIVE_DEADLINE_WINDOW
    return False


def _validate_deadline(raw: str | None, email: EmailContext, text: _Text, warnings: _Warnings) -> datetime | None:
    if raw is None or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        warnings.add("Deadline was not a valid ISO-8601 date and was removed.")
        return None
    deadline = ensure_utc(parsed)
    if not _deadline_supported(deadline, email, text):
        warnings.add("Deadline was removed because no date in the email supports it.")
        return None
    return deadline


def _grounded_name(value: str, haystack_norm: str) -> bool:
    needle = _norm(value)
    if needle and needle in haystack_norm:
        return True
    tokens = [t.strip("'&-.") for t in _TOKEN_RE.findall(needle)]
    tokens = [t for t in tokens if len(t) >= 2 and t not in _NAME_STOPWORDS]
    return bool(tokens) and all(re.search(r"(?<![\w])" + re.escape(t) + r"(?![\w])", haystack_norm) for t in tokens)


def _optional(value: str | None, limit: int, label: str, warnings: _Warnings) -> str | None:
    if value is None:
        return None
    cleaned = _WS_RE.sub(" ", value).strip()
    return _clip(cleaned, limit, label, warnings) if cleaned else None


def _grounded_optional(
    value: str | None, limit: int, label: str, haystack_norm: str, warnings: _Warnings
) -> str | None:
    cleaned = _optional(value, limit, label, warnings)
    if cleaned is not None and not _grounded_name(cleaned, haystack_norm):
        warnings.add(f"{label} was removed: not stated in email.")
        return None
    return cleaned


def _validate_requester_email(value: str | None, email: EmailContext, text: _Text, warnings: _Warnings) -> str | None:
    if value is None or not value.strip():
        return None
    candidate = value.strip().strip("<>").strip()
    try:
        _, normalized = validate_email(candidate)
    except ValueError:
        warnings.add("Requester email was not a valid address and was removed.")
        return None
    if len(normalized) > 320:
        warnings.add("Requester email was too long and was removed.")
        return None
    if normalized.casefold() not in text.people_norm:
        warnings.add("Requester email was removed: not stated in email.")
        return None
    return normalized


def _validate_fields(fields: list[ProposedField], warnings: _Warnings) -> list[ProposedField]:
    kept: list[ProposedField] = []
    for item in fields:
        name = item.name.strip()
        if not _FIELD_NAME_RE.match(name) or len(name) > 128:
            warnings.add("Dropped a proposed field with an invalid reference name.")
            continue
        if any(f.name == name for f in kept):
            continue
        value = _clip(item.value.strip(), MAX_FIELD_VALUE_CHARS, f"Field {name}", warnings)
        kept.append(ProposedField(name=name, value=value))
    if len(kept) > MAX_PROPOSED_FIELDS:
        warnings.add(f"Only the first {MAX_PROPOSED_FIELDS} proposed fields were kept.")
        kept = kept[:MAX_PROPOSED_FIELDS]
    return kept


def _validate_extraction(
    email: EmailContext, output: ExtractionOutput, text: _Text, warnings: _Warnings
) -> ValidatedExtraction:
    summary = _WS_RE.sub(" ", output.summary).strip()
    if not summary:
        warnings.add("Summary was empty; using the email subject.")
        summary = email.subject.strip() or "(no subject)"
    reference_urls, reference_ids = _split_references(output.references, text, warnings)
    owner = _grounded_optional(output.suggested_owner, 200, "Suggested owner", text.content_norm, warnings)
    if owner is not None and email.injection_flags:
        warnings.add("Suggested owner was removed because the email contains prompt-injection indicators.")
        owner = None
    return ValidatedExtraction(
        source_email_id=email.email_id,
        summary=_clip(summary, 255, "Summary", warnings),
        description=_clip(output.description.strip(), 32_000, "Description", warnings),
        requester_name=_grounded_optional(output.requester_name, 200, "Requester name", text.people_norm, warnings),
        requester_email=_validate_requester_email(output.requester_email, email, text, warnings),
        organization=_grounded_optional(output.organization, 200, "Organization", text.people_norm, warnings),
        product_or_system=_optional(output.product_or_system, 200, "Product or system", warnings),
        environment=_optional(output.environment, 100, "Environment", warnings),
        impact=_optional(output.impact, 2_000, "Impact", warnings),
        deadline=_validate_deadline(output.deadline, email, text, warnings),
        suggested_owner=owner,
        suggested_team=_grounded_optional(output.suggested_team, 200, "Suggested team", text.content_norm, warnings),
        reference_urls=reference_urls,
        reference_ids=reference_ids,
        suggested_work_item_type=_optional(output.suggested_work_item_type, 128, "Work item type", warnings),
        proposed_fields=_validate_fields(output.proposed_fields, warnings),
        evidence=_ground_evidence(output.evidence, text, warnings),
    )


def validate_analysis(
    email: EmailContext,
    classification: ClassificationOutput,
    extraction: ExtractionOutput,
    *,
    thresholds: ConfidenceThresholds,
    enabled_categories: list[EmailCategory],
) -> ValidatedAnalysis:
    """Apply deterministic rules to model output and compute the `heuristic-v1` confidence.

    Rules (each correction adds a warning): disabled category -> other; confidence clamped to [0, 1];
    evidence must be a whitespace-normalized, case-insensitive substring of subject+body (else dropped);
    references kept only if they appear verbatim (http(s) URLs -> reference_urls, others -> reference_ids);
    deadline must be ISO-8601 (naive = UTC) and supported by a date expression in the email; requester
    email must be valid and stated; owner/team/organization/requester name must be stated in the email
    (owner is also dropped when injection indicators exist); proposed field names must be reference names;
    over-long strings are truncated; an empty summary falls back to the subject.

    Confidence: see `compute_confidence`. The warning count is the model's `extraction_warnings` plus the
    validation warnings, excluding ungrounded-evidence warnings (penalized separately).
    """
    warnings = _Warnings()
    text = _Text.of(email)
    validated_classification = _validate_classification(classification, enabled_categories, warnings)
    validated_extraction = _validate_extraction(email, extraction, text, warnings)
    score = compute_confidence(
        validated_classification.confidence,
        ungrounded=len(warnings.grounding),
        warnings=len(validated_classification.extraction_warnings) + len(warnings.other),
        needs_more_info=validated_classification.needs_more_info,
        injection_detected=bool(email.injection_flags),
    )
    return ValidatedAnalysis(
        classification=validated_classification,
        extraction=validated_extraction,
        warnings=warnings.all,
        injection_flags=list(email.injection_flags),
        confidence_score=score,
        confidence_bucket=confidence_bucket(score, thresholds),
        confidence_method=CONFIDENCE_METHOD,
    )
