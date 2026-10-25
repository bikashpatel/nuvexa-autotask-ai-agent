"""RATE_LIMIT_BACKEND=database through the API: limits hold across API replicas sharing one database (risk R-1).

Each "replica" is a separate application instance with its own engine and connection pool, as separate API
processes would be. Runs on SQLite by default and on PostgreSQL with TEST_DATABASE_URL; the heavier PostgreSQL-only
scenarios are in test_postgres_rate_limit.py.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.db.session import Database
from tests.conftest import make_settings
from tests.integration.conftest import API, Harness, Persona, assert_error, build_harness
from tests.support.rate_limit import FrozenClock, freeze_database_rate_limits

SIGN_IN_LIMIT = 20  # POST /auth/demo-login
SYNC_LIMIT = 10  # POST /emails/sync


@asynccontextmanager
async def replicas(settings: Settings, count: int = 2) -> AsyncIterator[list[Harness]]:
    """`count` API instances, each with its own engine and pool, sharing the test database."""
    async with AsyncExitStack() as stack:
        yield [await stack.enter_async_context(build_harness(settings, Database(settings))) for _ in range(count)]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FrozenClock:
    frozen = FrozenClock()  # 10 s into a window: no test straddles a window boundary by accident
    freeze_database_rate_limits(monkeypatch, frozen)
    return frozen


def shared_limits(database_url: str, **overrides: Any) -> Settings:
    return make_settings(database_url, rate_limit_enabled=True, rate_limit_backend="database", **overrides)


async def anonymous_sign_in(h: Harness) -> httpx.Response:
    h.client.cookies.clear()  # anonymous → limited per client IP
    return await h.client.post(f"{API}/auth/demo-login", json={"persona": "viewer"})


def same_browser_on(h: Harness, persona: Persona) -> Persona:
    """The persona's session (cookies and CSRF header) sent to another replica."""
    client = h.tracked_client()
    client.cookies.update(persona.client.cookies)
    return Persona(persona.name, client, persona.headers, persona.user_id, persona.tenant_id)


async def test_the_sign_in_limit_holds_across_replicas(database_url: str, db: Database, clock: FrozenClock) -> None:
    async with replicas(shared_limits(database_url, audit_hmac_key="r" * 32)) as (first, second):
        statuses = [(await anonymous_sign_in((first, second)[i % 2])).status_code for i in range(SIGN_IN_LIMIT + 4)]
        limited = await anonymous_sign_in(second)

    assert statuses == [200] * SIGN_IN_LIMIT + [429] * 4
    assert_error(limited, 429, "rate_limited")
    assert limited.headers["retry-after"] == "51"


async def test_the_in_process_backend_counts_per_replica(database_url: str, db: Database) -> None:
    # The default backend, for contrast: every replica has its own budget, so N replicas allow N x the limit.
    settings = make_settings(database_url, rate_limit_enabled=True)
    async with replicas(settings) as (first, second):
        statuses = [(await anonymous_sign_in((first, second)[i % 2])).status_code for i in range(2 * SIGN_IN_LIMIT + 2)]

    assert Counter(statuses) == {200: 2 * SIGN_IN_LIMIT, 429: 2}


async def test_a_users_budget_is_shared_across_replicas(database_url: str, db: Database, clock: FrozenClock) -> None:
    async with replicas(shared_limits(database_url)) as (first, second):
        member = await first.persona("member")
        sessions = (member, same_browser_on(second, member))

        statuses = [(await sessions[i % 2].post("/emails/sync", json={})).status_code for i in range(SYNC_LIMIT + 2)]
        approver = await second.persona("approver")
        other_user = await approver.post("/emails/sync", json={})

    assert statuses == [202] * SYNC_LIMIT + [429] * 2
    assert other_user.status_code == 202


async def test_concurrent_requests_across_replicas_never_exceed_the_limit(
    database_url: str, db: Database, clock: FrozenClock
) -> None:
    async with replicas(shared_limits(database_url)) as (first, second):
        member = await first.persona("member")
        sessions = (member, same_browser_on(second, member))

        responses = await asyncio.gather(
            *(sessions[i % 2].post("/emails/sync", json={}) for i in range(3 * SYNC_LIMIT))
        )

    # Exact within a window: the conditional upsert admits at most `limit` requests whatever the interleaving.
    assert Counter(r.status_code for r in responses) == {202: SYNC_LIMIT, 429: 2 * SYNC_LIMIT}
