"""Mailbox synchronization: provider change sets → idempotent upserts keyed by provider message id."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import Conflict, IntegrationError
from app.core.timeutil import utcnow
from app.db.models import ConnectedAccount, EmailMessage, EmailSyncCheckpoint
from app.domain.enums import (
    AuditOutcome,
    ConnectionStatus,
    EmailAnalysisStatus,
    FailureCategory,
    JobType,
    SyncStatus,
    WorkflowTrigger,
)
from app.integrations.microsoft_graph.factory import build_mailbox_provider
from app.observability.logging import get_logger
from app.observability.tracing import start_span
from app.schemas.email import NormalizedEmail
from app.services.audit import record_audit
from app.services.authz import Actor, Perm, require
from app.services.settings_service import get_tenant_settings
from app.services.workflow_service import WorkflowService
from app.workers.queue import enqueue

log = get_logger(__name__)

INBOX = "inbox"


@dataclass
class SyncResult:
    account_id: uuid.UUID
    inserted: int = 0
    updated: int = 0
    removed: int = 0
    truncated: bool = False
    new_email_ids: list[uuid.UUID] = field(default_factory=list)


def _recipients(items: list[object]) -> list[dict[str, str | None]]:
    out = []
    for r in items:
        name = getattr(r, "name", None)
        address = getattr(r, "address", None)
        out.append({"name": name, "address": address.lower() if address else None})
    return out


def apply_normalized(email: EmailMessage, msg: NormalizedEmail) -> bool:
    """Copy provider fields onto the row. Returns True if analysis-relevant content changed."""
    new_hash = msg.content_hash()
    changed = email.content_hash != new_hash
    email.internet_message_id = msg.internet_message_id
    email.conversation_id = msg.conversation_id
    email.change_key = msg.change_key
    email.subject = msg.subject
    email.sender_name = msg.sender.name if msg.sender else None
    email.sender_address = msg.sender.address.lower() if msg.sender and msg.sender.address else None
    email.to_recipients = _recipients(list(msg.to_recipients))
    email.cc_recipients = _recipients(list(msg.cc_recipients))
    email.received_at = msg.received_at
    email.sent_at = msg.sent_at
    email.body_text = msg.body_text
    email.body_preview = (msg.body_preview or msg.body_text)[:500]
    email.body_content_type = msg.original_body_content_type
    email.has_attachments = msg.has_attachments
    email.attachments = [a.model_dump(mode="json") for a in msg.attachments]
    email.importance = msg.importance
    email.is_read = msg.is_read
    email.web_link = msg.web_link
    email.content_hash = new_hash
    email.deleted_at = None
    email.body_purged_at = None
    return changed


class EmailSyncService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def request_sync(
        self, session: AsyncSession, actor: Actor, connected_account_id: uuid.UUID | None
    ) -> list[uuid.UUID]:
        require(actor, Perm.EMAIL_SYNC, resource_type="mailbox")
        stmt = select(ConnectedAccount).where(
            ConnectedAccount.tenant_id == actor.tenant_id,
            ConnectedAccount.user_id == actor.user_id,  # only the user's own mailboxes
            ConnectedAccount.status == ConnectionStatus.ACTIVE,
            ConnectedAccount.provider.in_(["microsoft_graph", "demo_mailbox"]),
        )
        if connected_account_id is not None:
            stmt = stmt.where(ConnectedAccount.id == connected_account_id)
        accounts = (await session.execute(stmt)).scalars().all()
        if not accounts:
            raise Conflict("No connected mailbox to synchronize. Connect Microsoft 365 first.")
        job_ids = []
        for account in accounts:
            job = await enqueue(
                session,
                job_type=JobType.SYNC_MAILBOX,
                payload={"connected_account_id": str(account.id), "requested_by": str(actor.user_id)},
                tenant_id=actor.tenant_id,
                dedupe_key=f"sync:{account.id}",
                concurrency_key=f"sync:{account.id}",
            )
            job_ids.append(job.id)
        record_audit(
            session,
            tenant_id=actor.tenant_id,
            actor=actor,
            event_type="mailbox_sync_requested",
            resource_type="mailbox",
            resource_id=None,
            details={"accounts": [str(a.id) for a in accounts]},
        )
        await session.commit()
        return job_ids

    async def sync_account(self, session: AsyncSession, account_id: uuid.UUID) -> SyncResult:
        account = await session.get(ConnectedAccount, account_id)
        if account is None or account.status is not ConnectionStatus.ACTIVE:
            raise Conflict("Mailbox connection is not active.")
        checkpoint = await self._checkpoint(session, account)
        checkpoint.last_status = SyncStatus.RUNNING
        checkpoint.last_sync_started_at = utcnow()
        await session.commit()

        result = SyncResult(account_id=account.id)
        with start_span("nuvexa.mail.sync", {"nuvexa.provider": account.provider.value}):
            try:
                provider = build_mailbox_provider(account, session=session, settings=self.settings)
                try:
                    changes = await provider.fetch_changes(
                        checkpoint.delta_link,
                        max_messages=self.settings.mail_sync_max_messages,
                        initial_days=self.settings.mail_sync_initial_days,
                    )
                except IntegrationError as exc:
                    if exc.category != FailureCategory.DELTA_EXPIRED:
                        raise
                    log.info("mail_delta_expired_full_resync", account_id=str(account.id))
                    changes = await provider.fetch_changes(
                        None,
                        max_messages=self.settings.mail_sync_max_messages,
                        initial_days=self.settings.mail_sync_initial_days,
                    )
            except IntegrationError as exc:
                await session.rollback()  # expires `account`: use the plain id from here on
                await self._record_failure(session, account_id, exc)
                raise
            except Exception as exc:
                log.exception("mail_sync_unexpected_error", account_id=str(account_id))
                await session.rollback()
                generic = IntegrationError(
                    "Mailbox synchronization failed unexpectedly.", category=FailureCategory.INTERNAL
                )
                await self._record_failure(session, account_id, generic)
                raise generic from exc

        for msg in changes.upserts:
            await self._upsert(session, account, msg, result)
        if changes.removed_provider_ids:
            removed = (
                (
                    await session.execute(
                        select(EmailMessage).where(
                            EmailMessage.connected_account_id == account.id,
                            EmailMessage.provider_message_id.in_(changes.removed_provider_ids),
                            EmailMessage.deleted_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for email in removed:
                email.deleted_at = utcnow()
            result.removed = len(removed)
        result.truncated = changes.truncated
        checkpoint = await self._checkpoint(session, account)
        checkpoint.delta_link = changes.delta_link
        checkpoint.last_status = SyncStatus.SUCCEEDED
        checkpoint.last_sync_completed_at = utcnow()
        checkpoint.last_error = None
        checkpoint.messages_ingested_total += result.inserted
        record_audit(
            session,
            tenant_id=account.tenant_id,
            event_type="mailbox_synced",
            resource_type="connected_account",
            resource_id=account.id,
            details={
                "inserted": result.inserted,
                "updated": result.updated,
                "removed": result.removed,
                "truncated": result.truncated,
            },
        )
        await session.commit()

        if result.new_email_ids:
            await self._auto_analyze(session, account, result.new_email_ids)
        return result

    async def _upsert(
        self, session: AsyncSession, account: ConnectedAccount, msg: NormalizedEmail, result: SyncResult
    ) -> None:
        existing = (
            await session.execute(
                select(EmailMessage).where(
                    EmailMessage.connected_account_id == account.id,
                    EmailMessage.provider_message_id == msg.provider_message_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            email = EmailMessage(
                tenant_id=account.tenant_id,
                owner_user_id=account.user_id,
                connected_account_id=account.id,
                provider_message_id=msg.provider_message_id,
                analysis_status=EmailAnalysisStatus.NOT_ANALYZED,
                content_hash="",
            )
            apply_normalized(email, msg)
            session.add(email)
            await session.flush()
            result.inserted += 1
            result.new_email_ids.append(email.id)
            return
        if existing.change_key and existing.change_key == msg.change_key and existing.deleted_at is None:
            return  # duplicate delivery of an unchanged message
        content_changed = apply_normalized(existing, msg)
        if content_changed and existing.analysis_status is not EmailAnalysisStatus.NOT_ANALYZED:
            existing.content_changed_since_analysis = True
        result.updated += 1

    async def _checkpoint(self, session: AsyncSession, account: ConnectedAccount) -> EmailSyncCheckpoint:
        checkpoint = (
            await session.execute(
                select(EmailSyncCheckpoint).where(
                    EmailSyncCheckpoint.connected_account_id == account.id, EmailSyncCheckpoint.folder == INBOX
                )
            )
        ).scalar_one_or_none()
        if checkpoint is None:
            checkpoint = EmailSyncCheckpoint(
                tenant_id=account.tenant_id,
                connected_account_id=account.id,
                folder=INBOX,
                last_status=SyncStatus.IDLE,
                messages_ingested_total=0,
            )
            session.add(checkpoint)
            await session.flush()
        return checkpoint

    async def _record_failure(self, session: AsyncSession, account_id: uuid.UUID, exc: IntegrationError) -> None:
        account = await session.get(ConnectedAccount, account_id)
        if account is None:
            return
        checkpoint = await self._checkpoint(session, account)
        checkpoint.last_status = SyncStatus.FAILED
        checkpoint.last_error = exc.message[:1000]
        if exc.category == "authorization":
            account.status = ConnectionStatus.NEEDS_REAUTH
            account.last_error = "Microsoft 365 authorization expired or was revoked. Reconnect the mailbox."
        record_audit(
            session,
            tenant_id=account.tenant_id,
            event_type="mailbox_sync_failed",
            resource_type="connected_account",
            resource_id=account.id,
            outcome=AuditOutcome.FAILURE,
            details={"category": exc.category},
        )
        await session.commit()

    async def _auto_analyze(self, session: AsyncSession, account: ConnectedAccount, email_ids: list[uuid.UUID]) -> None:
        ts = await get_tenant_settings(session, account.tenant_id, self.settings)
        if not ts.auto_analyze_new_emails:
            return
        workflows = WorkflowService(self.settings)
        for email_id in email_ids:
            email = await session.get(EmailMessage, email_id)
            if email is not None:
                await workflows.create_run(session, email, requested_by=None, trigger=WorkflowTrigger.AUTO)
        await session.commit()
