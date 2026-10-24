"""Contracts for mailbox access. Implementations: Graph delta client and the synthetic demo mailbox."""

from __future__ import annotations

from typing import Protocol

from app.schemas.email import MailChangeSet

GRAPH_MAIL_SCOPES: tuple[str, ...] = ("Mail.Read",)
GRAPH_LOGIN_SCOPES: tuple[str, ...] = ("User.Read",)
AZURE_DEVOPS_DELEGATED_SCOPES: tuple[str, ...] = ("499b84ac-1321-427f-aa17-267ca6975798/user_impersonation",)


class MailboxProvider(Protocol):
    """Reads changes from one authorized mailbox folder (inbox).

    * `delta_link=None` starts a new round bounded by `initial_days` / `max_messages`.
    * Must return Graph immutable ids so the same message keeps the same `provider_message_id`.
    * Must never download attachment content.
    * Raises `app.core.errors.IntegrationError` (category `authorization` for consent/token problems,
      `transient`/`rate_limited` for retryable failures, `delta_expired` when the delta token is no
      longer valid and a full resync is required).
    """

    async def fetch_changes(self, delta_link: str | None, *, max_messages: int, initial_days: int) -> MailChangeSet: ...


class AccessTokenProvider(Protocol):
    """Returns a valid delegated access token for the given scopes (refreshing silently)."""

    async def get_access_token(self, scopes: tuple[str, ...]) -> str: ...
