"""Microsoft Graph `message` JSON → `NormalizedEmail`. Input is untrusted; every field is optional except `id`."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, TypeGuard
from urllib.parse import urlsplit

from app.core.text import html_to_text, normalize_text
from app.core.timeutil import ensure_utc, utcnow
from app.schemas.email import AttachmentMeta, EmailAddress, NormalizedEmail

# Fields requested from Graph (`$select`). `id` is always returned.
GRAPH_MESSAGE_SELECT: tuple[str, ...] = (
    "subject",
    "from",
    "sender",
    "toRecipients",
    "ccRecipients",
    "receivedDateTime",
    "sentDateTime",
    "body",
    "bodyPreview",
    "hasAttachments",
    "importance",
    "isRead",
    "webLink",
    "internetMessageId",
    "conversationId",
    "changeKey",
)

_MAX_ID = 512
_MAX_INTERNET_ID = 998
_MAX_NAME = 320
_MAX_PREVIEW = 500
_MAX_RECIPIENTS = 100
_MAX_ATTACHMENTS = 50
_MAX_WEB_LINK = 4096
_IMPORTANCE = frozenset({"low", "normal", "high"})


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _bool(value: Any) -> bool:
    # Graph occasionally serializes booleans as strings in delta responses ("isRead": "true").
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value[:limit] if value else None


def _has_control_chars(value: str) -> bool:
    """Control, format, surrogate, private-use or unassigned code points (Unicode category C)."""
    return any(unicodedata.category(ch)[0] == "C" for ch in value)


def _token(value: Any, limit: int) -> str | None:
    """An identifier-like field (ids, addresses, MIME types): no control characters (PostgreSQL rejects NUL),
    whitespace runs collapsed, clipped to its column size."""
    if not isinstance(value, str):
        return None
    cleaned = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in value)
    return _clip(" ".join(cleaned.split()), limit)


def usable_message_id(value: object) -> TypeGuard[str]:
    """A provider message id is stored verbatim: it must be non-empty, bounded and free of control characters."""
    return isinstance(value, str) and 0 < len(value) <= _MAX_ID and not _has_control_chars(value)


def parse_graph_datetime(value: Any) -> datetime | None:
    """Parse Graph ISO-8601 timestamps (e.g. `2024-05-01T10:00:00Z`) to aware UTC; None when invalid."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _address(recipient: Any) -> EmailAddress | None:
    if not isinstance(recipient, Mapping):
        return None
    inner = recipient.get("emailAddress")
    if not isinstance(inner, Mapping):
        return None
    name = _clip(normalize_text(inner["name"]) if isinstance(inner.get("name"), str) else None, _MAX_NAME)
    address = _token(inner.get("address"), _MAX_NAME)
    if name is None and address is None:
        return None
    return EmailAddress(name=name, address=address)


def _recipients(value: Any) -> list[EmailAddress]:
    if not isinstance(value, list):
        return []
    return [addr for item in value[:_MAX_RECIPIENTS] if (addr := _address(item)) is not None]


def _attachments(value: Any) -> list[AttachmentMeta]:
    """Metadata only (name/type/size/inline). `contentBytes` is never read."""
    if not isinstance(value, list):
        return []
    result: list[AttachmentMeta] = []
    for item in value[:_MAX_ATTACHMENTS]:
        if not isinstance(item, Mapping):
            continue
        name = _clip(normalize_text(item["name"]) if isinstance(item.get("name"), str) else None, 255)
        size = item.get("size")
        result.append(
            AttachmentMeta(
                name=name or "(unnamed attachment)",
                content_type=_token(item.get("contentType"), 255),
                size=size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
                is_inline=_bool(item.get("isInline")),
            )
        )
    return result


def _body(value: Any) -> tuple[str, Literal["text", "html"]]:
    # Invisible-character tricks (bidi overrides, tag characters, runs of invisibles) are replaced by a visible
    # marker rather than dropped silently: the stored text is clean, and the analysis still reports them.
    if not isinstance(value, Mapping):
        return "", "text"
    content = _str(value.get("content")) or ""
    if str(value.get("contentType", "")).lower() == "html":
        return html_to_text(content, mark_hidden=True), "html"
    return normalize_text(content, mark_hidden=True), "text"


def _web_link(value: Any) -> str | None:
    link = _str(value)
    # A link with control characters or whitespace (NUL, smuggled CR/LF) or an absurd length is dropped whole.
    if not link or len(link) > _MAX_WEB_LINK or _has_control_chars(link) or any(ch.isspace() for ch in link):
        return None
    parts = urlsplit(link)
    # Only plain https links are kept; anything else (javascript:, data:, http:) is dropped.
    return link if parts.scheme == "https" and parts.netloc else None


def normalize_graph_message(
    item: Mapping[str, Any], *, fallback_received_at: datetime | None = None
) -> NormalizedEmail:
    """Convert one Graph message resource to `NormalizedEmail`.

    Raises `ValueError` only when the message has no usable `id`. Missing timestamps fall back to
    `sentDateTime`, then `fallback_received_at` (default: now).
    """
    message_id = item.get("id")
    if not usable_message_id(message_id):
        raise ValueError("Graph message has no usable id")

    sent_at = parse_graph_datetime(item.get("sentDateTime"))
    received_at = parse_graph_datetime(item.get("receivedDateTime")) or sent_at or fallback_received_at or utcnow()
    body_text, content_type = _body(item.get("body"))
    preview_raw = _str(item.get("bodyPreview"))
    preview = normalize_text(preview_raw) if preview_raw is not None else body_text
    subject_raw = _str(item.get("subject")) or ""
    subject = " ".join(normalize_text(subject_raw, mark_hidden=True).split())
    sender = _address(item.get("from")) or _address(item.get("sender"))
    importance = _str(item.get("importance"))
    attachments = _attachments(item.get("attachments"))

    return NormalizedEmail(
        provider_message_id=message_id,
        internet_message_id=_token(item.get("internetMessageId"), _MAX_INTERNET_ID),
        conversation_id=_token(item.get("conversationId"), _MAX_ID),
        change_key=_token(item.get("changeKey"), _MAX_ID),
        subject=subject,
        sender=sender,
        to_recipients=_recipients(item.get("toRecipients")),
        cc_recipients=_recipients(item.get("ccRecipients")),
        received_at=received_at,
        sent_at=sent_at,
        body_text=body_text,
        body_preview=preview[:_MAX_PREVIEW],
        original_body_content_type=content_type,
        has_attachments=_bool(item.get("hasAttachments")) or bool(attachments),
        attachments=attachments,
        importance=importance.lower() if importance and importance.lower() in _IMPORTANCE else None,
        is_read=_bool(item.get("isRead")),
        web_link=_web_link(item.get("webLink")),
    )
