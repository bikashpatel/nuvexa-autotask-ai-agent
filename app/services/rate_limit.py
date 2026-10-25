"""Rate-limit backends for the `rate_limit()` API dependency (setting RATE_LIMIT_BACKEND).

* `memory` (default): `app.api.middleware.SlidingWindowRateLimiter`, an exact sliding log per API process. With N
  replicas every limit is effectively N times larger (risk R-1).
* `database`: `DatabaseRateLimiter` below. Counters live in the application database (`rate_limit_counters`), so
  every replica that uses the same database enforces one shared budget.

Algorithm (database): a sliding-window counter over two fixed windows. Each bucket has one row per fixed window of
`window_seconds` (window index = floor(unix time / window)). A request `e` seconds into the current window is
admitted while

    current + previous * (1 - e / window) < limit

i.e. the previous window counts with the share of it that still overlaps the sliding window (it assumes those hits
were spread evenly). Compared with the exact sliding log this smooths bursts at a window boundary instead of
allowing a full new budget at once; in the worst case (all previous hits at the very end of their window) a sliding
window of `window` seconds can see up to about twice the limit, where a plain fixed window allows 2x within an
instant. Denied requests are not counted, like the in-process limiter, so a client that keeps hammering does not
extend its own lock-out.

Atomicity: admission is one conditional upsert (`INSERT … ON CONFLICT DO UPDATE SET hits = hits + 1 WHERE hits <
cap RETURNING hits`); PostgreSQL locks the row for it and re-checks the condition against the latest committed
version, SQLite serializes writers. Within one window the limit is therefore exact under any concurrency. The
previous window's count is read just before; requests that cross a window boundary while previous-window requests
are still in flight may read it before those commit, which can admit at most one extra request per such in-flight
request. Window boundaries come from each replica's wall clock (keep replicas NTP-synchronised; skew shifts a
replica's boundaries by the skew).

Every check runs in its own short transaction on its own pooled connection — never the request's session — so a
counter is committed (or not) independently of the request, and no rate-limit row ever joins the caller's
transaction. A request that is denied because the budget is spent reads but does not write.

Privacy: `bucket` is HMAC-SHA256 over the limit name, the identity (`user:<id>` or `ip:<address>`) and the window
length, keyed with a key derived from AUDIT_HMAC_KEY (replicas share it already). Raw IPs and user ids are never
stored, and a digest cannot be reversed by enumerating the IPv4 space without the key. Rows expire two windows after
their window starts; each limiter deletes expired rows at most once per `cleanup_interval_seconds` while it serves
requests.

Failures: database errors raise `RateLimiterUnavailable`; the dependency applies the route's policy (sign-in routes
fail closed, authenticated routes fail open — see app/api/deps.py).
"""

from __future__ import annotations

import hashlib
import hmac
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from sqlalchemy import Table, delete, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.errors import ConfigurationError
from app.db.models import RateLimitCounter
from app.observability.logging import get_logger

log = get_logger(__name__)

_COUNTERS = cast(Table, RateLimitCounter.__table__)
# Development/test fallback only: production requires AUDIT_HMAC_KEY (Settings), from which the real key is derived.
_DEVELOPMENT_KEY = b"nuvexa-development-rate-limit-key-not-for-production"
_KEY_PURPOSE = b"nuvexa/rate-limit-bucket/v1"  # domain separation from the audit-event HMAC
_FLOAT_SLACK = 1e-9  # keeps float noise from rounding a limit boundary up (exactly at the boundary is denied)


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0  # denied: whole seconds after which the same request would be admitted (Retry-After)


class RateLimiterUnavailable(Exception):
    """The shared counter store could not be read or updated. The message is the error type only."""


class RateLimiter(Protocol):
    async def acquire(self, key: str, limit: int, window_seconds: float) -> RateLimitDecision: ...


def retry_after_seconds(wait: float) -> int:
    """Whole seconds after which `wait` has *strictly* passed (both limiters deny exactly at the boundary), >= 1.

    A microsecond of slack absorbs float noise (5.999999999 is treated as 6 → 7)."""
    return max(0, math.floor(wait + 1e-6)) + 1


