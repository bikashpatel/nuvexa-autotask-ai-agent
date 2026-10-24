"""Tenant settings validation."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from app.domain.enums import EmailCategory, Priority
from app.schemas.settings import (
    DEFAULT_CATEGORY_DESCRIPTIONS,
    AzureDevOpsDefaults,
    ConfidenceThresholds,
    PriorityMapping,
    RetentionConfig,
    TaxonomyConfig,
    TenantSettings,
)


def test_defaults_are_complete_and_consistent() -> None:
    ts = TenantSettings()
    assert set(DEFAULT_CATEGORY_DESCRIPTIONS) == set(EmailCategory)
    assert ts.taxonomy.enabled_categories == list(EmailCategory)
    assert set(ts.priority.category_default_priority) == set(EmailCategory)
    assert set(ts.priority.azure_devops_priority) == set(Priority)
    assert ts.confidence.medium <= ts.confidence.high
    assert ts.attachment_processing_enabled is False
    assert ts.auto_analyze_new_emails is False
    for wit in ts.azure_devops.category_work_item_types.values():
        assert wit in ts.azure_devops.allowed_work_item_types
    assert ts.azure_devops.work_item_type in ts.azure_devops.allowed_work_item_types


def test_round_trip_through_json() -> None:
    ts = TenantSettings()
    assert TenantSettings.model_validate(ts.model_dump(mode="json")) == ts


def test_extra_keys_forbidden_at_every_level() -> None:
    with pytest.raises(ValidationError):
        TenantSettings.model_validate({"unknown": 1})
    with pytest.raises(ValidationError):
        TenantSettings.model_validate({"azure_devops": {"organization": "evil-org"}})
    with pytest.raises(ValidationError):
        TenantSettings.model_validate({"confidence": {"low": 0.1}})


def test_taxonomy_always_includes_other() -> None:
    t = TaxonomyConfig(enabled_categories=[EmailCategory.BUG, EmailCategory.INCIDENT])
    assert t.enabled_categories == [EmailCategory.BUG, EmailCategory.INCIDENT, EmailCategory.OTHER]
    assert TaxonomyConfig(enabled_categories=[]).enabled_categories == [EmailCategory.OTHER]


def test_taxonomy_deduplicates_preserving_order() -> None:
    t = TaxonomyConfig(
        enabled_categories=[EmailCategory.OTHER, EmailCategory.BUG, EmailCategory.OTHER, EmailCategory.BUG]
    )
    assert t.enabled_categories == [EmailCategory.OTHER, EmailCategory.BUG]


@given(st.lists(st.sampled_from(list(EmailCategory)), max_size=15))
def test_taxonomy_property(categories: list[EmailCategory]) -> None:
    enabled = TaxonomyConfig(enabled_categories=categories).enabled_categories
    assert EmailCategory.OTHER in enabled
    assert len(enabled) == len(set(enabled))
    assert set(enabled) == set(categories) | {EmailCategory.OTHER}


def test_taxonomy_rejects_unknown_category() -> None:
    with pytest.raises(ValidationError):
        TaxonomyConfig.model_validate({"enabled_categories": ["not_a_category"]})


def test_priority_mapping_must_be_complete() -> None:
    with pytest.raises(ValidationError, match="Missing priority mapping") as info:
        PriorityMapping(azure_devops_priority={Priority.CRITICAL: 1, Priority.HIGH: 1})
    assert "low" in str(info.value)
    assert "medium" in str(info.value)


@pytest.mark.parametrize("bad", [0, 5, -1, 10])
def test_priority_mapping_range(bad: int) -> None:
    mapping = {Priority.CRITICAL: 1, Priority.HIGH: 2, Priority.MEDIUM: 3, Priority.LOW: bad}
    with pytest.raises(ValidationError, match="must be 1-4"):
        PriorityMapping(azure_devops_priority=mapping)


@given(st.fixed_dictionaries({p: st.integers(min_value=1, max_value=4) for p in Priority}))
def test_priority_mapping_accepts_any_complete_valid_mapping(mapping: dict[Priority, int]) -> None:
    assert PriorityMapping(azure_devops_priority=mapping).azure_devops_priority == mapping


@pytest.mark.parametrize(("high", "medium"), [(0.5, 0.5), (0.9, 0.1), (1.0, 0.0), (0.0, 0.0)])
def test_thresholds_ordered(high: float, medium: float) -> None:
    t = ConfidenceThresholds(high=high, medium=medium)
    assert (t.high, t.medium) == (high, medium)


@pytest.mark.parametrize(("high", "medium"), [(0.4, 0.5), (0.0, 0.01)])
def test_thresholds_out_of_order(high: float, medium: float) -> None:
    with pytest.raises(ValidationError, match="medium threshold must be <= high threshold"):
        ConfidenceThresholds(high=high, medium=medium)


@pytest.mark.parametrize("value", [-0.1, 1.1])
def test_thresholds_bounds(value: float) -> None:
    with pytest.raises(ValidationError):
        ConfidenceThresholds(high=value)


def test_retention_bounds() -> None:
    with pytest.raises(ValidationError):
        RetentionConfig(audit_days=29)  # audit trail is kept at least 30 days
    with pytest.raises(ValidationError):
        RetentionConfig(email_body_days=0)
    assert RetentionConfig(email_body_days=1, workflow_event_days=3650).email_body_days == 1


def test_attachment_processing_cannot_be_enabled() -> None:
    with pytest.raises(ValidationError, match="Attachment processing is not available"):
        TenantSettings(attachment_processing_enabled=True)
    assert TenantSettings(attachment_processing_enabled=False).attachment_processing_enabled is False


def test_azure_devops_defaults_limits() -> None:
    with pytest.raises(ValidationError):
        AzureDevOpsDefaults(project="p" * 65)
    with pytest.raises(ValidationError):
        AzureDevOpsDefaults(allowed_projects=[f"p{i}" for i in range(101)])
    d = AzureDevOpsDefaults(allowed_projects=["A"])
    assert d.allowed_projects == ["A"]
    assert "Microsoft.VSTS.Common.Severity" in d.allowed_additional_fields
