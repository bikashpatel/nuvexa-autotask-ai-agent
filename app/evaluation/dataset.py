"""Evaluation dataset format (JSON Lines, one email per line)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import ActionType, EmailCategory, Priority

DEFAULT_DATASET = Path(__file__).resolve().parents[2] / "tests" / "evaluation" / "datasets" / "emails_v1.jsonl"
DEFAULT_RECEIVED_AT = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
_NAMESPACE = uuid.UUID("6f1c3a52-5d0e-4a8e-9a43-1f6f0f6a2b10")

ForbiddenOutcome = Literal[
    "critical_priority",  # classification or proposed work item priority is critical
    "owner_set",  # validated extraction kept a suggested owner
    "work_item_proposed",  # an Azure DevOps work item was proposed
    "approval_claim",  # agent-authored text claims something was approved
    "prompt_leak",  # output contains system-prompt text or data-block markers
    "deadline_set",  # validated extraction kept a deadline
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedOutcome(_Strict):
    category: EmailCategory
    priority: list[Priority] = Field(min_length=1, description="Acceptable priorities.")
    needs_more_info: bool
    # Optional checks: evaluated only when the key is present in the record (null means "must be null").
    environment: str | None = None
    product_or_system_contains: str | None = None
    requester_email: str | None = None
    deadline_present: bool
    expected_action_types: list[ActionType]
    injection: bool = False
    forbidden: list[ForbiddenOutcome] = Field(default_factory=list)


Split = Literal["dev", "holdout"]


class EvalEmail(_Strict):
    id: str = Field(min_length=1, max_length=64)
    # "dev": records the heuristic rules were developed against; "holdout": added afterwards, never tuned on.
    split: Split = "dev"
    subject: str
    sender_name: str | None = None
    sender_address: str | None = None
    to: list[str] = Field(default_factory=lambda: ["support@example.com"])
    cc: list[str] = Field(default_factory=list)
    received_at: datetime = DEFAULT_RECEIVED_AT
    body: str
    attachment_names: list[str] = Field(default_factory=list)
    expected: ExpectedOutcome

    @property
    def email_id(self) -> uuid.UUID:
        return uuid.uuid5(_NAMESPACE, self.id)


def load_dataset(path: Path = DEFAULT_DATASET) -> list[EvalEmail]:
    records: list[EvalEmail] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            records.append(EvalEmail.model_validate_json(line))
        except ValueError as exc:
            raise ValueError(f"{path.name}:{number}: invalid record: {exc}") from exc
    ids = [r.id for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path.name}: duplicate record ids")
    return records
