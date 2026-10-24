"""Executable action payloads. An approval is bound to the SHA-256 hash of exactly one of these."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, TypeAdapter, field_validator

from app.domain.enums import ActionType

_TAG_RE = re.compile(r"^[\w .\-:/]{1,64}$")
_FIELD_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
_PATH_RE = re.compile(r"^[^<>|\"*?:\x00-\x1f]{1,256}$")


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceEmailReference(_Payload):
    email_id: uuid.UUID
    internet_message_id: str | None = Field(default=None, max_length=998)
    subject: str = Field(default="", max_length=998)
    sender: str | None = Field(default=None, max_length=320)
    received_at: datetime | None = None


class AzureDevOpsWorkItemPayload(_Payload):
    kind: Literal["create_azure_devops_work_item"] = "create_azure_devops_work_item"
    project: str = Field(min_length=1, max_length=64)
    work_item_type: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=32_000)
    priority: int = Field(ge=1, le=4, description="Microsoft.VSTS.Common.Priority (1 = highest)")
    area_path: str | None = Field(default=None, max_length=256)
    iteration_path: str | None = Field(default=None, max_length=256)
    tags: list[str] = Field(default_factory=list, max_length=10)
    assigned_to: EmailStr | None = None
    additional_fields: dict[str, str | int | float] = Field(default_factory=dict, max_length=10)
    source_email: SourceEmailReference

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str]) -> list[str]:
        cleaned = []
        for raw in value:
            tag = raw.strip()
            if not tag:
                continue
            if ";" in tag or not _TAG_RE.match(tag):
                raise ValueError(f"Invalid tag: {tag!r}")
            cleaned.append(tag)
        return list(dict.fromkeys(cleaned))

    @field_validator("additional_fields")
    @classmethod
    def _field_refs(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key, val in value.items():
            if not _FIELD_REF_RE.match(key):
                raise ValueError(f"Invalid field reference name: {key!r}")
            if isinstance(val, str) and len(val) > 4_000:
                raise ValueError(f"Field {key} value is too long")
        return value

    @field_validator("area_path", "iteration_path")
    @classmethod
    def _paths(cls, value: str | None) -> str | None:
        if value is not None and not _PATH_RE.match(value):
            raise ValueError("Invalid classification path")
        return value


class EmailReplyDraftPayload(_Payload):
    kind: Literal["propose_email_reply"] = "propose_email_reply"
    to: list[EmailStr] = Field(min_length=1, max_length=1)
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=20_000)
    in_reply_to_internet_message_id: str | None = Field(default=None, max_length=998)
    source_email: SourceEmailReference


class FollowUpTaskPayload(_Payload):
    kind: Literal["create_follow_up_task"] = "create_follow_up_task"
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=10_000)
    due_at: datetime | None = None
    source_email: SourceEmailReference


class NoActionPayload(_Payload):
    kind: Literal["mark_no_action"] = "mark_no_action"
    reason: str = Field(min_length=1, max_length=1_000)
    source_email: SourceEmailReference


ActionPayload = Annotated[
    AzureDevOpsWorkItemPayload | EmailReplyDraftPayload | FollowUpTaskPayload | NoActionPayload,
    Field(discriminator="kind"),
]

ACTION_PAYLOAD_ADAPTER: TypeAdapter[
    AzureDevOpsWorkItemPayload | EmailReplyDraftPayload | FollowUpTaskPayload | NoActionPayload
] = TypeAdapter(ActionPayload)

PAYLOAD_KIND_BY_ACTION: dict[ActionType, str] = {
    ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM: "create_azure_devops_work_item",
    ActionType.PROPOSE_EMAIL_REPLY: "propose_email_reply",
    ActionType.CREATE_FOLLOW_UP_TASK: "create_follow_up_task",
    ActionType.MARK_NO_ACTION: "mark_no_action",
}

REQUIRED_PERMISSIONS: dict[ActionType, list[str]] = {
    ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM: ["azure_devops:vso.work_write", "nuvexa:action.execute"],
    ActionType.PROPOSE_EMAIL_REPLY: ["nuvexa:reply_draft.create"],
    ActionType.CREATE_FOLLOW_UP_TASK: ["nuvexa:task.create"],
    ActionType.MARK_NO_ACTION: ["nuvexa:email.update"],
}


def parse_payload(
    data: dict[str, Any],
) -> AzureDevOpsWorkItemPayload | EmailReplyDraftPayload | FollowUpTaskPayload | NoActionPayload:
    return ACTION_PAYLOAD_ADAPTER.validate_python(data)
