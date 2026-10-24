from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import InvalidStateTransition
from app.core.security import SecretBox, compute_payload_hash
from app.core.text import html_to_text, normalize_text
from app.core.timeutil import utcnow
from app.db.models import ConnectedAccount, EmailMessage
from app.domain.enums import ActionStatus, ConnectionProvider, WorkflowStatus
from app.domain.state_machine import derive_review_status, ensure_action_transition
from tests.conftest import TEST_FERNET_KEY


def test_payload_hash_is_order_independent_and_sensitive_to_changes():
    a = compute_payload_hash("t", "x", {"b": 1, "a": "z"})
    b = compute_payload_hash("t", "x", {"a": "z", "b": 1})
    c = compute_payload_hash("t", "x", {"a": "z", "b": 2})
    assert a == b
    assert a != c


def test_secret_box_roundtrip_and_rotation():
    box = SecretBox([TEST_FERNET_KEY])
    token = box.encrypt_str("refresh-token-value")
    assert b"refresh-token-value" not in token
    from cryptography.fernet import Fernet

    rotated = SecretBox([Fernet.generate_key().decode(), TEST_FERNET_KEY])
    assert rotated.decrypt_str(token) == "refresh-token-value"


def test_terminal_action_states_cannot_transition():
    with pytest.raises(InvalidStateTransition):
        ensure_action_transition(ActionStatus.COMPLETED, ActionStatus.EXECUTING)
    with pytest.raises(InvalidStateTransition):
        ensure_action_transition(ActionStatus.REJECTED, ActionStatus.APPROVED)
    with pytest.raises(InvalidStateTransition):
        ensure_action_transition(ActionStatus.PENDING_APPROVAL, ActionStatus.EXECUTING)


def test_derive_review_status():
    assert (
        derive_review_status([ActionStatus.PENDING_APPROVAL, ActionStatus.COMPLETED]) is WorkflowStatus.PENDING_APPROVAL
    )
    assert derive_review_status([ActionStatus.REJECTED]) is WorkflowStatus.REJECTED
    assert derive_review_status([ActionStatus.COMPLETED, ActionStatus.REJECTED]) is WorkflowStatus.COMPLETED
    assert derive_review_status([ActionStatus.UNCERTAIN]) is WorkflowStatus.FAILED


def test_html_to_text_drops_scripts_and_invisible_chars():
    text = html_to_text("<p>Hello<script>alert(1)</script></p><style>x{}</style><div>W\u200borld</div>")
    assert "alert" not in text
    assert "Hello" in text
    assert "World" in text
    assert normalize_text("a\r\n\r\n\r\n\r\nb") == "a\n\nb"


async def test_duplicate_provider_message_is_rejected(session, tenant, make_user):
    user = await make_user(tenant)
    account = ConnectedAccount(
        tenant_id=tenant.id, user_id=user.id, provider=ConnectionProvider.DEMO_MAILBOX, external_account_id="demo"
    )
    session.add(account)
    await session.commit()

    def msg() -> EmailMessage:
        return EmailMessage(
            tenant_id=tenant.id,
            owner_user_id=user.id,
            connected_account_id=account.id,
            provider_message_id="m-1",
            received_at=utcnow(),
            content_hash="0" * 64,
        )

    session.add(msg())
    await session.commit()
    session.add(msg())
    with pytest.raises(IntegrityError):
        await session.commit()
