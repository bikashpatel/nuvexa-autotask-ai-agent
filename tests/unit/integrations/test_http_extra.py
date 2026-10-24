"""Retry/backoff helpers: Retry-After parsing, status classes, transport outcomes and retry loops."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.errors import IntegrationError
from app.domain.enums import FailureCategory
from app.integrations.http import (
    CERTAINLY_NOT_SENT_ERRORS,
    RETRYABLE_STATUSES,
    WRITE_AMBIGUOUS_STATUSES,
    WRITE_SAFE_RETRY_STATUSES,
    RetryPolicy,
    classify_response,
    classify_transport_error,
    failure_category_of,
    no_sleep,
    parse_retry_after,
    request_idempotent,
    transport_failure_category,
)

URL = "https://api.example.com/items"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", 0.0),
        ("120", 120.0),
        ("1e1", 10.0),
        ("-0", 0.0),
        ("inf", float("inf")),  # capped later by RetryPolicy.max_retry_after
        ("nan", None),
        ("-5", None),
        ("", None),
        ("   ", None),
        ("Wed, 01 Jan 2026 12:00:45 GMT", 45.0),
        ("Thu, 01 Jan 2026 12:00:30 -0000", 30.0),  # "-0000" yields a naive datetime -> assumed UTC
        ("Wed, 01 Jan 2026 14:00:30 +0200", 30.0),
        ("Tue, 31 Dec 2025 12:00:00 GMT", 0.0),  # in the past
        ("Wed, 32 Jan 2026 12:00:00 GMT", None),  # invalid day
        ("tomorrow-ish", None),
        ("12:00", None),
    ],
)
def test_parse_retry_after_variants(value: str, expected: float | None) -> None:
    assert parse_retry_after(value, now=NOW) == expected


def test_parse_retry_after_uses_the_current_time_by_default() -> None:
    future = (datetime.now(UTC) + timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    delay = parse_retry_after(future)
    assert delay is not None
    assert 3500 < delay <= 3600


@settings(max_examples=100, deadline=None)
@given(st.text(max_size=40))
def test_parse_retry_after_never_raises_and_is_non_negative(value: str) -> None:
    result = parse_retry_after(value, now=NOW)
    assert result is None or result >= 0


@settings(max_examples=100, deadline=None)
@given(
    retry_index=st.integers(min_value=-3, max_value=50),
    retry_after=st.one_of(st.none(), st.floats(min_value=-10, max_value=1e6), st.just(float("inf"))),
)
def test_compute_delay_is_bounded(retry_index: int, retry_after: float | None) -> None:
    policy = RetryPolicy(max_retries=3, base_delay=0.5, max_delay=8.0, max_retry_after=30.0)
    delay = policy.compute_delay(retry_index, retry_after)
    if retry_after is None:
        assert 0.0 <= delay <= min(8.0, 0.5 * 2 ** max(0, retry_index))
    else:
        assert delay == max(0.0, min(retry_after, 30.0))


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (100, (FailureCategory.INTERNAL, False)),
        (200, (FailureCategory.INTERNAL, False)),
        (204, (FailureCategory.INTERNAL, False)),
        (301, (FailureCategory.CONFIGURATION, False)),
        (302, (FailureCategory.CONFIGURATION, False)),
        (399, (FailureCategory.CONFIGURATION, False)),
        (400, (FailureCategory.VALIDATION, False)),
        (401, (FailureCategory.AUTHORIZATION, False)),
        (403, (FailureCategory.PERMISSION, False)),
        (404, (FailureCategory.NOT_FOUND, False)),
        (405, (FailureCategory.VALIDATION, False)),
        (408, (FailureCategory.TIMEOUT, True)),
        (409, (FailureCategory.VALIDATION, False)),
        (410, (FailureCategory.VALIDATION, False)),
        (422, (FailureCategory.VALIDATION, False)),
        (429, (FailureCategory.RATE_LIMITED, True)),
        (499, (FailureCategory.VALIDATION, False)),
        (500, (FailureCategory.TRANSIENT, True)),
        (502, (FailureCategory.TRANSIENT, True)),
        (504, (FailureCategory.TRANSIENT, True)),
        (599, (FailureCategory.TRANSIENT, True)),
    ],
)
def test_classify_response_every_status_class(status: int, expected: tuple[FailureCategory, bool]) -> None:
    assert classify_response(status) == expected


def test_write_status_sets_partition_the_retryable_statuses() -> None:
    assert WRITE_SAFE_RETRY_STATUSES | WRITE_AMBIGUOUS_STATUSES == RETRYABLE_STATUSES
    assert not WRITE_SAFE_RETRY_STATUSES & WRITE_AMBIGUOUS_STATUSES
    assert all(classify_response(s)[1] for s in RETRYABLE_STATUSES)


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (httpx.ConnectTimeout("x"), FailureCategory.TIMEOUT),
        (httpx.ReadTimeout("x"), FailureCategory.TIMEOUT),
        (httpx.WriteTimeout("x"), FailureCategory.TIMEOUT),
        (httpx.PoolTimeout("x"), FailureCategory.TIMEOUT),
        (httpx.ConnectError("x"), FailureCategory.TRANSIENT),
        (httpx.ReadError("x"), FailureCategory.TRANSIENT),
        (httpx.RemoteProtocolError("x"), FailureCategory.TRANSIENT),
        (httpx.ProxyError("x"), FailureCategory.TRANSIENT),
    ],
)
def test_transport_failure_category(error: httpx.TransportError, category: FailureCategory) -> None:
    assert transport_failure_category(error) is category
    assert classify_transport_error(error).value == (
        "not_sent" if isinstance(error, CERTAINLY_NOT_SENT_ERRORS) else "ambiguous"
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (IntegrationError("x", category=FailureCategory.PERMISSION), FailureCategory.PERMISSION),
        (IntegrationError("x", category="delta_expired"), FailureCategory.DELTA_EXPIRED),
        (IntegrationError("x", category="weird", retryable=True), FailureCategory.TRANSIENT),
        (IntegrationError("x", category="weird"), FailureCategory.INTERNAL),
    ],
)
def test_failure_category_of(error: IntegrationError, expected: FailureCategory) -> None:
    assert failure_category_of(error) is expected


async def test_unsupported_scheme_is_a_configuration_error(http_client, sleep) -> None:
    with pytest.raises(IntegrationError) as info:
        await request_idempotent(http_client, "GET", "ftp://api.example.com/x", policy=RetryPolicy(), sleep=sleep)
    assert info.value.category == FailureCategory.CONFIGURATION
    assert info.value.retryable is False
    assert sleep.calls == []


@respx.mock
async def test_request_passes_headers_body_and_timeout(http_client, sleep) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(201, json={"ok": True}))
    response = await request_idempotent(
        http_client,
        "POST",
        URL,
        policy=RetryPolicy(max_retries=0),
        sleep=sleep,
        headers={"X-Test": "1"},
        json={"q": "select"},
        timeout_seconds=3.0,
        operation="test.op",
    )
    assert response.status_code == 201
    request = route.calls.last.request
    assert request.headers["X-Test"] == "1"
    assert request.content == b'{"q":"select"}'
    assert request.extensions["timeout"]["read"] == 3.0


@respx.mock
async def test_retry_statuses_are_configurable_and_exhaustion_returns_the_last_response(http_client, sleep) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(500))
    policy = RetryPolicy(max_retries=2)
    response = await request_idempotent(http_client, "GET", URL, policy=policy, sleep=sleep)
    assert response.status_code == 500
    assert route.call_count == 3
    assert len(sleep.calls) == 2
    assert all(0 <= d <= policy.max_delay for d in sleep.calls)

    route.reset()
    response = await request_idempotent(
        http_client, "GET", URL, policy=policy, sleep=sleep, retry_statuses=frozenset({503})
    )
    assert response.status_code == 500
    assert route.call_count == 1  # 500 not in the custom retry set


@respx.mock
async def test_retry_after_header_drives_the_delay(http_client, sleep) -> None:
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "900"}),  # capped at max_retry_after
            httpx.Response(503, headers={"Retry-After": "garbage"}),  # invalid -> jittered backoff
            httpx.Response(200),
        ]
    )
    await request_idempotent(http_client, "GET", URL, policy=RetryPolicy(max_retries=3), sleep=sleep)
    assert sleep.calls[0] == 30.0
    assert 0.0 <= sleep.calls[1] <= 1.0


@respx.mock
async def test_connect_errors_exhausting_retries_are_transient(http_client, sleep) -> None:
    route = respx.get(URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(IntegrationError) as info:
        await request_idempotent(
            http_client, "GET", URL, policy=RetryPolicy(max_retries=1), sleep=sleep, operation="op"
        )
    assert info.value.category == FailureCategory.TRANSIENT
    assert info.value.retryable is True
    assert "op failed after 2 attempt(s): ConnectError" in info.value.message
    assert route.call_count == 2


async def test_no_sleep_returns_immediately() -> None:
    assert await no_sleep(100.0) is None
