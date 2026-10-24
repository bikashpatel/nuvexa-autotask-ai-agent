"""Runs last (file order): exhausts the login rate limit for this client, which would affect later tests."""

from __future__ import annotations

import httpx

from e2e.conftest import API


def test_login_rate_limit_applies_through_the_proxy_even_with_spoofed_headers() -> None:
    statuses = []
    for i in range(30):
        resp = httpx.post(
            f"{API}/auth/demo-login",
            json={"persona": "viewer"},
            headers={"X-Forwarded-For": f"198.51.100.{i}"},  # rotating spoofed IPs must not reset the limit
        )
        statuses.append(resp.status_code)
        if resp.status_code == 429:
            body = resp.json()
            assert body["error"]["code"] == "rate_limited"
            assert body["error"]["correlation_id"]
            break
    assert 429 in statuses, f"no rate limiting after {len(statuses)} logins: {statuses}"
