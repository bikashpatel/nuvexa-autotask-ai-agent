from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import Conflict, IntegrationError, IntegrationNotConfigured
from app.core.timeutil import utcnow
from app.db.models import ConnectedAccount, CredentialRecord, EmailMessage, EmailSyncCheckpoint, WorkflowRun
from app.domain.enums import (
    ActorType,
    AuditOutcome,
    ConnectionProvider,
    ConnectionStatus,
    EmailAnalysisStatus,
    FailureCategory,
    JobType,
    SyncStatus,
    UserRole,
    WorkflowStatus,
    WorkflowTrigger,
)
from app.schemas.email import AttachmentMeta, EmailAddress, MailChangeSet, NormalizedEmail
from app.schemas.settings import TenantSettings
from app.services import email_sync_service
from app.services.authz import AuditedPermissionDenied, Perm
from app.services.email_sync_service import INBOX, EmailSyncService, _recipients, apply_normalized
from app.services.settings_service import load_settings_row
from tests.unit.services.conftest import RECEIVED_AT, Factory, Fresh, World

SENT_AT = datetime(2026, 10, 1, 9, 29, tzinfo=UTC)


def _msg(provider_id: str = "p-1", **kw: Any) -> NormalizedEmail:
    values: dict[str, Any] = {
        "provider_message_id": provider_id,
        "internet_message_id": f"<{provider_id}@example.com>",
        "conversation_id": "conv-1",
        "change_key": "ck-1",
        "subject": "Printer on fire",
        "sender": EmailAddress(name="Dana Requester", address="Dana@Example.COM"),
        "to_recipients": [EmailAddress(name="Desk", address="Support@Example.com")],
        "cc_recipients": [EmailAddress(name="No Address", address=None)],
        "received_at": RECEIVED_AT,
        "sent_at": SENT_AT,
        "body_text": "The printer on floor 3 is on fire.",
        "body_preview": "The printer on floor 3",
        "original_body_content_type": "html",
        "has_attachments": True,
        "attachments": [AttachmentMeta(name="photo.jpg", content_type="image/jpeg", size=1234)],
        "importance": "high",
        "is_read": False,
        "web_link": "https://outlook.example.invalid/mail/p-1",
    }
    values.update(kw)
    return NormalizedEmail(**values)


