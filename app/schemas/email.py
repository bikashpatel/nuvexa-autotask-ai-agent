"""Email representations shared by integrations (normalized provider data) and agents (sanitized context)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.security import canonical_json, sha256_hex


class EmailAddress(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str | None = None
    address: str | None = None


class AttachmentMeta(BaseModel):
    """Attachment metadata only. Content is never downloaded in the MVP."""

    name: str
    content_type: str | None = None
    size: int | None = None
    is_inline: bool = False


class NormalizedEmail(BaseModel):
    """Provider-neutral message produced by the Graph client or the demo mailbox."""

    provider_message_id: str = Field(min_length=1, max_length=512)
    internet_message_id: str | None = None
    conversation_id: str | None = None
    change_key: str | None = None
    subject: str = ""
    sender: EmailAddress | None = None
    to_recipients: list[EmailAddress] = Field(default_factory=list)
    cc_recipients: list[EmailAddress] = Field(default_factory=list)
    received_at: datetime
    sent_at: datetime | None = None
    body_text: str = ""
    body_preview: str = ""
    original_body_content_type: Literal["text", "html"] = "text"
    has_attachments: bool = False
    attachments: list[AttachmentMeta] = Field(default_factory=list)
    importance: str | None = None
    is_read: bool = False
    web_link: str | None = None

    def content_hash(self) -> str:
        """Hash of analysis-relevant content; a change means a previous analysis may be stale."""
        return sha256_hex(
            canonical_json(
                {
                    "subject": self.subject,
                    "sender": self.sender.address if self.sender else None,
                    "body": self.body_text,
                    "attachments": [a.name for a in self.attachments],
                }
            )
        )


class MailChangeSet(BaseModel):
    """Result of one incremental sync round."""

    upserts: list[NormalizedEmail] = Field(default_factory=list)
    removed_provider_ids: list[str] = Field(default_factory=list)
    delta_link: str | None = None  # persist for the next round
    truncated: bool = False  # max_messages reached before the round completed


class InjectionFlag(BaseModel):
    """A deterministic prompt-injection indicator found in untrusted content."""

    rule: str
    severity: Literal["low", "medium", "high"]
    excerpt: str = Field(max_length=200)


class EmailContext(BaseModel):
    """Sanitized, size-bounded view of an email handed to the analysis agent. Treat as untrusted data."""

    email_id: uuid.UUID
    subject: str
    sender_name: str | None
    sender_address: str | None
    to: list[str]
    cc: list[str]
    received_at: datetime
    body_text: str
    truncated: bool
    has_attachments: bool
    attachment_names: list[str]
    injection_flags: list[InjectionFlag] = Field(default_factory=list)
