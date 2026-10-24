from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx

from app.core.errors import IntegrationError
from app.domain.enums import FailureCategory
from app.integrations.http import (
    RetryPolicy,
    TransportOutcome,
    classify_response,
    classify_transport_error,
    parse_retry_after,
    request_idempotent,
)

URL = "https://api.example.com/items"


def test_backoff_is_full_jitter_within_exponential_cap():
    policy = RetryPolicy(max_retries=5, base_delay=0.5, max_delay=8.0)
    for retry_index, cap in [(0, 0.5), (1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (10, 8.0)]:
        delays = [policy.compute_delay(retry_index) for _ in range(200)]
        assert all(0.0 <= d <= cap for d in delays)
    assert policy.compute_delay(0, retry_after=3.0) == 3.0
    assert policy.compute_delay(0, retry_after=900.0) == policy.max_retry_after


def test_parse_retry_after_seconds_and_http_date():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after(" 2.5 ") == 2.5
    assert parse_retry_after(format_datetime(now + timedelta(seconds=30), usegmt=True), now=now) == 30.0
    assert parse_retry_after(format_datetime(now - timedelta(seconds=30), usegmt=True), now=now) == 0.0
    assert parse_retry_after("soon") is None
    assert parse_retry_after("-1") is None
    assert parse_retry_after(None) is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, (FailureCategory.VALIDATION, False)),
        (401, (FailureCategory.AUTHORIZATION, False)),
        (403, (FailureCategory.PERMISSION, False)),
        (404, (FailureCategory.NOT_FOUND, False)),
        (409, (FailureCategory.VALIDATION, False)),
        (408, (FailureCategory.TIMEOUT, True)),
        (429, (FailureCategory.RATE_LIMITED, True)),
        (500, (FailureCategory.TRANSIENT, True)),
        (503, (FailureCategory.TRANSIENT, True)),
    ],
)
def test_classify_response(status, expected):
    assert classify_response(status) == expected


def test_transport_errors_split_into_not_sent_and_ambiguous():
    request = httpx.Request("POST", URL)
    not_sent: list[httpx.TransportError] = [
        httpx.ConnectError("x", request=request),
        httpx.ConnectTimeout("x"),
        httpx.PoolTimeout("x"),
    ]
    ambiguous: list[httpx.TransportError] = [
        httpx.ReadTimeout("x"),
        httpx.WriteTimeout("x"),
        httpx.RemoteProtocolError("x"),
        httpx.ReadError("x"),
    ]
    assert all(classify_transport_error(e) is TransportOutcome.NOT_SENT for e in not_sent)
    assert all(classify_transport_error(e) is TransportOutcome.AMBIGUOUS for e in ambiguous)


@respx.mock
async def test_request_idempotent_retries_statuses_and_transport_errors(http_client, sleep):
    route = respx.get(URL).mock(
        side_effect=[
            httpx.ConnectError("down"),
            httpx.Response(503, headers={"Retry-After": "4"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    response = await request_idempotent(http_client, "GET", URL, policy=RetryPolicy(max_retries=3), sleep=sleep)
    assert response.status_code == 200
    assert route.call_count == 3
    assert sleep.calls[1] == 4.0


@respx.mock
async def test_request_idempotent_gives_up_with_integration_error(http_client, sleep):
    respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(IntegrationError) as info:
        await request_idempotent(http_client, "GET", URL, policy=RetryPolicy(max_retries=2), sleep=sleep)
    assert info.value.category == FailureCategory.TIMEOUT
    assert info.value.retryable is True
    assert len(sleep.calls) == 2


@respx.mock
async def test_request_idempotent_returns_last_error_response_without_retrying_4xx(http_client, sleep):
    route = respx.get(URL).mock(return_value=httpx.Response(400))
    response = await request_idempotent(http_client, "GET", URL, policy=RetryPolicy(max_retries=3), sleep=sleep)
    assert response.status_code == 400
    assert route.call_count == 1
    assert sleep.calls == []
