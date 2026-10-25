"""Rate limits cannot be reset by the client: anonymous requests are keyed by IP, authenticated ones by user.

Every test runs with both backends: in-process (default) and the shared database store (RATE_LIMIT_BACKEND=database).
"""

from __future__ import annotations

import re
import secrets

import pytest
from sqlalchemy import select, text

from app.core.config import Settings
from app.db.models import RateLimitCounter
from tests.conftest import make_settings
from tests.security.conftest import Harness
from tests.support.rate_limit import FrozenClock, freeze_database_rate_limits

API = "/api/v1"
SHARED_STORE = pytest.mark.parametrize("settings", ["database"], indirect=True)


@pytest.fixture(params=["memory", "database"])
def settings(request: pytest.FixtureRequest, database_url: str, monkeypatch: pytest.MonkeyPatch) -> Settings:
    # Overrides the shared fixture: limits ON for this module, once per backend (database: frozen clock).
    freeze_database_rate_limits(monkeypatch, FrozenClock())
    return make_settings(database_url, rate_limit_enabled=True, rate_limit_backend=request.param)


async def test_random_session_cookies_do_not_reset_the_login_limit(harness: Harness) -> None:
    # Arrange: an attacker sends a fresh, made-up session cookie with every login attempt
    statuses: list[int] = []

    # Act
    for _ in range(30):
        harness.client.cookies.clear()
        harness.client.cookies.set(harness.settings.session_cookie_name, secrets.token_urlsafe(32))
        resp = await harness.client.post(f"{API}/auth/demo-login", json={"persona": "viewer"})
        statuses.append(resp.status_code)

    # Assert
    assert 429 in statuses, f"login limit never applied: {statuses}"
    assert statuses.index(429) <= 20


async def test_a_user_with_several_sessions_shares_one_budget(harness: Harness) -> None:
    # Arrange: the same user signs in from two browser sessions
    first_headers = await harness.login("member")
    async with harness.new_client() as second:
        await second.post(f"{API}/auth/demo-login", json={"persona": "member"})
        second_headers = {"X-CSRF-Token": second.cookies.get(harness.settings.csrf_cookie_name) or ""}

        # Act: alternate mailbox syncs (limit 10/min) between the two sessions
        statuses = []
        for i in range(14):
            client, headers = (harness.client, first_headers) if i % 2 == 0 else (second, second_headers)
            statuses.append((await client.post(f"{API}/emails/sync", json={}, headers=headers)).status_code)

    # Assert: the per-user budget is exhausted although each session alone stayed under the limit
    assert 429 in statuses, statuses


async def test_different_users_have_independent_budgets(harness: Harness) -> None:
    # Arrange: exhaust the member's sync budget
    member_headers = await harness.login("member")
    for _ in range(12):
        await harness.client.post(f"{API}/emails/sync", json={}, headers=member_headers)
    assert (await harness.client.post(f"{API}/emails/sync", json={}, headers=member_headers)).status_code == 429

    # Act: another user from the same IP
    async with harness.new_client() as approver:
        await approver.post(f"{API}/auth/demo-login", json={"persona": "approver"})
        headers = {"X-CSRF-Token": approver.cookies.get(harness.settings.csrf_cookie_name) or ""}
        resp = await approver.post(f"{API}/emails/sync", json={}, headers=headers)

    # Assert
    assert resp.status_code == 202


@SHARED_STORE
async def test_sign_in_stays_throttled_when_the_shared_store_fails(harness: Harness) -> None:
    # Arrange: a member signed in earlier; then the counter table becomes unusable (e.g. a broken migration)
    member_headers = await harness.login("member")
    async with harness.db.engine.begin() as conn:
        await conn.execute(text("DROP TABLE rate_limit_counters"))

    # Act
    attempts = []
    for _ in range(5):
        async with harness.new_client() as attacker:
            attempts.append(await attacker.post(f"{API}/auth/demo-login", json={"persona": "admin"}))
    sync = await harness.client.post(f"{API}/emails/sync", json={}, headers=member_headers)

    # Assert: breaking the limiter never opens the sign-in routes (fail closed) …
    assert [r.status_code for r in attempts] == [429] * 5
    assert all(r.headers["retry-after"] == "5" for r in attempts)
    # … while verified users keep working (fail open, RBAC and idempotency still apply)
    assert sync.status_code == 202


@SHARED_STORE
async def test_shared_counters_never_store_client_ips_or_user_ids(harness: Harness) -> None:
    # Arrange / Act: sign-in (per IP) and mailbox sync (per user) both write counters
    member_headers = await harness.login("member")
    for _ in range(3):
        await harness.client.post(f"{API}/emails/sync", json={}, headers=member_headers)
    me = (await harness.client.get(f"{API}/auth/me")).json()["user"]

    async with harness.db.session() as s:
        stored = [tuple(row) for row in (await s.execute(select(RateLimitCounter.__table__))).all()]

    # Assert: keyed digests only — not reversible without AUDIT_HMAC_KEY, and no raw identity
    assert len(stored) == 2
    assert all(re.fullmatch(r"[0-9a-f]{64}", str(row[0])) for row in stored)
    dump = repr(stored)
    for identity in ("127.0.0.1", me["id"], me["id"].replace("-", ""), "ip:", "user:", "sync", "auth"):
        assert identity not in dump
