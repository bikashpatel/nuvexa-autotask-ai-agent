"""Schemas for AI-generated data.

Two layers:

* **LLM-facing models** (`ClassificationOutput`, `ExtractionOutput`, `Propose*Args`) are sent to the
  provider as strict JSON schemas. They use only types, enums and nullability (no free-form dicts,
  no length/format keywords) so they work with OpenAI and Azure OpenAI strict mode. Field
  descriptions instruct the model.
* **Validated models** (`ValidatedExtraction`, `ValidatedAnalysis`) are produced by deterministic
  code that enforces lengths, URL/date formats, evidence grounding and confidence rules, and that
  records warnings instead of silently accepting malformed output.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import ActionType, ConfidenceBucket, EmailCategory, Priority, Urgency
from app.schemas.email import InjectionFlag

AI_SCHEMA_VERSION = "2026-10-24.1"


class _LLMModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------------------------


class ClassificationOutput(_LLMModel):
    category: EmailCategory = Field(description="Best matching category from the configured taxonomy.")
    rationale: str = Field(
        description="One or two short sentences a reviewer can read explaining the category. "
        "Reference facts in the email. No step-by-step reasoning."
    )
    urgency: Urgency = Field(description="How quickly the sender needs a response, based on the email only.")
    priority: Priority = Field(description="Business priority implied by impact and urgency.")
    action_recommended: bool = Field(description="True if some follow-up work should be created.")
    needs_more_info: bool = Field(description="True if essential information is missing to act.")
    missing_information: list[str] = Field(
        description="Short descriptions of missing facts (e.g. 'affected environment'). Empty if none."
    )
    extraction_warnings: list[str] = Field(
        description="Concerns about the email (ambiguity, conflicting statements, suspicious "
        "instructions). Empty if none."
    )
    confidence: float = Field(
        description="Your own estimate from 0.0 to 1.0 that the category is correct. This is an uncalibrated estimate."
    )


# ---------------------------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------------------------


class EvidenceSnippet(_LLMModel):
    field: str = Field(description="Name of the extracted field this quote supports, e.g. 'impact'.")
    quote: str = Field(description="An exact, verbatim quote from the email body or subject (max ~200 chars).")


class ProposedField(_LLMModel):
    name: str = Field(description="Azure DevOps field reference name, e.g. 'Microsoft.VSTS.Common.Severity'.")
    value: str = Field(description="Value for the field as a string.")


class ExtractionOutput(_LLMModel):
    summary: str = Field(description="Imperative one-line summary suitable as a work item title (<= 120 chars).")
    description: str = Field(description="Factual description of the request using only information in the email.")
    requester_name: str | None = Field(description="Name of the person asking, if stated. Null if unknown.")
    requester_email: str | None = Field(description="Email address of the requester, if stated. Null if unknown.")
    organization: str | None = Field(description="Customer or organization named in the email. Null if unknown.")
    product_or_system: str | None = Field(description="Product, service or system affected. Null if unknown.")
    environment: str | None = Field(description="Environment (production, staging, …) if stated. Null if unknown.")
    impact: str | None = Field(description="Who/what is affected and how, if stated. Null if unknown.")
    deadline: str | None = Field(
        description="Deadline exactly as an ISO-8601 date or date-time ONLY if the email states one. "
        "Never infer or invent. Null otherwise."
    )
    suggested_owner: str | None = Field(
        description="A person explicitly named as owner/assignee in the email. Never guess. Null otherwise."
    )
    suggested_team: str | None = Field(description="Team explicitly mentioned in the email. Null otherwise.")
    references: list[str] = Field(description="URLs or ticket identifiers that appear verbatim in the email.")
    suggested_work_item_type: str | None = Field(
        description="Suggested work item type such as 'Bug' or 'Task'. Null if no work item is appropriate."
    )
    proposed_fields: list[ProposedField] = Field(
        description="Optional additional work item fields. Use only when clearly supported by the email."
    )
    evidence: list[EvidenceSnippet] = Field(
        description="Verbatim quotes supporting the category, impact, urgency and any deadline."
    )


# ---------------------------------------------------------------------------------------------
# Planning — registered proposal tools (function calling). Calling a tool only PROPOSES an action.
# ---------------------------------------------------------------------------------------------


class ProposeAzureDevOpsWorkItemArgs(_LLMModel):
    """Propose creating an Azure DevOps work item for human review. This does not create anything."""

    title: str = Field(description="Work item title, imperative, <= 120 characters.")
    description: str = Field(description="Work item description built only from facts in the email.")
    work_item_type: str = Field(description="One of the allowed work item types listed in the instructions.")
    priority: Priority
    tags: list[str] = Field(description="Up to 5 short tags (letters, digits, dashes).")
    rationale: str = Field(description="One sentence explaining why this action is proposed.")
    expected_impact: str = Field(description="One sentence describing the expected outcome.")


class ProposeEmailReplyArgs(_LLMModel):
    """Propose a reply draft to the original sender for human review. The reply is never sent automatically."""

    subject: str
    body: str = Field(description="Polite, concise plain-text reply. Make no commitments on dates or fixes.")
    rationale: str
    expected_impact: str


class ProposeFollowUpTaskArgs(_LLMModel):
    """Propose an internal follow-up task in the NUVEXA task list for human review."""

    title: str
    description: str
    due_date: str | None = Field(description="ISO-8601 date only if the email states a deadline. Else null.")
    rationale: str
    expected_impact: str


class ProposeNoActionArgs(_LLMModel):
    """Propose marking the email as requiring no action, for human confirmation."""

    reason: str
    rationale: str
    expected_impact: str


PLANNING_TOOLS: dict[str, tuple[ActionType, type[_LLMModel]]] = {
    "propose_azure_devops_work_item": (ActionType.CREATE_AZURE_DEVOPS_WORK_ITEM, ProposeAzureDevOpsWorkItemArgs),
    "propose_email_reply": (ActionType.PROPOSE_EMAIL_REPLY, ProposeEmailReplyArgs),
    "propose_follow_up_task": (ActionType.CREATE_FOLLOW_UP_TASK, ProposeFollowUpTaskArgs),
    "propose_no_action": (ActionType.MARK_NO_ACTION, ProposeNoActionArgs),
}


class PlannedToolCall(BaseModel):
    """A validated proposal-tool call returned by the planner (arguments already schema-validated)."""

    tool_name: Literal[
        "propose_azure_devops_work_item", "propose_email_reply", "propose_follow_up_task", "propose_no_action"
    ]
    action_type: ActionType
    arguments: dict[str, Any]


class PlanningContext(BaseModel):
    """Deterministic constraints given to the planner (from tenant settings, never from the email)."""

    allowed_action_types: list[ActionType]
    allowed_work_item_types: list[str]
    default_work_item_type: str
    category_work_item_type: str | None = None
    azure_devops_available: bool = True
    already_completed_actions: list[str] = Field(
        default_factory=list, description='Actions already executed for this email, formatted "<action_type>: <title>".'
    )


# ---------------------------------------------------------------------------------------------
# Validated (deterministic) results
# ---------------------------------------------------------------------------------------------


class ValidatedExtraction(BaseModel):
    source_email_id: uuid.UUID
    summary: str = Field(max_length=255)
    description: str = Field(max_length=32_000)
    requester_name: str | None = Field(default=None, max_length=200)
    requester_email: str | None = Field(default=None, max_length=320)
    organization: str | None = Field(default=None, max_length=200)
    product_or_system: str | None = Field(default=None, max_length=200)
    environment: str | None = Field(default=None, max_length=100)
    impact: str | None = Field(default=None, max_length=2_000)
    deadline: datetime | None = None
    suggested_owner: str | None = Field(default=None, max_length=200)
    suggested_team: str | None = Field(default=None, max_length=200)
    reference_urls: list[str] = Field(default_factory=list)
    reference_ids: list[str] = Field(default_factory=list)
    suggested_work_item_type: str | None = Field(default=None, max_length=128)
    proposed_fields: list[ProposedField] = Field(default_factory=list)
    evidence: list[EvidenceSnippet] = Field(default_factory=list)


class ValidatedAnalysis(BaseModel):
    classification: ClassificationOutput
    extraction: ValidatedExtraction
    warnings: list[str] = Field(default_factory=list)
    injection_flags: list[InjectionFlag] = Field(default_factory=list)
    confidence_score: float = Field(ge=0, le=1)
    confidence_bucket: ConfidenceBucket
    confidence_method: str = "heuristic-v1"
