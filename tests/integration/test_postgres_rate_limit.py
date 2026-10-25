"""Shared rate limits on a real PostgreSQL: separate API instances ("replicas") share one budget, also under load.

Run with: TEST_DATABASE_URL=postgresql+psycopg://… uv run pytest -m postgres tests/integration
"""

from __future__ import annotations

import asyncio
import os
from collections import Counter

import pytest
from sqlalchemy import select

from app.db.models import RateLimitCounter
from app.db.session import Database
from app.services.rate_limit import DatabaseRateLimiter
from tests.integration.test_shared_rate_limit import (
    SIGN_IN_LIMIT,
    SYNC_LIMIT,
    anonymous_sign_in,
    replicas,
    same_browser_on,
    shared_limits,
)
from tests.support.rate_limit import FrozenClock, freeze_database_rate_limits

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
        reason="requires PostgreSQL (set TEST_DATABASE_URL=postgresql+psycopg://...)",
    ),
]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FrozenClock:
    frozen = FrozenClock()  # 10 s into a window
    freeze_database_rate_limits(monkeypatch, frozen)
    return frozen


async def test_two_replicas_share_the_sign_in_limit(database_url: str, db: Database, clock: FrozenClock) -> None:
    async with replicas(shared_limits(database_url, audit_hmac_key="p" * 32)) as (first, second):
        statuses = [(await anonymous_sign_in((first, second)[i % 2])).status_code for i in range(SIGN_IN_LIMIT + 6)]

    assert statuses == [200] * SIGN_IN_LIMIT + [429] * 6


async def test_concurrent_requests_on_three_replicas_never_exceed_the_limit(
    database_url: str, db: Database, clock: FrozenClock
) -> None:
    async with replicas(shared_limits(database_url), count=3) as apps:
        member = await apps[0].persona("member")
        sessions = [member, *(same_browser_on(h, member) for h in apps[1:])]

        responses = await asyncio.gather(
            *(sessions[i % 3].post("/emails/sync", json={}) for i in range(6 * SYNC_LIMIT))
        )

    assert Counter(r.status_code for r in responses) == {202: SYNC_LIMIT, 429: 5 * SYNC_LIMIT}


async def test_hundreds_of_concurrent_acquisitions_on_four_engines_admit_exactly_the_limit(
    db: Database, clock: FrozenClock
) -> None:
    engines = [Database(db.settings) for _ in range(3)]
    try:
        limiters = [DatabaseRateLimiter(d.engine, secret=b"q" * 32, clock=clock) for d in [db, *engines]]
        decisions = await asyncio.gather(*(limiters[i % 4].acquire("load:ip:192.0.2.1", 25, 60) for i in range(200)))
    finally:
        for engine in engines:
            await engine.dispose()

    assert sum(d.allowed for d in decisions) == 25
    assert {d.retry_after for d in decisions if not d.allowed} == {51}
    async with db.session() as s:
        assert (await s.execute(select(RateLimitCounter.hits))).scalars().all() == [25]
