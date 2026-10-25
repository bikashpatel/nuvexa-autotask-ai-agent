"""Build the sanitized, size-bounded `EmailContext` handed to analysis agents."""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from app.agents.injection import merge_flags, screen_for_injection
from app.core.text import normalize_text, truncate
from app.core.timeutil import ensure_utc
from app.schemas.email import EmailContext

MAX_SUBJECT_CHARS = 998
MAX_NAME_CHARS = 200
MAX_ADDRESS_CHARS = 320
MAX_RECIPIENTS = 50
MAX_ATTACHMENT_NAMES = 20
MAX_ATTACHMENT_NAME_CHARS = 120
# Upper bound on text screened for injection (bodies are capped far below this before reaching a model).
SCREEN_LIMIT_CHARS = 200_000

_WS_RE = re.compile(r"\s+")


def _line(value: str | None, limit: int) -> str:
    return _WS_RE.sub(" ", normalize_text(value or "")).strip()[:limit]


def _optional_line(value: str | None, limit: int) -> str | None:
    return _line(value, limit) or None


def build_email_context(
    *,
    email_id: uuid.UUID,
    subject: str,
    sender_name: str | None,
    sender_address: str | None,
    to: list[str],
    cc: list[str],
    received_at: datetime,
    body_text: str,
    has_attachments: bool,
    attachment_names: list[str],
    max_chars: int,
) -> EmailContext:
    """Normalize and bound untrusted fields, then screen every field that reaches the prompt for injection.

    Subject and body are screened over the full normalized text (before truncation) and over the raw text, so
    invisible-character tricks removed by normalization are still reported. The header fields that reach the
    model (sender name and address, recipients, attachment names) are screened the same way, as a separate text
    so that words in one header cannot serve as the "AI context" of a phrase in the body.
    """
    clean_subject = _line(subject, MAX_SUBJECT_CHARS)
    clean_body = normalize_text(body_text or "")
    body, truncated = truncate(clean_body, max_chars)
    clean_sender_name = _optional_line(sender_name, MAX_NAME_CHARS)
    clean_sender_address = _optional_line(sender_address, MAX_ADDRESS_CHARS)
    clean_to = [a for a in (_line(r, MAX_ADDRESS_CHARS) for r in to[:MAX_RECIPIENTS]) if a]
    clean_cc = [a for a in (_line(r, MAX_ADDRESS_CHARS) for r in cc[:MAX_RECIPIENTS]) if a]
    names = [n for n in (_line(a, MAX_ATTACHMENT_NAME_CHARS) for a in attachment_names) if n][:MAX_ATTACHMENT_NAMES]
    raw_headers = [sender_name or "", sender_address or "", *to[:MAX_RECIPIENTS], *cc[:MAX_RECIPIENTS]]
    raw_headers += attachment_names[:MAX_ATTACHMENT_NAMES]
    clean_headers = [clean_sender_name or "", clean_sender_address or "", *clean_to, *clean_cc, *names]
    flags = merge_flags(
        screen_for_injection(f"{clean_subject}\n{clean_body}"[:SCREEN_LIMIT_CHARS]),
        screen_for_injection(f"{subject or ''}\n{body_text or ''}"[:SCREEN_LIMIT_CHARS]),
        screen_for_injection("\n".join(clean_headers)[:SCREEN_LIMIT_CHARS]),
        screen_for_injection("\n".join(raw_headers)[:SCREEN_LIMIT_CHARS]),
    )
    return EmailContext(
        email_id=email_id,
        subject=clean_subject,
        sender_name=clean_sender_name,
        sender_address=clean_sender_address,
        to=clean_to,
        cc=clean_cc,
        received_at=ensure_utc(received_at),
        body_text=body,
        truncated=truncated,
        has_attachments=has_attachments or bool(names),
        attachment_names=names,
        injection_flags=flags,
    )
