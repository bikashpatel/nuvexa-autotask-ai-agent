"""Shared HTTP retry/backoff helpers for integration adapters.

Rules enforced here:

* Bounded exponential backoff with full jitter; `Retry-After` (seconds or HTTP date) is honoured, capped.
* Transport errors are split into *certainly not sent* (connect/pool failures → always safe to retry) and
  *ambiguous* (the server may have processed the request → never blindly retry a non-idempotent write).
* Nothing in this module logs headers, tokens, URLs or bodies — only operation names, statuses and timings.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any

import httpx

from app.core.errors import IntegrationError
from app.domain.enums import FailureCategory
from app.observability.logging import get_logger

log = get_logger(__name__)

type SleepFn = Callable[[float], Awaitable[None]]

# Statuses worth retrying for idempotent requests (GET, read-only POST such as WIQL).
RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
# For non-idempotent writes: the server refused the request before processing it → safe to resend.
WRITE_SAFE_RETRY_STATUSES: frozenset[int] = frozenset({408, 429, 503})
# For non-idempotent writes: the server (or a gateway in front of it) may have applied the write.
WRITE_AMBIGUOUS_STATUSES: frozenset[int] = frozenset({500, 502, 504})

# The request never left the client: no connection was established / no pooled connection was available.
CERTAINLY_NOT_SENT_ERRORS: tuple[type[httpx.TransportError], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)

_rng = random.SystemRandom()


class TransportOutcome(StrEnum):
    NOT_SENT = "not_sent"  # safe to retry any request
    AMBIGUOUS = "ambiguous"  # may have been processed (read/write timeout, dropped connection, protocol error)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded retries with exponential backoff and full jitter."""

    max_retries: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    # Upper bound for server-provided Retry-After values (protects workers from very long sleeps).
    max_retry_after: float = 30.0

    def compute_delay(self, retry_index: int, retry_after: float | None = None) -> float:
        """Delay before retry number `retry_index + 1` (0-based index of the retry being scheduled)."""
        if retry_after is not None:
            return max(0.0, min(retry_after, self.max_retry_after))
        cap = min(self.max_delay, self.base_delay * (2 ** max(0, retry_index)))
        return _rng.uniform(0.0, cap)


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a `Retry-After` header (delay-seconds or HTTP-date) into seconds; None if absent/invalid."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        return seconds if seconds >= 0 else None
    try:
        when = parsedate_to_datetime(value)
    except TypeError, ValueError, IndexError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def classify_response(status: int) -> tuple[FailureCategory, bool]:
    """Map an HTTP error status to `(failure category, retryable)`."""
    if status == 401:
        return FailureCategory.AUTHORIZATION, False
    if status == 403:
        return FailureCategory.PERMISSION, False
    if status == 404:
        return FailureCategory.NOT_FOUND, False
    if status == 408:
        return FailureCategory.TIMEOUT, True
    if status == 429:
        return FailureCategory.RATE_LIMITED, True
    if status >= 500:
        return FailureCategory.TRANSIENT, True
    if 400 <= status < 500:
        return FailureCategory.VALIDATION, False
    if 300 <= status < 400:
        return FailureCategory.CONFIGURATION, False
    return FailureCategory.INTERNAL, False


def classify_transport_error(exc: httpx.TransportError) -> TransportOutcome:
    return TransportOutcome.NOT_SENT if isinstance(exc, CERTAINLY_NOT_SENT_ERRORS) else TransportOutcome.AMBIGUOUS


def transport_failure_category(exc: httpx.TransportError) -> FailureCategory:
    return FailureCategory.TIMEOUT if isinstance(exc, httpx.TimeoutException) else FailureCategory.TRANSIENT


def failure_category_of(error: IntegrationError) -> FailureCategory:
    """Best-effort conversion of an `IntegrationError.category` string to the domain enum."""
    try:
        return FailureCategory(error.category)
    except ValueError:
        return FailureCategory.TRANSIENT if error.retryable else FailureCategory.INTERNAL


async def request_idempotent(
    client: httpx.AsyncClient,
    method: str,
    url: str | httpx.URL,
    *,
    policy: RetryPolicy,
    sleep: SleepFn = asyncio.sleep,
    headers: Mapping[str, str] | None = None,
    json: Any = None,
    content: bytes | None = None,
    timeout_seconds: float | None = None,
    retry_statuses: frozenset[int] = RETRYABLE_STATUSES,
    operation: str = "http_request",
) -> httpx.Response:
    """Send an *idempotent* request, retrying transport errors and `retry_statuses` within `policy`.

    Returns the final response (possibly an error status once retries are exhausted). Raises
    `IntegrationError` (category `timeout`/`transient`, retryable) when the transport keeps failing.
    Never use this for non-idempotent writes.
    """
    kwargs: dict[str, Any] = {"headers": headers, "json": json, "content": content}
    if timeout_seconds is not None:
        kwargs["timeout"] = timeout_seconds
    retry_index = 0
    while True:
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.UnsupportedProtocol as exc:
            raise IntegrationError(
                f"{operation}: unsupported URL scheme", category=FailureCategory.CONFIGURATION
            ) from exc
        except httpx.TransportError as exc:
            if retry_index >= policy.max_retries:
                raise IntegrationError(
                    f"{operation} failed after {retry_index + 1} attempt(s): {type(exc).__name__}",
                    category=transport_failure_category(exc),
                    retryable=True,
                ) from exc
            delay = policy.compute_delay(retry_index)
            log.warning(
                "integration_http_retry",
                operation=operation,
                attempt=retry_index + 1,
                error_type=type(exc).__name__,
                delay_s=round(delay, 3),
            )
            retry_index += 1
            await sleep(delay)
            continue
        if response.status_code in retry_statuses and retry_index < policy.max_retries:
            delay = policy.compute_delay(retry_index, parse_retry_after(response.headers.get("Retry-After")))
            log.warning(
                "integration_http_retry",
                operation=operation,
                attempt=retry_index + 1,
                http_status=response.status_code,
                delay_s=round(delay, 3),
            )
            await response.aclose()
            retry_index += 1
            await sleep(delay)
            continue
        return response


async def no_sleep(_seconds: float) -> None:
    """Sleep replacement for tests and the in-process mock adapter."""
    return None
