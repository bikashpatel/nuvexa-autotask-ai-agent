"""Structured JSON logging with correlation ids and redaction of sensitive values."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

from app.core.context import get_correlation_id

_SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|passwd|authorization|cookie|api[_-]?key|client[_-]?secret|"
    r"code[_-]?verifier|^code$|assertion|credential|body|content|private[_-]?key|pat$)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")
_MAX_STRING = 2_000
REDACTED = "[REDACTED]"

# Keys that look sensitive by pattern but are safe metadata.
_SAFE_KEYS = frozenset(
    {
        "content_hash",
        "body_preview_length",
        "body_length",
        "content_type",
        "token_usage",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "has_body",
    }
)


def redact_value(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        return {k: (REDACTED if _is_sensitive_key(str(k)) else redact_value(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(v, depth + 1) for v in value]
    if isinstance(value, str):
        text = _JWT_RE.sub(REDACTED, _BEARER_RE.sub(f"Bearer {REDACTED}", value))
        if len(text) > _MAX_STRING:
            return text[:_MAX_STRING] + "…[truncated]"
        return text
    return value


def _is_sensitive_key(key: str) -> bool:
    return key not in _SAFE_KEYS and bool(_SENSITIVE_KEY_RE.search(key))


def _redact_processor(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in list(event_dict.keys()):
        if key in {"event", "level", "timestamp", "logger"}:
            continue
        event_dict[key] = REDACTED if _is_sensitive_key(key) else redact_value(event_dict[key])
    return event_dict


def _add_correlation_id(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    cid = get_correlation_id()
    if cid and "correlation_id" not in event_dict:
        event_dict["correlation_id"] = cid
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_correlation_id,
        _redact_processor,
    ]
    renderer: Any = structlog.processors.JSONRenderer() if fmt == "json" else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Keep noisy libraries quiet; never log HTTP bodies from client libraries.
    for name in ("httpx", "httpcore", "msal", "openai", "urllib3", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
