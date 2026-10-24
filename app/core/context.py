"""Request/job scoped context propagated through contextvars (logs, audit, traces)."""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

_CORRELATION_ID: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def sanitize_correlation_id(value: str | None) -> str:
    """Accept a caller-provided correlation id only if it is short and safe to log."""
    if value and _CORRELATION_ID_RE.match(value):
        return value
    return new_correlation_id()


def get_correlation_id() -> str | None:
    return _CORRELATION_ID.get()


def set_correlation_id(value: str | None) -> None:
    _CORRELATION_ID.set(value)
