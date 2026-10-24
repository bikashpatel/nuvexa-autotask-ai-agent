"""Login sessions: client attribution in the audit trail and session rotation (fixation defence)."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import AuditEvent
from tests.security.conftest import Harness

API = "/api/v1"
ASGI_CLIENT_IP = "127.0.0.1"  # httpx.ASGITransport's default client address


async def test_demo_login_audit_event_records_the_client_ip(harness: Harness) -> None:
    # Arrange / Act
    await harness.login("member")

    # Assert
    async with harness.db.session() as s:
        event = (
            (
                await s.execute(
                    select(AuditEvent).where(AuditEvent.event_type == "login").order_by(AuditEvent.created_at.desc())
                )
            )
            .scalars()
            .first()
        )
    assert event is not None
    assert event.ip_address == ASGI_CLIENT_IP


async def test_logging_in_again_revokes_the_previous_session(harness: Harness) -> None:
    # Arrange: an existing session in this browser
    await harness.login("member")
    old_token = harness.client.cookies.get(harness.settings.session_cookie_name)
    assert old_token

    # Act: the same browser logs in again (cookie is sent with the login request)
    await harness.login("member")
    new_token = harness.client.cookies.get(harness.settings.session_cookie_name)

    # Assert: a new session was issued and the old one can no longer be used
    assert new_token != old_token
    async with harness.new_client() as replay:
        replay.cookies.set(harness.settings.session_cookie_name, old_token)
        assert (await replay.get(f"{API}/auth/me")).status_code == 401
    assert (await harness.client.get(f"{API}/auth/me")).status_code == 200
