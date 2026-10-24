"""Synthetic demo mailbox (`app/demo/fixtures/demo_mailbox.json`) with Graph-like delta semantics.

* `delta_link=None` starts a round and delivers the first 10 messages → cursor `demo:cursor:10`.
* Each following call delivers the next 2 messages until the fixture is exhausted; then an empty change
  set with the same cursor is returned.
* Messages are rendered as Graph `message` JSON and pass through the same normalizer as real mail.
* Timestamps are deterministic offsets relative to `now` (fixed at construction).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from functools import lru_cache
from importlib import resources
from typing import Any

from app.core.errors import IntegrationError
from app.core.timeutil import ensure_utc, utcnow
from app.integrations.microsoft_graph.normalize import normalize_graph_message
from app.schemas.email import MailChangeSet

DEMO_CURSOR_PREFIX = "demo:cursor:"
_CURSOR_RE = re.compile(r"^demo:cursor:(\d{1,4})$")
_DUE_IN = timedelta(days=10)


@lru_cache(maxsize=1)
def load_demo_fixture() -> dict[str, Any]:
    """The parsed fixture (cached; treat as read-only)."""
    text = resources.files("app.demo").joinpath("fixtures/demo_mailbox.json").read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise ValueError("demo_mailbox.json is malformed")
    return data


class DemoMailboxProvider:
    """`MailboxProvider` over the synthetic fixture. Never performs I/O beyond reading the packaged file."""

    def __init__(self, now: datetime | None = None) -> None:
        self._now = ensure_utc(now) if now is not None else utcnow().replace(second=0, microsecond=0)
        fixture = load_demo_fixture()
        self._messages: list[dict[str, Any]] = fixture["messages"]
        self._mailbox: dict[str, Any] = fixture.get("mailbox") or {}
        self._first_round = int(fixture.get("first_round_size", 10))
        self._batch = int(fixture.get("batch_size", 2))

    @property
    def total_messages(self) -> int:
        return len(self._messages)

    async def fetch_changes(self, delta_link: str | None, *, max_messages: int, initial_days: int) -> MailChangeSet:
        if max_messages < 1:
            raise ValueError("max_messages must be >= 1")
        start = 0 if delta_link is None else self._parse_cursor(delta_link)
        planned = self._first_round if delta_link is None else self._batch
        take = min(planned, max_messages)
        end = min(start + take, len(self._messages))
        upserts = [normalize_graph_message(self._render(raw)) for raw in self._messages[start:end]]
        truncated = take < planned and end < min(start + planned, len(self._messages))
        return MailChangeSet(upserts=upserts, delta_link=f"{DEMO_CURSOR_PREFIX}{end}", truncated=truncated)

    # ------------------------------------------------------------------------------------------
    def _parse_cursor(self, delta_link: str) -> int:
        match = _CURSOR_RE.match(delta_link)
        position = int(match.group(1)) if match else -1
        if not 0 <= position <= len(self._messages):
            raise IntegrationError(
                "Demo mailbox cursor is invalid; a full resync is required", category="delta_expired"
            )
        return position

    def _fill(self, text: str) -> str:
        due = self._now + _DUE_IN
        return text.replace("{{due_date}}", due.date().isoformat()).replace(
            "{{due_datetime}}", due.replace(hour=17, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        )

    @staticmethod
    def _recipient(entry: dict[str, Any]) -> dict[str, Any]:
        return {"emailAddress": {"name": entry.get("name"), "address": entry.get("address")}}

    def _render(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Fixture entry → Graph `message` JSON (as returned with `outlook.body-content-type` honoured)."""
        received = self._now - timedelta(minutes=int(raw.get("minutes_ago", 0)))
        message_id = str(raw["id"])
        to = raw.get("to") or [self._mailbox]
        attachments = raw.get("attachments") or []
        return {
            "id": message_id,
            "internetMessageId": f"<{message_id}@demo.example.com>",
            "conversationId": f"demo-conv-{raw.get('conversation') or message_id}",
            "changeKey": f"{message_id}-v1",
            "subject": self._fill(str(raw.get("subject", ""))),
            "from": self._recipient(raw["from"]),
            "sender": self._recipient(raw["from"]),
            "toRecipients": [self._recipient(r) for r in to],
            "ccRecipients": [self._recipient(r) for r in raw.get("cc") or []],
            "receivedDateTime": received.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sentDateTime": (received - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "body": {"contentType": raw.get("body_type", "text"), "content": self._fill(str(raw.get("body", "")))},
            "hasAttachments": bool(attachments),
            "attachments": [
                {
                    "name": a.get("name"),
                    "contentType": a.get("content_type"),
                    "size": a.get("size"),
                    "isInline": bool(a.get("is_inline")),
                }
                for a in attachments
            ],
            "importance": raw.get("importance", "normal"),
            "isRead": bool(raw.get("is_read", False)),
            "webLink": None,
        }
