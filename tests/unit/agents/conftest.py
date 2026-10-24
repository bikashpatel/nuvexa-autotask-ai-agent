from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from app.agents.sanitize import build_email_context
from app.domain.enums import ActionType
from app.schemas.ai import PlanningContext
from app.schemas.email import EmailContext
from app.schemas.settings import AzureDevOpsDefaults

RECEIVED_AT = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
SPEC_BODY = (
    "Hi team,\n\nProduction users are unable to log in after today's deployment. Please investigate this "
    "immediately. The issue affects our customer portal.\n\nThanks,\nDana"
)

CtxFactory = Callable[..., EmailContext]


@pytest.fixture
def make_ctx() -> CtxFactory:
    def _make(body: str, subject: str = "Test email", **overrides: Any) -> EmailContext:
        values: dict[str, Any] = {
            "email_id": uuid.uuid4(),
            "subject": subject,
            "sender_name": "Dana Reyes",
            "sender_address": "dana.reyes@example.com",
            "to": ["support@example.org"],
            "cc": [],
            "received_at": RECEIVED_AT,
            "body_text": body,
            "has_attachments": False,
            "attachment_names": [],
            "max_chars": 12_000,
        }
        values.update(overrides)
        return build_email_context(**values)

    return _make


@pytest.fixture
def planning_context() -> Callable[..., PlanningContext]:
    def _make(**overrides: Any) -> PlanningContext:
        defaults = AzureDevOpsDefaults()
        values: dict[str, Any] = {
            "allowed_action_types": list(ActionType),
            "allowed_work_item_types": defaults.allowed_work_item_types,
            "default_work_item_type": "Task",
            "category_work_item_type": None,
            "azure_devops_available": True,
        }
        values.update(overrides)
        return PlanningContext(**values)

    return _make
