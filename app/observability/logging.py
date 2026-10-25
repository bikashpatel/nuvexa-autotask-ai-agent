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
# `Authorization: Basic <base64>` (e.g. an Azure DevOps PAT); requires a digit, "+", "/" or "=" so that prose such
# as "basic authentication" is left alone.
_BASIC_RE = re.compile(r"(?i)\bbasic\s+(?=[A-Za-z0-9+/]*[0-9+/=])[A-Za-z0-9+/]{12,}={0,2}")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")
_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")  # OpenAI-style keys (sk-..., sk-proj-...)
# Azure DevOps personal access tokens: classic (52 lower-case base32 characters) and current (84, "AZDO" at 76).
_ADO_PAT_RE = re.compile(r"\b(?:[a-z2-7]{52}|[A-Za-z0-9]{76}AZDO[A-Za-z0-9]{4})\b")
# `name=value` / `name: value` pairs for credential-like names (headers or query strings echoed in messages).
_CREDENTIAL_PAIR_RE = re.compile(
    r"(?i)\b(pat|password|passwd|pwd|client[_-]?secret|secret|x-api-key|api[_-]?key|subscription[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|id[_-]?token)([\"']?\s*[=:]\s*[\"']?)(?!\[REDACTED\])[^\s;,&\"'<>]{4,}"
)
_MAX_STRING = 2_000
_MAX_TRACEBACK = 16_000
REDACTED = "[REDACTED]"
NOISY_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2", "msal", "openai", "urllib3", "uvicorn.access")

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
        text = redact_text(value)
        if len(text) > _MAX_STRING:
            return text[:_MAX_STRING] + "…[truncated]"
        return text
    return value


def redact_text(value: str) -> str:
    """Replace credential-shaped substrings (bearer/basic credentials, JWTs, API keys, PATs, `key=value` secrets)."""
    text = _BEARER_RE.sub(f"Bearer {REDACTED}", value)
    text = _BASIC_RE.sub(f"Basic {REDACTED}", text)
    text = _JWT_RE.sub(REDACTED, text)
    text = _API_KEY_RE.sub(REDACTED, text)
    text = _ADO_PAT_RE.sub(REDACTED, text)
    return _CREDENTIAL_PAIR_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


def _redact_traceback(value: Any) -> Any:
    """Rendered traceback: redacted like any value, but long ones keep their end (where the error is)."""
    if not isinstance(value, str):
        return redact_value(value)
    text = redact_text(value)
    return text if len(text) <= _MAX_TRACEBACK else "[truncated]…" + text[-_MAX_TRACEBACK:]


def _is_sensitive_key(key: str) -> bool:
    return key not in _SAFE_KEYS and bool(_SENSITIVE_KEY_RE.search(key))


def _redact_processor(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in list(event_dict.keys()):
        if key in {"level", "timestamp", "logger"}:
            continue
        if key == "event":  # message text (e.g. from stdlib/library loggers) is scrubbed by value, never dropped
            event_dict[key] = redact_value(event_dict[key])
            continue
        if key == "exception":  # traceback rendered by `format_exc_info` just before this processor
            event_dict[key] = _redact_traceback(event_dict[key])
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
        # Tracebacks are rendered to text *before* redaction: exception messages often carry request headers,
        # provider responses or email text, and rendering them later (in the formatter) bypassed the redactor.
        structlog.processors.format_exc_info,
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
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Keep noisy libraries quiet; never log HTTP bodies or request URLs from client libraries (`httpx2`/`httpcore2`
    # are the transports of the OpenAI SDK and log every request line at INFO).
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
