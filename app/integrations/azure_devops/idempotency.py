"""Create-at-most-once algorithm shared by the REST client and the mock adapter.

1. Look the idempotency tag up; found → EXISTING (nothing written). Lookup failure → FAILED (nothing written).
2. Attempt the create:
   * `created`   → CREATED.
   * `retry`     → the server certainly did not apply it (connect error, 429, 503, 408) → retry within bound.
   * `ambiguous` → it may have been applied (read timeout, dropped connection, 500/502/504) → look the tag up:
     found → CREATED (`recovered_after_ambiguous_error=True`), confirmed absent → retry within bound,
     lookup failed → UNCERTAIN (a human reconciles later).
   * `permanent` → FAILED with the category (validation/authorization/permission/not_found); never retried.
3. Retries exhausted → FAILED (only ever reached when the item is confirmed absent).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.errors import IntegrationError
from app.domain.enums import FailureCategory
from app.integrations.azure_devops.base import CreateOutcome, CreateOutcomeStatus, WorkItemRef
from app.integrations.http import RetryPolicy, SleepFn, failure_category_of
from app.observability.logging import get_logger

log = get_logger(__name__)

type AttemptKind = Literal["created", "retry", "ambiguous", "permanent"]


@dataclass(frozen=True, slots=True)
class CreateAttempt:
    """Result of a single create request, as classified by the adapter."""

    kind: AttemptKind
    work_item: WorkItemRef | None = None
    http_status: int | None = None
    category: FailureCategory | None = None
    message: str | None = None
    retry_after: float | None = None
    summary: dict[str, Any] = field(default_factory=dict)


def ref_summary(ref: WorkItemRef, **extra: Any) -> dict[str, Any]:
    return {"id": ref.id, "rev": ref.rev, "url": ref.url, **extra}


async def run_idempotent_create(
    *,
    lookup: Callable[[], Awaitable[WorkItemRef | None]],
    attempt: Callable[[], Awaitable[CreateAttempt]],
    policy: RetryPolicy,
    sleep: SleepFn,
) -> CreateOutcome:
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        existing = await lookup()
    except IntegrationError as exc:
        return CreateOutcome(
            status=CreateOutcomeStatus.FAILED,
            latency_ms=elapsed(),
            http_status=exc.status,
            error_category=failure_category_of(exc),
            error_message=f"Duplicate check failed before create; nothing was created. {exc.message}",
        )
    if existing is not None:
        return CreateOutcome(
            status=CreateOutcomeStatus.EXISTING,
            work_item=existing,
            latency_ms=elapsed(),
            response_summary=ref_summary(existing, found_by="idempotency_tag"),
        )

    retries = 0
    while True:
        try:
            result = await attempt()
        except Exception as exc:  # unexpected adapter failure: the request may already have been sent
            log.warning("ado_create_attempt_error", error_type=type(exc).__name__)
            result = CreateAttempt(
                kind="ambiguous",
                category=FailureCategory.INTERNAL,
                message=f"Unexpected error during the create request ({type(exc).__name__})",
            )
        kind: AttemptKind = result.kind
        if kind == "created" and result.work_item is None:
            kind = "ambiguous"  # "success" without an id: never resend blindly, look it up instead
        if kind == "created" and result.work_item is not None:
            return CreateOutcome(
                status=CreateOutcomeStatus.CREATED,
                work_item=result.work_item,
                retry_count=retries,
                latency_ms=elapsed(),
                http_status=result.http_status,
                response_summary=result.summary or ref_summary(result.work_item),
            )
        if kind == "permanent":
            return CreateOutcome(
                status=CreateOutcomeStatus.FAILED,
                retry_count=retries,
                latency_ms=elapsed(),
                http_status=result.http_status,
                error_category=result.category or FailureCategory.INTERNAL,
                error_message=result.message,
            )
        if kind == "ambiguous":
            try:
                found = await lookup()
            except Exception as exc:  # any lookup failure leaves the outcome unknown
                category = exc.category if isinstance(exc, IntegrationError) else type(exc).__name__
                log.warning("ado_create_uncertain", retry_count=retries, lookup_error=category)
                return CreateOutcome(
                    status=CreateOutcomeStatus.UNCERTAIN,
                    retry_count=retries,
                    latency_ms=elapsed(),
                    http_status=result.http_status,
                    error_category=FailureCategory.AMBIGUOUS,
                    error_message=(
                        f"{result.message or 'The create request outcome is unknown'}; the duplicate check also "
                        "failed. Reconcile before retrying."
                    ),
                )
            if found is not None:
                log.info("ado_create_recovered_after_ambiguous_error", retry_count=retries, work_item_id=found.id)
                return CreateOutcome(
                    status=CreateOutcomeStatus.CREATED,
                    work_item=found,
                    retry_count=retries,
                    latency_ms=elapsed(),
                    http_status=result.http_status,
                    response_summary=ref_summary(found, recovered_after_ambiguous_error=True),
                )
            # Confirmed absent → resending is safe.
        if retries >= policy.max_retries:
            return CreateOutcome(
                status=CreateOutcomeStatus.FAILED,
                retry_count=retries,
                latency_ms=elapsed(),
                http_status=result.http_status,
                error_category=result.category or FailureCategory.TRANSIENT,
                error_message=f"{result.message or 'Create failed'} (gave up after {retries + 1} attempt(s))",
            )
        delay = policy.compute_delay(retries, result.retry_after)
        log.warning(
            "ado_create_retry",
            attempt=retries + 1,
            kind=kind,
            http_status=result.http_status,
            category=result.category,
            delay_s=round(delay, 3),
        )
        retries += 1
        await sleep(delay)
