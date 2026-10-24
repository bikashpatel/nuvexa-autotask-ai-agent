"""Correlation-id context, UTC helpers and the error hierarchy."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core import errors
from app.core.context import get_correlation_id, new_correlation_id, sanitize_correlation_id, set_correlation_id
from app.core.timeutil import ensure_utc, utcnow

SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


@pytest.mark.parametrize("value", ["abcdefgh", "req-123:abc.def_X", "a" * 128, "0f" * 16])
def test_sanitize_accepts_safe_ids(value: str) -> None:
    assert sanitize_correlation_id(value) == value


@pytest.mark.parametrize(
    "value", [None, "", "short", "a" * 129, "has space123", "new\nline-abc", 'quote"abcdef', "ünïcode-abc", "a/b/c/d/e"]
)
def test_sanitize_replaces_unsafe_ids(value: str | None) -> None:
    out = sanitize_correlation_id(value)
    assert out != value
    assert re.fullmatch(r"[0-9a-f]{32}", out)


@given(st.text(max_size=150))
def test_sanitize_always_returns_loggable_id(value: str) -> None:
    out = sanitize_correlation_id(value)
    assert SAFE_ID.match(out)
    assert out == value or re.fullmatch(r"[0-9a-f]{32}", out)


def test_new_correlation_ids_are_unique() -> None:
    ids = {new_correlation_id() for _ in range(50)}
    assert len(ids) == 50


def test_correlation_id_context_roundtrip() -> None:
    set_correlation_id("ctx-123456")
    try:
        assert get_correlation_id() == "ctx-123456"
    finally:
        set_correlation_id(None)
    assert get_correlation_id() is None


async def test_correlation_id_is_task_local() -> None:
    async def worker(cid: str) -> str | None:
        set_correlation_id(cid)
        await asyncio.sleep(0)
        return get_correlation_id()

    results = await asyncio.gather(worker("task-aaaaaaaa"), worker("task-bbbbbbbb"))
    assert results == ["task-aaaaaaaa", "task-bbbbbbbb"]
    assert get_correlation_id() is None


def test_utcnow_is_aware_utc() -> None:
    now = utcnow()
    assert now.tzinfo is UTC
    assert abs((datetime.now(UTC) - now).total_seconds()) < 5


def test_ensure_utc() -> None:
    naive = datetime(2026, 1, 2, 3, 4, 5)
    assert ensure_utc(naive) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    plus2 = datetime(2026, 1, 2, 5, 4, 5, tzinfo=timezone(timedelta(hours=2)))
    converted = ensure_utc(plus2)
    assert converted.tzinfo is UTC
    assert converted.hour == 3
    already = datetime(2026, 1, 2, tzinfo=UTC)
    assert ensure_utc(already) == already


ERROR_TABLE = [
    (errors.AppError, 500, "internal_error"),
    (errors.ConfigurationError, 500, "configuration_error"),
    (errors.AuthenticationRequired, 401, "authentication_required"),
    (errors.PermissionDenied, 403, "permission_denied"),
    (errors.CsrfFailed, 403, "csrf_failed"),
    (errors.NotFound, 404, "not_found"),
    (errors.Conflict, 409, "conflict"),
    (errors.InvalidStateTransition, 409, "invalid_state_transition"),
    (errors.StaleVersion, 409, "stale_version"),
    (errors.ValidationFailed, 422, "validation_failed"),
    (errors.RateLimited, 429, "rate_limited"),
    (errors.IntegrationError, 502, "integration_error"),
    (errors.IntegrationNotConfigured, 409, "integration_not_configured"),
]


@pytest.mark.parametrize(("cls", "status", "code"), ERROR_TABLE, ids=lambda v: getattr(v, "__name__", str(v)))
def test_error_classes(cls: type[errors.AppError], status: int, code: str) -> None:
    exc = cls()
    assert isinstance(exc, errors.AppError)
    assert exc.status_code == status
    assert exc.code == code
    assert exc.message == cls.public_message
    assert str(exc) == cls.public_message
    assert exc.details == []


def test_error_custom_message_and_details() -> None:
    exc = errors.Conflict("custom", details=[{"msg": "x"}])
    assert exc.message == "custom"
    assert exc.details == [{"msg": "x"}]
    assert exc.public_message != "custom"


def test_integration_error_fields() -> None:
    exc = errors.IntegrationError("boom", category="timeout", retryable=True, status=504, details=[{"a": 1}])
    assert (exc.category, exc.retryable, exc.status, exc.details) == ("timeout", True, 504, [{"a": 1}])
    default = errors.IntegrationError()
    assert (default.category, default.retryable, default.status) == ("unknown", False, None)
    nc = errors.IntegrationNotConfigured("connect first")
    assert (nc.message, nc.category, nc.retryable) == ("connect first", "not_configured", False)
