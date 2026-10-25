"""Microsoft Graph delta-query client for the signed-in user's Inbox (delegated `Mail.Read`).

* `GET {graph}/me/mailFolders/inbox/messages/delta` with `$select` + `$filter=receivedDateTime ge …` on a new
  round, then `@odata.nextLink` pages until `@odata.deltaLink`.
* `Prefer: IdType="ImmutableId"` (stable ids), `outlook.body-content-type="text"`, `odata.maxpagesize`.
* Only next/delta links on the configured Graph origin are followed (SSRF guard).
* A round stops after the current page once it reaches `max_messages`, 500 pages,
  `MAIL_SYNC_ROUND_SECONDS` or `MAIL_SYNC_MAX_BODY_BYTES`, and resumes from the pending nextLink.
* Attachment content is never requested; only `hasAttachments` is read.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.core.config import Settings
from app.core.errors import IntegrationError
from app.core.timeutil import utcnow
from app.domain.enums import FailureCategory
from app.integrations.http import RetryPolicy, SleepFn, classify_response, request_idempotent
from app.integrations.microsoft_graph.base import GRAPH_MAIL_SCOPES, AccessTokenProvider
from app.integrations.microsoft_graph.normalize import GRAPH_MESSAGE_SELECT, normalize_graph_message
from app.observability.logging import get_logger
from app.observability.tracing import start_span
from app.schemas.email import MailChangeSet, NormalizedEmail

log = get_logger(__name__)

DELTA_EXPIRED = "delta_expired"
_DELTA_EXPIRED_CODES = frozenset({"syncstatenotfound", "resyncrequired", "syncstateinvalid"})
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MAX_PAGE_SIZE = 50
_MAX_PAGES_PER_CALL = 500


class GraphMailboxProvider:
    """`MailboxProvider` backed by Microsoft Graph delta query."""

    folder = "inbox"

    def __init__(
        self,
        token_provider: AccessTokenProvider,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
        sleep: SleepFn = asyncio.sleep,
        *,
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._token_provider = token_provider
        self._settings = settings
        self._http_client = http_client
        self._sleep = sleep
        self._policy = retry_policy or RetryPolicy(max_retries=3)
        self._base = httpx.URL(settings.graph_base_url.rstrip("/"))
        if self._base.scheme != "https" and not settings.graph_base_url.startswith("http://localhost"):
            raise IntegrationError("GRAPH_BASE_URL must use https", category=FailureCategory.CONFIGURATION)

    # ------------------------------------------------------------------------------------------
    async def fetch_changes(self, delta_link: str | None, *, max_messages: int, initial_days: int) -> MailChangeSet:
        if max_messages < 1:
            raise ValueError("max_messages must be >= 1")
        resumed = delta_link is not None
        url = self._initial_url(initial_days) if delta_link is None else self._stored_link(delta_link)
        page_size = max(1, min(_MAX_PAGE_SIZE, max_messages))
        with start_span(
            "graph.mail.fetch_changes",
            {"graph.folder": self.folder, "graph.delta.resumed": resumed, "graph.max_messages": max_messages},
        ) as span:
            token = await self._token_provider.get_access_token(GRAPH_MAIL_SCOPES)
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Prefer": f'IdType="ImmutableId", outlook.body-content-type="text", odata.maxpagesize={page_size}',
            }
            # Keyed by provider id; None marks a removal. Later entries win (Graph may replay changes).
            changes: dict[str, NormalizedEmail | None] = {}
            pages = 0
            skipped = 0
            started = self._clock()
            async with self._client() as client:
                while True:
                    response = await request_idempotent(
                        client,
                        "GET",
                        url,
                        policy=self._policy,
                        sleep=self._sleep,
                        headers=headers,
                        timeout_seconds=self._settings.graph_timeout_seconds,
                        operation="graph.messages.delta",
                    )
                    span.set_attribute("http.response.status_code", response.status_code)
                    self._raise_for_error(response)
                    data = self._json(response)
                    pages += 1
                    skipped += self._collect(data.get("value"), changes)
                    final_link = data.get("@odata.deltaLink")
                    next_link = data.get("@odata.nextLink")
                    if isinstance(final_link, str) and final_link:
                        self._response_link(final_link)
                        result = self._result(changes, final_link, truncated=False)
                        break
                    if not isinstance(next_link, str) or not next_link:
                        raise IntegrationError(
                            "Microsoft Graph delta response had neither nextLink nor deltaLink",
                            category=FailureCategory.TRANSIENT,
                            retryable=True,
                        )
                    url = self._response_link(next_link)
                    if (
                        len(changes) >= max_messages
                        or pages >= _MAX_PAGES_PER_CALL
                        or self._clock() - started >= self._settings.mail_sync_round_seconds
                        or _body_bytes(changes) >= self._settings.mail_sync_max_body_bytes
                    ):
                        # Resume from the pending page next time; nothing on it has been consumed.
                        result = self._result(changes, next_link, truncated=True)
                        break
            span.set_attribute("graph.pages", pages)
            span.set_attribute("graph.upserts", len(result.upserts))
            span.set_attribute("graph.removed", len(result.removed_provider_ids))
            span.set_attribute("graph.truncated", result.truncated)
            if skipped:
                span.set_attribute("graph.skipped", skipped)
                log.warning("graph_delta_entries_skipped", count=skipped)
            return result

    # ------------------------------------------------------------------------------------------
    @asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._http_client is not None:
            yield self._http_client
            return
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self._settings.graph_timeout_seconds), follow_redirects=False
        ) as client:
            yield client

    def _initial_url(self, initial_days: int) -> httpx.URL:
        since = (utcnow() - timedelta(days=max(1, initial_days))).replace(microsecond=0)
        query = urlencode(
            {
                "$select": ",".join(GRAPH_MESSAGE_SELECT),
                "$filter": f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            },
            quote_via=quote,
            safe="$,:",
        )
        return httpx.URL(f"{self._base}/me/mailFolders/{self.folder}/messages/delta?{query}")

    def _same_origin(self, url: httpx.URL) -> bool:
        base_path = self._base.path.rstrip("/")
        return (
            url.scheme == self._base.scheme
            and url.host == self._base.host
            and url.port == self._base.port
            and not url.userinfo
            and url.path.startswith(f"{base_path}/")
        )

    def _stored_link(self, link: str) -> httpx.URL:
        """A persisted delta/next link from a previous round. Unusable links force a full resync."""
        try:
            url = httpx.URL(link)
        except httpx.InvalidURL:
            url = None
        if url is None or not self._same_origin(url):
            raise IntegrationError(
                "Stored mailbox sync cursor is not a Microsoft Graph link; a full resync is required",
                category=DELTA_EXPIRED,
            )
        return url

    def _response_link(self, link: str) -> httpx.URL:
        """A link returned by Graph. Anything off the configured Graph origin is refused (SSRF guard)."""
        try:
            url = httpx.URL(link)
        except httpx.InvalidURL as exc:
            raise IntegrationError(
                "Microsoft Graph returned an invalid paging link", category=FailureCategory.VALIDATION
            ) from exc
        if not self._same_origin(url):
            raise IntegrationError(
                "Microsoft Graph returned a paging link outside the configured Graph endpoint; refusing to follow it",
                category=FailureCategory.VALIDATION,
            )
        return url

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise IntegrationError(
                "Microsoft Graph returned a non-JSON response", category=FailureCategory.TRANSIENT, retryable=True
            ) from exc
        if not isinstance(data, dict):
            raise IntegrationError(
                "Microsoft Graph returned an unexpected response", category=FailureCategory.TRANSIENT, retryable=True
            )
        return data

    @staticmethod
    def _error_code(response: httpx.Response) -> str | None:
        try:
            data = response.json()
        except ValueError:
            return None
        error = data.get("error") if isinstance(data, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        return code if isinstance(code, str) and _SAFE_CODE_RE.match(code) else None

    def _raise_for_error(self, response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        code = self._error_code(response)
        suffix = f", {code}" if code else ""
        if status == 410 or (code is not None and code.lower() in _DELTA_EXPIRED_CODES):
            raise IntegrationError(
                f"Mailbox sync state expired (HTTP {status}{suffix}); a full resync is required",
                category=DELTA_EXPIRED,
                status=status,
            )
        if status in (401, 403):
            raise IntegrationError(
                f"Microsoft Graph rejected the mailbox credentials (HTTP {status}{suffix}); reconnect the mailbox",
                category=FailureCategory.AUTHORIZATION,
                status=status,
            )
        category, retryable = classify_response(status)
        raise IntegrationError(
            f"Microsoft Graph request failed (HTTP {status}{suffix})",
            category=category,
            retryable=retryable,
            status=status,
        )

    @staticmethod
    def _collect(value: Any, changes: dict[str, NormalizedEmail | None]) -> int:
        """Merge one page into `changes`; returns the number of unusable entries skipped."""
        if not isinstance(value, list):
            return 0
        skipped = 0
        for item in value:
            if not isinstance(item, dict):
                skipped += 1
                continue
            message_id = item.get("id")
            if "@removed" in item:
                if isinstance(message_id, str) and message_id:
                    changes.pop(message_id, None)
                    changes[message_id] = None
                else:
                    skipped += 1
                continue
            try:
                normalized = normalize_graph_message(item)
            except ValueError:
                skipped += 1
                continue
            changes.pop(normalized.provider_message_id, None)
            changes[normalized.provider_message_id] = normalized
        return skipped

    @staticmethod
    def _result(changes: dict[str, NormalizedEmail | None], link: str, *, truncated: bool) -> MailChangeSet:
        """`link` is persisted verbatim (Graph state tokens are opaque; never rewrite them)."""
        return MailChangeSet(
            upserts=[email for email in changes.values() if email is not None],
            removed_provider_ids=[pid for pid, email in changes.items() if email is None],
            delta_link=link,
            truncated=truncated,
        )


def _body_bytes(changes: dict[str, NormalizedEmail | None]) -> int:
    return sum(len(email.body_text.encode("utf-8")) for email in changes.values() if email is not None)