class FakeProvider:
    """Scripted `MailboxProvider`: each fetch pops the next result (a change set or an exception)."""

    def __init__(self, *results: MailChangeSet | BaseException) -> None:
        self.results = list(results)
        self.calls: list[tuple[str | None, int, int]] = []
        self.on_fetch: Callable[[], Awaitable[None]] | None = None

    async def fetch_changes(self, delta_link: str | None, *, max_messages: int, initial_days: int) -> MailChangeSet:
        self.calls.append((delta_link, max_messages, initial_days))
        if self.on_fetch is not None:
            await self.on_fetch()
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _install(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> list[ConnectedAccount]:
    built: list[ConnectedAccount] = []

    def build(account: ConnectedAccount, *, session: AsyncSession, settings: Settings) -> FakeProvider:
        built.append(account)
        return provider

    monkeypatch.setattr(email_sync_service, "build_mailbox_provider", build)
    return built


async def _checkpoint(fresh: Fresh, account: ConnectedAccount) -> EmailSyncCheckpoint:
    [cp] = await fresh.all(EmailSyncCheckpoint, EmailSyncCheckpoint.connected_account_id == account.id)
    return cp


async def _email_by_pid(fresh: Fresh, account: ConnectedAccount, pid: str) -> EmailMessage:
    [email] = await fresh.all(
        EmailMessage, EmailMessage.connected_account_id == account.id, EmailMessage.provider_message_id == pid
    )
    return email


# ---------------------------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------------------------


def test_recipients_lowercase_addresses_and_keep_missing_ones() -> None:
    assert _recipients([EmailAddress(name="A", address="A.B@Example.COM"), EmailAddress(name=None, address=None)]) == [
        {"name": "A", "address": "a.b@example.com"},
        {"name": None, "address": None},
    ]
    assert _recipients([]) == []


def test_apply_normalized_maps_every_field() -> None:
    email = EmailMessage(content_hash="", deleted_at=utcnow(), body_purged_at=utcnow())
    msg = _msg()
    assert apply_normalized(email, msg) is True

    assert email.internet_message_id == "<p-1@example.com>"
    assert email.conversation_id == "conv-1"
    assert email.change_key == "ck-1"
    assert email.subject == "Printer on fire"
    assert email.sender_name == "Dana Requester"
    assert email.sender_address == "dana@example.com"
    assert email.to_recipients == [{"name": "Desk", "address": "support@example.com"}]
    assert email.cc_recipients == [{"name": "No Address", "address": None}]
    assert email.received_at == RECEIVED_AT
    assert email.sent_at == SENT_AT
    assert email.body_text == "The printer on floor 3 is on fire."
    assert email.body_preview == "The printer on floor 3"
    assert email.body_content_type == "html"
    assert email.has_attachments is True
    assert email.attachments == [{"name": "photo.jpg", "content_type": "image/jpeg", "size": 1234, "is_inline": False}]
    assert email.importance == "high"
    assert email.is_read is False
    assert email.web_link == "https://outlook.example.invalid/mail/p-1"
    assert email.content_hash == msg.content_hash()
    assert email.deleted_at is None  # re-delivered → visible again
    assert email.body_purged_at is None


def test_apply_normalized_reports_content_change_only_for_analysis_relevant_fields() -> None:
    email = EmailMessage(content_hash="")
    assert apply_normalized(email, _msg()) is True
    assert apply_normalized(email, _msg(is_read=True, change_key="ck-2", importance="low")) is False
    assert apply_normalized(email, _msg(body_text="Now it is out.")) is True
    assert apply_normalized(email, _msg(body_text="Now it is out.", subject="Printer fixed")) is True


def test_apply_normalized_handles_missing_sender_and_preview_fallback() -> None:
    email = EmailMessage(content_hash="")
    body = "x" * 600
    apply_normalized(email, _msg(sender=None, body_preview="", body_text=body))
    assert email.sender_name is None
    assert email.sender_address is None
    assert email.body_preview == "x" * 500  # falls back to the body, truncated

    anonymous = EmailMessage(content_hash="")
    apply_normalized(anonymous, _msg(sender=EmailAddress(name="Anon", address=None)))
    assert anonymous.sender_name == "Anon"
    assert anonymous.sender_address is None


# ---------------------------------------------------------------------------------------------
# request_sync
# ---------------------------------------------------------------------------------------------


async def test_request_sync_requires_sync_permission(session: AsyncSession, settings: Settings, world: World) -> None:
    with pytest.raises(AuditedPermissionDenied) as info:
        await EmailSyncService(settings).request_sync(session, world.actor(world.viewer), None)
    assert info.value.permission == Perm.EMAIL_SYNC
    assert info.value.resource_type == "mailbox"


async def test_request_sync_only_queues_own_active_mailboxes(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, fresh: Fresh
) -> None:
    graph = await factory.account(world.owner, ConnectionProvider.MICROSOFT_GRAPH)
    await factory.account(world.owner, ConnectionProvider.AZURE_DEVOPS)
    await factory.account(world.owner, ConnectionProvider.MICROSOFT_GRAPH, status=ConnectionStatus.NEEDS_REAUTH)
    await factory.account(world.owner, ConnectionProvider.MICROSOFT_GRAPH, status=ConnectionStatus.DISCONNECTED)
    await factory.account(world.other)
    other_tenant = await factory.tenant()
    await factory.account(await factory.user(other_tenant))
    service = EmailSyncService(settings)
    actor = world.actor(world.owner)

    job_ids = await service.request_sync(session, actor, None)

    jobs = {j.id: j for j in await fresh.jobs()}
    assert set(job_ids) == set(jobs)
    by_account = {j.payload["connected_account_id"]: j for j in jobs.values()}
    assert set(by_account) == {str(world.account.id), str(graph.id)}
    for account_id, job in by_account.items():
        assert job.job_type is JobType.SYNC_MAILBOX
        assert job.tenant_id == world.tenant.id
        assert job.payload == {"connected_account_id": account_id, "requested_by": str(world.owner.id)}
        assert job.dedupe_key == f"sync:{account_id}"
        assert job.concurrency_key == f"sync:{account_id}"
    [event] = await fresh.audits("mailbox_sync_requested")
    assert event.resource_type == "mailbox"
    assert event.resource_id is None
    assert event.actor_user_id == world.owner.id
    assert sorted(event.details["accounts"]) == sorted([str(world.account.id), str(graph.id)])

    assert sorted(await service.request_sync(session, actor, None)) == sorted(job_ids)  # deduplicated
    assert len(await fresh.jobs()) == 2


async def test_request_sync_for_one_account(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, fresh: Fresh
) -> None:
    graph = await factory.account(world.owner, ConnectionProvider.MICROSOFT_GRAPH)
    [job_id] = await EmailSyncService(settings).request_sync(session, world.actor(world.owner), graph.id)
    [job] = await fresh.jobs()
    assert job.id == job_id
    assert job.payload["connected_account_id"] == str(graph.id)


async def test_request_sync_rejects_other_users_account_and_missing_mailbox(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, fresh: Fresh
) -> None:
    service = EmailSyncService(settings)
    with pytest.raises(Conflict, match="No connected mailbox"):
        await service.request_sync(session, world.actor(world.owner), (await factory.account(world.other)).id)
    with pytest.raises(Conflict, match="No connected mailbox"):
        await service.request_sync(session, world.actor(world.approver), None)
    with pytest.raises(Conflict):
        await service.request_sync(session, world.actor(world.owner), uuid.uuid4())
    assert await fresh.jobs() == []
    assert await fresh.audits("mailbox_sync_requested") == []


# ---------------------------------------------------------------------------------------------
# sync_account
# ---------------------------------------------------------------------------------------------


async def test_sync_account_rejects_missing_and_inactive_accounts(
    session: AsyncSession, settings: Settings, factory: Factory, world: World
) -> None:
    service = EmailSyncService(settings)
    with pytest.raises(Conflict, match="not active"):
        await service.sync_account(session, uuid.uuid4())
    reauth = await factory.account(
        world.owner, ConnectionProvider.MICROSOFT_GRAPH, status=ConnectionStatus.NEEDS_REAUTH
    )
    with pytest.raises(Conflict, match="not active"):
        await service.sync_account(session, reauth.id)


async def test_sync_account_inserts_new_messages_and_checkpoints(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider(MailChangeSet(upserts=[_msg("p-1"), _msg("p-2")], delta_link="delta-1"))
    built = _install(monkeypatch, provider)
    observed: list[SyncStatus] = []

    async def check_running() -> None:  # "running" is committed before the provider is called
        observed.append((await _checkpoint(fresh, world.account)).last_status)

    provider.on_fetch = check_running
    result = await EmailSyncService(settings).sync_account(session, world.account.id)

    assert [a.id for a in built] == [world.account.id]
    assert observed == [SyncStatus.RUNNING]
    assert provider.calls == [(None, settings.mail_sync_max_messages, settings.mail_sync_initial_days)]
    assert (result.account_id, result.inserted, result.updated, result.removed) == (world.account.id, 2, 0, 0)
    assert result.truncated is False
    assert len(result.new_email_ids) == 2

    email = await _email_by_pid(fresh, world.account, "p-1")
    assert email.id in result.new_email_ids
    assert email.tenant_id == world.tenant.id
    assert email.owner_user_id == world.owner.id
    assert email.analysis_status is EmailAnalysisStatus.NOT_ANALYZED
    assert email.sender_address == "dana@example.com"
    assert email.content_hash == _msg("p-1").content_hash()
    assert email.content_changed_since_analysis is False

    cp = await _checkpoint(fresh, world.account)
    assert cp.folder == INBOX
    assert cp.tenant_id == world.tenant.id
    assert cp.delta_link == "delta-1"
    assert cp.last_status is SyncStatus.SUCCEEDED
    assert cp.last_sync_started_at is not None
    assert cp.last_sync_completed_at is not None
    assert cp.last_sync_completed_at >= cp.last_sync_started_at
    assert cp.last_error is None
    assert cp.messages_ingested_total == 2

    [event] = await fresh.audits("mailbox_synced")
    assert event.resource_type == "connected_account"
    assert event.resource_id == str(world.account.id)
    assert event.actor_type is ActorType.SYSTEM
    assert event.details == {"inserted": 2, "updated": 0, "removed": 0, "truncated": False}
    assert await fresh.all(WorkflowRun, WorkflowRun.email_message_id == email.id) == []  # auto-analyze off


async def test_sync_account_uses_saved_delta_link_and_accumulates_total(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider(
        MailChangeSet(upserts=[_msg("p-1")], delta_link="delta-1"),
        MailChangeSet(upserts=[_msg("p-2"), _msg("p-3")], delta_link="delta-2", truncated=True),
    )
    _install(monkeypatch, provider)
    service = EmailSyncService(settings)

    await service.sync_account(session, world.account.id)
    result = await service.sync_account(session, world.account.id)

    assert [c[0] for c in provider.calls] == [None, "delta-1"]
    assert result.inserted == 2
    assert result.truncated is True
    cp = await _checkpoint(fresh, world.account)
    assert cp.delta_link == "delta-2"
    assert cp.messages_ingested_total == 3
    assert len(await fresh.all(EmailSyncCheckpoint)) == 1
    events = await fresh.audits("mailbox_synced")
    assert sorted(e.details["truncated"] for e in events) == [False, True]


async def test_sync_account_skips_duplicate_delivery(
    session: AsyncSession,
    settings: Settings,
    factory: Factory,
    world: World,
    fresh: Fresh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = await factory.email(world.account, provider_message_id="p-1", change_key="ck-1", subject="Original")
    _install(monkeypatch, FakeProvider(MailChangeSet(upserts=[_msg("p-1", change_key="ck-1")])))

    result = await EmailSyncService(settings).sync_account(session, world.account.id)

    assert (result.inserted, result.updated, result.new_email_ids) == (0, 0, [])
    stored = await fresh.get(EmailMessage, existing.id)
    assert stored is not None
    assert stored.subject == "Original"  # unchanged message is not re-applied
    assert (await _checkpoint(fresh, world.account)).delta_link is None


async def test_sync_account_update_without_content_change_keeps_analysis_fresh(
    session: AsyncSession,
    settings: Settings,
    factory: Factory,
    world: World,
    fresh: Fresh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    msg = _msg("p-1", change_key="ck-2", is_read=True)
    existing = await factory.email(
        world.account,
        provider_message_id="p-1",
        change_key="ck-1",
        content_hash=msg.content_hash(),
        analysis_status=EmailAnalysisStatus.AWAITING_REVIEW,
    )
    _install(monkeypatch, FakeProvider(MailChangeSet(upserts=[msg])))

    result = await EmailSyncService(settings).sync_account(session, world.account.id)

    assert (result.inserted, result.updated) == (0, 1)
    stored = await fresh.get(EmailMessage, existing.id)
    assert stored is not None
    assert stored.change_key == "ck-2"
    assert stored.is_read is True
    assert stored.content_changed_since_analysis is False
    assert stored.analysis_status is EmailAnalysisStatus.AWAITING_REVIEW


@pytest.mark.parametrize(
    ("analysis_status", "flagged"),
    [
        (EmailAnalysisStatus.DONE, True),
        (EmailAnalysisStatus.AWAITING_REVIEW, True),
        (EmailAnalysisStatus.NOT_ANALYZED, False),
    ],
)
async def test_sync_account_content_change_flags_analyzed_emails(
    session: AsyncSession,
    settings: Settings,
    factory: Factory,
    world: World,
    fresh: Fresh,
    monkeypatch: pytest.MonkeyPatch,
    analysis_status: EmailAnalysisStatus,
    flagged: bool,
) -> None:
    existing = await factory.email(
        world.account, provider_message_id="p-1", change_key=None, analysis_status=analysis_status
    )
    _install(monkeypatch, FakeProvider(MailChangeSet(upserts=[_msg("p-1", body_text="Edited body.")])))

    result = await EmailSyncService(settings).sync_account(session, world.account.id)

    assert result.updated == 1
    stored = await fresh.get(EmailMessage, existing.id)
    assert stored is not None
    assert stored.body_text == "Edited body."
    assert stored.change_key == "ck-1"
    assert stored.content_changed_since_analysis is flagged


async def test_sync_account_redelivered_deleted_email_is_restored(
    session: AsyncSession,
    settings: Settings,
    factory: Factory,
    world: World,
    fresh: Fresh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = await factory.email(world.account, provider_message_id="p-1", change_key="ck-1", deleted_at=utcnow())
    _install(monkeypatch, FakeProvider(MailChangeSet(upserts=[_msg("p-1", change_key="ck-1")])))

    result = await EmailSyncService(settings).sync_account(session, world.account.id)

    assert result.updated == 1
    stored = await fresh.get(EmailMessage, existing.id)
    assert stored is not None
    assert stored.deleted_at is None


async def test_sync_account_soft_deletes_removed_messages(
    session: AsyncSession,
    settings: Settings,
    factory: Factory,
    world: World,
    fresh: Fresh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed = await factory.email(world.account, provider_message_id="p-1")
    earlier = datetime(2026, 9, 1, tzinfo=UTC)
    already = await factory.email(world.account, provider_message_id="p-2", deleted_at=earlier)
    kept = await factory.email(world.account, provider_message_id="p-3")
    second_account = await factory.account(world.owner, ConnectionProvider.MICROSOFT_GRAPH)
    same_id_elsewhere = await factory.email(second_account, provider_message_id="p-1")
    _install(monkeypatch, FakeProvider(MailChangeSet(removed_provider_ids=["p-1", "p-2", "unknown"])))

    result = await EmailSyncService(settings).sync_account(session, world.account.id)

    assert result.removed == 1
    stored_removed = await fresh.get(EmailMessage, removed.id)
    assert stored_removed is not None
    assert stored_removed.deleted_at is not None
    stored_already = await fresh.get(EmailMessage, already.id)
    assert stored_already is not None
    assert stored_already.deleted_at == earlier  # not re-stamped
    for untouched in (kept, same_id_elsewhere):
        stored = await fresh.get(EmailMessage, untouched.id)
        assert stored is not None
        assert stored.deleted_at is None
    [event] = await fresh.audits("mailbox_synced")
    assert event.details["removed"] == 1


async def test_sync_account_delta_expired_triggers_full_resync(
    session: AsyncSession,
    settings: Settings,
    factory: Factory,
    world: World,
    fresh: Fresh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider(
        MailChangeSet(delta_link="old-link"),
        IntegrationError("delta token expired", category=FailureCategory.DELTA_EXPIRED),
        MailChangeSet(upserts=[_msg("p-1")], delta_link="new-link"),
    )
    _install(monkeypatch, provider)
    service = EmailSyncService(settings)
    await service.sync_account(session, world.account.id)

    result = await service.sync_account(session, world.account.id)

    assert [c[0] for c in provider.calls] == [None, "old-link", None]
    assert result.inserted == 1
    cp = await _checkpoint(fresh, world.account)
    assert cp.delta_link == "new-link"
    assert cp.last_status is SyncStatus.SUCCEEDED
    assert await fresh.audits("mailbox_sync_failed") == []


async def test_sync_account_authorization_failure_marks_account_for_reauth(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = IntegrationError("token revoked by the user", category=FailureCategory.AUTHORIZATION, status=401)
    _install(monkeypatch, FakeProvider(error))

    with pytest.raises(IntegrationError) as info:
        await EmailSyncService(settings).sync_account(session, world.account.id)
    assert info.value is error

    account = await fresh.get(ConnectedAccount, world.account.id)
    assert account is not None
    assert account.status is ConnectionStatus.NEEDS_REAUTH
    assert account.last_error is not None
    assert "Reconnect the mailbox" in account.last_error
    cp = await _checkpoint(fresh, world.account)
    assert cp.last_status is SyncStatus.FAILED
    assert cp.last_error == "token revoked by the user"
    [event] = await fresh.audits("mailbox_sync_failed")
    assert event.outcome is AuditOutcome.FAILURE
    assert event.resource_id == str(world.account.id)
    assert event.details == {"category": "authorization"}
    assert await fresh.audits("mailbox_synced") == []


@pytest.mark.parametrize(
    "script",
    [
        [IntegrationError("Graph throttled", category=FailureCategory.RATE_LIMITED, retryable=True)],
        [
            IntegrationError("expired", category=FailureCategory.DELTA_EXPIRED),
            IntegrationError("Graph throttled", category=FailureCategory.RATE_LIMITED, retryable=True),
        ],
    ],
    ids=["first-fetch", "resync-after-delta-expired"],
)
async def test_sync_account_other_integration_errors_are_recorded(
    session: AsyncSession,
    settings: Settings,
    world: World,
    fresh: Fresh,
    monkeypatch: pytest.MonkeyPatch,
    script: list[IntegrationError],
) -> None:
    provider = FakeProvider(*script)
    _install(monkeypatch, provider)

    with pytest.raises(IntegrationError, match="Graph throttled"):
        await EmailSyncService(settings).sync_account(session, world.account.id)

    assert len(provider.calls) == len(script)
    account = await fresh.get(ConnectedAccount, world.account.id)
    assert account is not None
    assert account.status is ConnectionStatus.ACTIVE  # only authorization failures need a reconnect
    assert account.last_error is None
    cp = await _checkpoint(fresh, world.account)
    assert cp.last_status is SyncStatus.FAILED
    assert cp.last_error == "Graph throttled"
    [event] = await fresh.audits("mailbox_sync_failed")
    assert event.details == {"category": "rate_limited"}


async def test_sync_account_provider_build_failure_is_recorded(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    def build(account: ConnectedAccount, *, session: AsyncSession, settings: Settings) -> FakeProvider:
        raise IntegrationNotConfigured("The demo mailbox is only available when DEMO_MODE=true")

    monkeypatch.setattr(email_sync_service, "build_mailbox_provider", build)
    with pytest.raises(IntegrationNotConfigured):
        await EmailSyncService(settings).sync_account(session, world.account.id)
    cp = await _checkpoint(fresh, world.account)
    assert cp.last_status is SyncStatus.FAILED
    [event] = await fresh.audits("mailbox_sync_failed")
    assert event.details == {"category": "not_configured"}


async def test_sync_account_unexpected_errors_are_recorded_and_raised_as_integration_errors(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, FakeProvider(RuntimeError("parser bug")))
    with pytest.raises(IntegrationError) as info:
        await EmailSyncService(settings).sync_account(session, world.account.id)
    assert isinstance(info.value.__cause__, RuntimeError)  # original error kept for the server log only
    assert "parser bug" not in info.value.message
    await session.rollback()
    [failure] = await fresh.audits("mailbox_sync_failed")
    assert failure.outcome is AuditOutcome.FAILURE
    assert await fresh.audits("mailbox_synced") == []
    [checkpoint] = await fresh.all(EmailSyncCheckpoint)
    assert checkpoint.last_status is SyncStatus.FAILED
    assert await fresh.all(EmailMessage, EmailMessage.provider_message_id == "p-1") == []


async def test_record_failure_for_unknown_account_is_a_noop(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh
) -> None:
    await EmailSyncService(settings)._record_failure(session, uuid.uuid4(), IntegrationError("x", category="transient"))
    assert await fresh.audits() == []
    assert await fresh.all(EmailSyncCheckpoint) == []


async def _enable_auto_analyze(session: AsyncSession, settings: Settings, world: World) -> None:
    row = await load_settings_row(session, world.tenant.id, settings)
    row.settings = TenantSettings(auto_analyze_new_emails=True).model_dump(mode="json")
    await session.commit()


async def test_sync_account_auto_analyzes_new_emails_when_enabled(
    session: AsyncSession,
    settings: Settings,
    factory: Factory,
    world: World,
    fresh: Fresh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _enable_auto_analyze(session, settings, world)
    updated = await factory.email(world.account, provider_message_id="p-old", change_key=None)
    _install(
        monkeypatch,
        FakeProvider(MailChangeSet(upserts=[_msg("p-1"), _msg("p-2"), _msg("p-old", body_text="Changed")])),
    )

    result = await EmailSyncService(settings).sync_account(session, world.account.id)

    assert (result.inserted, result.updated) == (2, 1)
    runs = await fresh.all(WorkflowRun, WorkflowRun.id != world.run.id)
    assert sorted(r.email_message_id for r in runs) == sorted(result.new_email_ids)  # only new emails
    for run in runs:
        assert run.trigger is WorkflowTrigger.AUTO
        assert run.requested_by_user_id is None
        assert run.status is WorkflowStatus.RECEIVED
        assert run.owner_user_id == world.owner.id
        email = await fresh.get(EmailMessage, run.email_message_id)
        assert email is not None
        assert email.analysis_status is EmailAnalysisStatus.QUEUED
        assert email.latest_workflow_run_id == run.id
    jobs = await fresh.jobs(job_type=JobType.ANALYZE_EMAIL)
    assert sorted(j.payload["workflow_run_id"] for j in jobs) == sorted(str(r.id) for r in runs)
    assert updated.id not in {r.email_message_id for r in runs}


async def test_auto_analyze_skips_unknown_emails(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh
) -> None:
    await _enable_auto_analyze(session, settings, world)
    await EmailSyncService(settings)._auto_analyze(session, world.account, [uuid.uuid4()])
    assert len(await fresh.all(WorkflowRun)) == 1  # only the fixture's run
    assert await fresh.jobs() == []


async def test_auto_analyze_disabled_creates_no_runs(
    session: AsyncSession, settings: Settings, factory: Factory, world: World, fresh: Fresh
) -> None:
    email = await factory.email(world.account)
    await EmailSyncService(settings)._auto_analyze(session, world.account, [email.id])
    assert await fresh.all(WorkflowRun, WorkflowRun.email_message_id == email.id) == []
    assert await fresh.jobs() == []


async def test_sync_account_for_viewer_mailbox_owner(
    session: AsyncSession,
    settings: Settings,
    factory: Factory,
    world: World,
    fresh: Fresh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The worker syncs by account id; the email is owned by the account's user regardless of role.
    account = await factory.account(await factory.user(world.tenant, UserRole.APPROVER))
    _install(monkeypatch, FakeProvider(MailChangeSet(upserts=[_msg("p-9")])))
    await EmailSyncService(settings).sync_account(session, account.id)
    email = await _email_by_pid(fresh, account, "p-9")
    assert email.owner_user_id == account.user_id
    assert email.connected_account_id == account.id


async def test_sync_failure_is_recorded_when_the_provider_used_the_session(
    session: AsyncSession, settings: Settings, world: World, fresh: Fresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: the real Graph provider loads encrypted credentials through the sync session before failing.
    # The rollback in the error path then expired `account`, and reading `account.id` raised MissingGreenlet,
    # so the failure was never recorded (no needs_reauth, checkpoint stuck in "running").
    provider = FakeProvider(IntegrationError("refresh token revoked", category=FailureCategory.AUTHORIZATION))

    async def load_credentials_like_the_token_provider() -> None:
        await session.execute(select(CredentialRecord).where(CredentialRecord.connected_account_id == world.account.id))

    provider.on_fetch = load_credentials_like_the_token_provider
    _install(monkeypatch, provider)
    account_id = world.account.id

    with pytest.raises(IntegrationError, match="refresh token revoked"):
        await EmailSyncService(settings).sync_account(session, account_id)

    account = await fresh.get(ConnectedAccount, account_id)
    assert account is not None
    assert account.status is ConnectionStatus.NEEDS_REAUTH
    [cp] = await fresh.all(EmailSyncCheckpoint, EmailSyncCheckpoint.connected_account_id == account_id)
    assert cp.last_status is SyncStatus.FAILED
    assert cp.last_error == "refresh token revoked"
    [event] = await fresh.audits("mailbox_sync_failed")
    assert event.details == {"category": "authorization"}
