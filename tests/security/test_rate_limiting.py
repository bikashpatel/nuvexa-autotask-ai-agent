"""Rate limits cannot be reset by the client: anonymous requests are keyed by IP, authenticated ones by user."""

from __future__ import annotations

import secrets

import pytest

from app.core.config import Settings
from tests.conftest import make_settings
from tests.security.conftest import Harness

API = "/api/v1"


@pytest.fixture
def settings(database_url: str) -> Settings:  # overrides the shared fixture: limits ON for this module
    return make_settings(database_url, rate_limit_enabled=True)


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
