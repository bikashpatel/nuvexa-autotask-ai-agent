"""Tenant-level settings stored in `app_settings.settings` and edited from the dashboard."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import EmailCategory, Priority

DEFAULT_CATEGORY_DESCRIPTIONS: dict[EmailCategory, str] = {
    EmailCategory.INCIDENT: "Production outage, degradation, or security event affecting users now.",
    EmailCategory.BUG: "Defect report: something behaves incorrectly but is not an active outage.",
    EmailCategory.SERVICE_REQUEST: "Request for access, configuration, account or standard service fulfilment.",
    EmailCategory.TASK_REQUEST: "Request to perform a specific piece of work.",
    EmailCategory.FEATURE_REQUEST: "Request for new functionality or an enhancement.",
    EmailCategory.FOLLOW_UP: "Follow-up or status check on an existing item or conversation.",
    EmailCategory.INFORMATION_ONLY: "Announcement, FYI or newsletter; no action requested.",
    EmailCategory.SPAM_OR_IRRELEVANT: "Spam, phishing, marketing or unrelated content.",
    EmailCategory.OTHER: "Anything that does not fit the other categories.",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AzureDevOpsDefaults(StrictModel):
    project: str | None = Field(default=None, max_length=64)
    work_item_type: str = Field(default="Task", max_length=128)
    area_path: str | None = Field(default=None, max_length=256)
    iteration_path: str | None = Field(default=None, max_length=256)
    # Empty list → any project returned by the connection is allowed.
    allowed_projects: list[str] = Field(default_factory=list, max_length=100)
    allowed_work_item_types: list[str] = Field(
        default_factory=lambda: ["Bug", "Task", "User Story", "Issue", "Product Backlog Item", "Epic", "Feature"]
    )
    category_work_item_types: dict[EmailCategory, str] = Field(
        default_factory=lambda: {
            EmailCategory.INCIDENT: "Bug",
            EmailCategory.BUG: "Bug",
            EmailCategory.SERVICE_REQUEST: "Task",
            EmailCategory.TASK_REQUEST: "Task",
            EmailCategory.FEATURE_REQUEST: "User Story",
        }
    )
    # Extra field reference names the planner/reviewer may set besides title/description/priority/tags.
    allowed_additional_fields: list[str] = Field(
        default_factory=lambda: ["Microsoft.VSTS.Common.Severity", "Microsoft.VSTS.TCM.ReproSteps"]
    )


class TaxonomyConfig(StrictModel):
    enabled_categories: list[EmailCategory] = Field(default_factory=lambda: list(EmailCategory))
    category_descriptions: dict[EmailCategory, str] = Field(default_factory=lambda: dict(DEFAULT_CATEGORY_DESCRIPTIONS))

    @field_validator("enabled_categories")
    @classmethod
    def _must_include_other(cls, value: list[EmailCategory]) -> list[EmailCategory]:
        # "other" is the fallback category and cannot be disabled.
        if EmailCategory.OTHER not in value:
            value = [*value, EmailCategory.OTHER]
        return list(dict.fromkeys(value))


class PriorityMapping(StrictModel):
    category_default_priority: dict[EmailCategory, Priority] = Field(
        default_factory=lambda: {
            EmailCategory.INCIDENT: Priority.HIGH,
            EmailCategory.BUG: Priority.MEDIUM,
            EmailCategory.SERVICE_REQUEST: Priority.MEDIUM,
            EmailCategory.TASK_REQUEST: Priority.MEDIUM,
            EmailCategory.FEATURE_REQUEST: Priority.LOW,
            EmailCategory.FOLLOW_UP: Priority.LOW,
            EmailCategory.INFORMATION_ONLY: Priority.LOW,
            EmailCategory.SPAM_OR_IRRELEVANT: Priority.LOW,
            EmailCategory.OTHER: Priority.LOW,
        }
    )
    # Microsoft.VSTS.Common.Priority: 1 (highest) … 4 (lowest)
    azure_devops_priority: dict[Priority, int] = Field(
        default_factory=lambda: {Priority.CRITICAL: 1, Priority.HIGH: 1, Priority.MEDIUM: 2, Priority.LOW: 3}
    )

    @field_validator("azure_devops_priority")
    @classmethod
    def _range(cls, value: dict[Priority, int]) -> dict[Priority, int]:
        if any(v not in (1, 2, 3, 4) for v in value.values()):
            raise ValueError("Azure DevOps priority values must be 1-4")
        missing = set(Priority) - set(value)
        if missing:
            raise ValueError(f"Missing priority mapping for: {sorted(missing)}")
        return value


class ConfidenceThresholds(StrictModel):
    high: float = Field(default=0.75, ge=0, le=1)
    medium: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> ConfidenceThresholds:
        if self.medium > self.high:
            raise ValueError("medium threshold must be <= high threshold")
        return self


class RetentionConfig(StrictModel):
    email_body_days: int = Field(default=90, ge=1, le=3650)
    workflow_event_days: int = Field(default=180, ge=1, le=3650)
    audit_days: int = Field(default=365, ge=30, le=3650)


class TenantSettings(StrictModel):
    azure_devops: AzureDevOpsDefaults = Field(default_factory=AzureDevOpsDefaults)
    taxonomy: TaxonomyConfig = Field(default_factory=TaxonomyConfig)
    priority: PriorityMapping = Field(default_factory=PriorityMapping)
    confidence: ConfidenceThresholds = Field(default_factory=ConfidenceThresholds)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    auto_analyze_new_emails: bool = False
    # Attachment download/parsing is not implemented in the MVP; the flag is reserved and must stay false.
    attachment_processing_enabled: bool = False

    @field_validator("attachment_processing_enabled")
    @classmethod
    def _attachments_disabled(cls, value: bool) -> bool:
        if value:
            raise ValueError("Attachment processing is not available in this version")
        return value