def _wait_until_admitted(limit: int, previous: int, current: int, elapsed: float, window: float) -> float:
    """Seconds until `current + previous * (1 - e / window) < limit` holds again, assuming no other requests."""
    if current < limit:
        # Room in this window, but the previous one still weighs too much (previous > 0 whenever this is reached):
        # previous * (1 - e / window) < limit - current  ⇔  e > window * (1 - (limit - current) / previous)
        return window * (1 - (limit - current) / previous) - elapsed
    # This window is spent. In the next one its count becomes the weighted previous window, which must decay below
    # the limit: current * (1 - e / window) < limit  ⇔  e > window * (1 - limit / current).
    return window - elapsed + window * (1 - limit / current)


def _utc(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC)


class DatabaseRateLimiter:
    """Rate limits shared by every API replica through the application database (RATE_LIMIT_BACKEND=database)."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        secret: bytes | None = None,
        clock: Callable[[], float] = time.time,
        cleanup_interval_seconds: float = 60.0,
    ) -> None:
        dialect = engine.dialect.name
        if dialect not in ("postgresql", "sqlite"):
            raise ConfigurationError(f"RATE_LIMIT_BACKEND=database supports PostgreSQL and SQLite, not {dialect}.")
        self._engine = engine
        self._insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
        self._key = hmac.new(secret or _DEVELOPMENT_KEY, _KEY_PURPOSE, hashlib.sha256).digest()
        self._clock = clock  # wall clock: every replica must derive the same window index
        self._cleanup_interval = cleanup_interval_seconds
        self._next_cleanup = -math.inf

    def digest(self, key: str, window_seconds: float) -> str:
        """The stored bucket id: keyed, so a client IP or user id cannot be recovered from a row."""
        material = f"{float(window_seconds)!r}\x00{key}".encode()
        return hmac.new(self._key, material, hashlib.sha256).hexdigest()

    async def acquire(self, key: str, limit: int, window_seconds: float) -> RateLimitDecision:
        now = self._clock()
        try:
            decision = await self._count(self.digest(key, window_seconds), limit, window_seconds, now)
        except (SQLAlchemyError, OSError) as exc:
            raise RateLimiterUnavailable(type(exc).__name__) from exc
        if now >= self._next_cleanup:
            self._next_cleanup = now + self._cleanup_interval
            await self._cleanup(now)
        return decision

    async def _count(self, bucket: str, limit: int, window: float, now: float) -> RateLimitDecision:
        index = math.floor(now / window)
        elapsed = now - index * window
        weight = 1.0 - elapsed / window  # share of the previous window still inside the sliding window
        async with self._engine.begin() as conn:
            rows = await conn.execute(
                select(_COUNTERS.c.window_index, _COUNTERS.c.hits).where(
                    _COUNTERS.c.bucket == bucket, _COUNTERS.c.window_index.in_((index - 1, index))
                )
            )
            hits: dict[int, int] = {int(w): int(h) for w, h in rows}
            previous, current = hits.get(index - 1, 0), hits.get(index, 0)
            # Admitted while current + previous * weight < limit, i.e. while current < cap.
            cap = math.ceil(limit - previous * weight - _FLOAT_SLACK)
            if current < cap:
                upsert = (
                    self._insert(_COUNTERS)
                    .values(bucket=bucket, window_index=index, hits=1, expires_at=_utc((index + 2) * window))
                    .on_conflict_do_update(
                        index_elements=[_COUNTERS.c.bucket, _COUNTERS.c.window_index],
                        set_={"hits": _COUNTERS.c.hits + 1},
                        where=_COUNTERS.c.hits < cap,
                    )
                    .returning(_COUNTERS.c.hits)
                )
                if (await conn.execute(upsert)).first() is not None:
                    return RateLimitDecision(allowed=True)
                current = max(current, cap)  # concurrent requests took the rest of the budget meanwhile
        return RateLimitDecision(
            allowed=False,
            retry_after=retry_after_seconds(_wait_until_admitted(limit, previous, current, elapsed, window)),
        )

    async def purge_expired(self, now: float | None = None) -> int:
        """Delete counters whose window can no longer affect any decision; returns the number of rows deleted."""
        cutoff = _utc(self._clock() if now is None else now)
        async with self._engine.begin() as conn:
            result = await conn.execute(delete(_COUNTERS).where(_COUNTERS.c.expires_at <= cutoff))
        return int(result.rowcount or 0)

    async def _cleanup(self, now: float) -> None:
        try:
            await self.purge_expired(now)
        except (SQLAlchemyError, OSError) as exc:  # best effort: the next interval tries again
            log.warning("rate_limit_cleanup_failed", error_type=type(exc).__name__)
