"""Shared rate limiter (RATE_LIMIT_BACKEND=database): a sliding-window counter over two weighted fixed windows.

A request `e` seconds into the current window is admitted when `current + previous * (1 - e / window) < limit`.
Every test runs on the real database (SQLite by default, PostgreSQL with TEST_DATABASE_URL) with a frozen clock.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import OperationalError

from app.core.errors import ConfigurationError
from app.core.timeutil import utcnow
from app.db.models import RateLimitCounter, Tenant
from app.db.session import Database
from app.services import rate_limit as rate_limit_module
from app.services.rate_limit import (
    DatabaseRateLimiter,
    RateLimitDecision,
    RateLimiterUnavailable,
    retry_after_seconds,
)
from tests.conftest import make_settings
from tests.support.rate_limit import WINDOW_START, FrozenClock, RecordingLog

KEY = "auth:ip:203.0.113.9"
SECRET = b"s" * 32


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()  # 10 s into a window


@pytest.fixture
def limiter(db: Database, clock: FrozenClock) -> DatabaseRateLimiter:
    return DatabaseRateLimiter(db.engine, secret=SECRET, clock=clock)


async def admitted(limiter: DatabaseRateLimiter, attempts: int, *, limit: int, key: str = KEY) -> int:
    return sum([(await limiter.acquire(key, limit, 60)).allowed for _ in range(attempts)])


async def rows(db: Database) -> list[tuple[str, int, int]]:
    async with db.session() as s:
        stmt = select(RateLimitCounter.bucket, RateLimitCounter.window_index, RateLimitCounter.hits)
        return [tuple(r) for r in (await s.execute(stmt.order_by(RateLimitCounter.window_index))).all()]


# --------------------------------------------------------------------------- one window


async def test_admits_up_to_the_limit_then_denies_until_the_window_is_over(limiter: DatabaseRateLimiter) -> None:
    decisions = [await limiter.acquire(KEY, 3, 60) for _ in range(4)]

    assert decisions[:3] == [RateLimitDecision(allowed=True)] * 3
    assert decisions[3] == RateLimitDecision(allowed=False, retry_after=51)  # 10 s in → 50 s left, boundary exclusive


async def test_denied_requests_are_not_counted(db: Database, limiter: DatabaseRateLimiter, clock: FrozenClock) -> None:
    assert await admitted(limiter, 7, limit=2) == 2

    [(_bucket, _window, hits)] = await rows(db)
    assert hits == 2
    clock.at(90)  # half-way through the next window: the previous 2 hits weigh 1 → one more fits
    assert await admitted(limiter, 3, limit=2) == 1


# --------------------------------------------------------------------------- previous window, roll-over


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (60, 0),  # boundary: the previous window still weighs fully (10 * 1.0 = 10, not < 10)
        (60.001, 1),
        (66, 1),  # 10 * 0.9 = 9 → exactly one slot; the second needs the estimate strictly below the limit
        (66.001, 2),
        (90, 5),  # half of the previous window still overlaps
        (105, 8),  # 10 * 0.25 = 2.5 → 7.5 left → 8 requests (the 8th sees 7 + 2.5 < 10)
        (119.999, 10),
        (120, 10),  # two windows later the old counts no longer apply at all
    ],
)
async def test_previous_window_weighs_by_its_remaining_overlap(
    limiter: DatabaseRateLimiter, clock: FrozenClock, offset: float, expected: int
) -> None:
    clock.at(30)
    assert await admitted(limiter, 10, limit=10) == 10

    clock.at(offset)
    assert await admitted(limiter, 12, limit=10) == expected


async def test_a_full_window_ending_at_the_boundary_rolls_over_smoothly(
    limiter: DatabaseRateLimiter, clock: FrozenClock
) -> None:
    clock.at(59.999)
    assert await admitted(limiter, 11, limit=10) == 10
    clock.at(60)
    assert await limiter.acquire(KEY, 10, 60) == RateLimitDecision(allowed=False, retry_after=1)
    clock.at(60.001)
    assert await admitted(limiter, 2, limit=10) == 1


# --------------------------------------------------------------------------- Retry-After


async def test_retry_after_is_the_time_until_the_previous_window_has_decayed_enough(
    limiter: DatabaseRateLimiter, clock: FrozenClock
) -> None:
    clock.at(30)
    assert await admitted(limiter, 10, limit=10) == 10
    clock.at(60.5)
    assert (await limiter.acquire(KEY, 10, 60)).allowed
    # 1 + 10 * (1 - e/60) < 10  ⇔  e > 6 → 5.5 s from now; whole seconds, strictly after → 6
    assert await limiter.acquire(KEY, 10, 60) == RateLimitDecision(allowed=False, retry_after=6)

    clock.advance(5)
    assert await limiter.acquire(KEY, 10, 60) == RateLimitDecision(allowed=False, retry_after=1)
    clock.advance(1)
    assert (await limiter.acquire(KEY, 10, 60)).allowed


@pytest.mark.parametrize(("offset", "retry_after"), [(0, 61), (10, 51), (59.5, 1)])
async def test_retry_after_for_a_spent_window_points_strictly_past_the_boundary(
    limiter: DatabaseRateLimiter, clock: FrozenClock, offset: float, retry_after: int
) -> None:
    clock.at(offset)
    assert await admitted(limiter, 3, limit=3) == 3
    denied = await limiter.acquire(KEY, 3, 60)
    assert denied == RateLimitDecision(allowed=False, retry_after=retry_after)

    clock.at(offset + retry_after - 1)
    assert not (await limiter.acquire(KEY, 3, 60)).allowed  # one second earlier is still too early
    clock.at(offset + retry_after)
    assert (await limiter.acquire(KEY, 3, 60)).allowed


async def test_a_bucket_shared_by_a_larger_limit_waits_for_its_weight_to_drop(
    limiter: DatabaseRateLimiter, clock: FrozenClock
) -> None:
    # Two routes may share a limit name with different limits (sign-in: 20, OAuth callback: 30).
    assert await admitted(limiter, 6, limit=6) == 6
    # 6 hits against a limit of 3: the next window must decay to 6 * (1 - e/60) < 3 → e > 30 → 50 + 30 s
    assert await limiter.acquire(KEY, 3, 60) == RateLimitDecision(allowed=False, retry_after=81)


@pytest.mark.parametrize(
    ("wait", "seconds"),
    [(-3.0, 1), (0.0, 1), (0.5, 1), (5.5, 6), (6.0, 7), (5.999_999_999_9, 7), (59.0, 60)],
)
def test_retry_after_seconds_is_whole_and_strictly_after_the_wait(wait: float, seconds: int) -> None:
    assert retry_after_seconds(wait) == seconds


# --------------------------------------------------------------------------- buckets and keys


async def test_buckets_are_separate_per_key_and_window_length(limiter: DatabaseRateLimiter) -> None:
    assert (await limiter.acquire(KEY, 1, 60)).allowed
    assert not (await limiter.acquire(KEY, 1, 60.0)).allowed  # 60 and 60.0 are the same bucket
    assert (await limiter.acquire("auth:ip:203.0.113.10", 1, 60)).allowed
    assert (await limiter.acquire("auth:user:203.0.113.9", 1, 60)).allowed
    assert (await limiter.acquire(KEY, 1, 30)).allowed  # another window length is another bucket


async def test_rows_hold_only_a_keyed_digest_of_the_identity(db: Database, limiter: DatabaseRateLimiter) -> None:
    await limiter.acquire(KEY, 5, 60)

    [(bucket, window_index, hits)] = await rows(db)
    assert re.fullmatch(r"[0-9a-f]{64}", bucket)
    assert "203.0.113.9" not in bucket
    assert bucket == limiter.digest(KEY, 60)
    assert (window_index, hits) == (int(WINDOW_START // 60), 1)
    other_secret = DatabaseRateLimiter(db.engine, secret=b"t" * 32)
    unconfigured = DatabaseRateLimiter(db.engine, secret=None)  # development: a built-in key
    assert len({bucket, other_secret.digest(KEY, 60), unconfigured.digest(KEY, 60)}) == 3
    assert DatabaseRateLimiter(db.engine, secret=SECRET).digest(KEY, 60) == bucket


async def test_limiters_on_separate_engines_share_one_budget(db: Database, clock: FrozenClock) -> None:
    replica = Database(db.settings)
    try:
        first = DatabaseRateLimiter(db.engine, secret=SECRET, clock=clock)
        second = DatabaseRateLimiter(replica.engine, secret=SECRET, clock=clock)
        outcomes = [(await (first, second)[i % 2].acquire(KEY, 5, 60)).allowed for i in range(8)]
        assert outcomes == [True] * 5 + [False] * 3
        # Replicas must share the key material (AUDIT_HMAC_KEY), or each one counts its own buckets.
        assert (await DatabaseRateLimiter(replica.engine, secret=b"x" * 32, clock=clock).acquire(KEY, 5, 60)).allowed
    finally:
        await replica.dispose()


async def test_concurrent_requests_on_several_engines_never_exceed_the_limit(db: Database, clock: FrozenClock) -> None:
    replicas = [Database(db.settings) for _ in range(2)]
    try:
        limiters = [DatabaseRateLimiter(d.engine, secret=SECRET, clock=clock) for d in [db, *replicas]]
        decisions = await asyncio.gather(*(limiters[i % 3].acquire(KEY, 10, 60) for i in range(36)))
    finally:
        for replica in replicas:
            await replica.dispose()

    assert sum(d.allowed for d in decisions) == 10
    [(_bucket, _window, hits)] = await rows(db)
    assert hits == 10


# --------------------------------------------------------------------------- transactions


async def test_the_counter_commits_in_its_own_transaction(db: Database, limiter: DatabaseRateLimiter) -> None:
    async with db.session() as caller:
        await caller.execute(select(func.count()).select_from(Tenant))
        assert caller.in_transaction()

        assert (await limiter.acquire(KEY, 5, 60)).allowed
        await caller.rollback()

    assert [hits for _b, _w, hits in await rows(db)] == [1]  # the caller's rollback does not undo the count


# --------------------------------------------------------------------------- cleanup


async def _expired_row(db: Database, bucket: str, age_windows: int = 3) -> None:
    async with db.session() as s:
        await s.execute(
            insert(RateLimitCounter).values(
                bucket=bucket,
                window_index=int(WINDOW_START // 60) - age_windows,
                hits=1,
                expires_at=utcnow().replace(year=2000),
            )
        )
        await s.commit()


async def test_expired_windows_are_deleted_opportunistically(
    db: Database, limiter: DatabaseRateLimiter, clock: FrozenClock
) -> None:
    await limiter.acquire("a", 5, 60)  # +10: the first request also cleans up (nothing to delete yet)
    await limiter.acquire("b", 5, 60)
    clock.at(70)  # next window: a and b are the previous window, still needed
    await limiter.acquire("c", 5, 60)
    assert len(await rows(db)) == 3

    clock.at(130)  # a and b expired at +120 (end of the window after theirs)
    await limiter.acquire("c", 5, 60)

    assert {(b, w) for b, w, _h in await rows(db)} == {
        (limiter.digest("c", 60), int(WINDOW_START // 60) + 1),
        (limiter.digest("c", 60), int(WINDOW_START // 60) + 2),
    }


async def test_cleanup_runs_at_most_once_per_interval(
    db: Database, limiter: DatabaseRateLimiter, clock: FrozenClock
) -> None:
    await limiter.acquire("a", 5, 60)  # cleanup ran at +10; the next one is due at +70
    await _expired_row(db, "left-over")

    clock.at(69)
    await limiter.acquire("a", 5, 60)
    assert "left-over" in {b for b, _w, _h in await rows(db)}
    clock.at(70)
    await limiter.acquire("a", 5, 60)
    assert "left-over" not in {b for b, _w, _h in await rows(db)}


async def test_purge_expired_reports_the_number_of_deleted_rows(db: Database, limiter: DatabaseRateLimiter) -> None:
    await _expired_row(db, "old-1")
    await _expired_row(db, "old-2", age_windows=5)
    assert await limiter.purge_expired() == 2
    assert await limiter.purge_expired() == 0


async def test_a_failed_cleanup_never_fails_the_request(
    limiter: DatabaseRateLimiter, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_a: Any) -> int:
        raise OperationalError("DELETE FROM rate_limit_counters", {}, Exception("database is locked"))

    monkeypatch.setattr(limiter, "purge_expired", broken)
    log = RecordingLog()
    monkeypatch.setattr(rate_limit_module, "log", log)

    decision = await limiter.acquire(KEY, 5, 60)

    assert decision.allowed
    assert log.events == [
        {"event": "rate_limit_cleanup_failed", "log_level": "warning", "error_type": "OperationalError"}
    ]


# --------------------------------------------------------------------------- failures


async def test_database_errors_raise_rate_limiter_unavailable(db: Database, limiter: DatabaseRateLimiter) -> None:
    async with db.engine.begin() as conn:
        await conn.execute(text("DROP TABLE rate_limit_counters"))

    with pytest.raises(RateLimiterUnavailable) as raised:
        await limiter.acquire(KEY, 5, 60)

    assert str(raised.value) in {"OperationalError", "ProgrammingError"}  # the error type only: no SQL, no key
    assert raised.value.__cause__ is not None


async def test_an_unreachable_database_raises_rate_limiter_unavailable(tmp_path: Any) -> None:
    broken = Database(make_settings(f"sqlite+aiosqlite:///{tmp_path / 'missing' / 'dir' / 'x.db'}"))
    try:
        with pytest.raises(RateLimiterUnavailable):
            await DatabaseRateLimiter(broken.engine).acquire(KEY, 5, 60)
    finally:
        await broken.dispose()


def test_only_postgresql_and_sqlite_are_supported() -> None:
    engine: Any = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    with pytest.raises(ConfigurationError):
        DatabaseRateLimiter(engine)
