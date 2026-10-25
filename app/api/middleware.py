"""Pure-ASGI middleware: correlation ids, request size limits, NUL guard, security headers."""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import unquote_to_bytes

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import sanitize_correlation_id, set_correlation_id
from app.observability.logging import get_logger
from app.services.rate_limit import RateLimitDecision, retry_after_seconds

log = get_logger("app.http")

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cache-control", b"no-store"),
]


class CorrelationIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        cid = sanitize_correlation_id(headers.get(b"x-request-id", b"").decode("latin-1") or None)
        set_correlation_id(cid)
        scope.setdefault("state", {})["correlation_id"] = cid
        started = time.perf_counter()
        status_holder: dict[str, int] = {}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                hdrs = list(message.get("headers", []))
                hdrs.append((b"x-request-id", cid.encode("latin-1")))
                existing = {k.lower() for k, _ in hdrs}
                hdrs.extend(h for h in SECURITY_HEADERS if h[0] not in existing)
                message["headers"] = hdrs
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            if not path.endswith(("/health/live", "/health/ready")):
                log.info(
                    "http_request",
                    method=scope.get("method"),
                    path=path,
                    status=status_holder.get("status"),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            set_correlation_id(None)


class RequestSizeLimitMiddleware:
    """Answers 413 for bodies over `max_bytes` before the application sees any of the body.

    The body is read (at most `max_bytes`) before the app runs and then replayed to it. Enforcing the limit
    inside the app's `receive` instead does not work for streamed (chunked) bodies: the framework's body
    parser catches whatever `receive` raises and answers "400 error parsing the body".
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await _too_large(scope, send)
            return
        chunks: list[bytes] = []
        received = 0
        terminal: Message | None = None  # e.g. http.disconnect before the body was complete
        while True:
            message = await receive()
            if message["type"] != "http.request":
                terminal = message
                break
            chunk = message.get("body", b"")
            received += len(chunk)
            if received > self.max_bytes:
                await _too_large(scope, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        replay: list[Message] = []
        if chunks or terminal is None:
            replay.append({"type": "http.request", "body": b"".join(chunks), "more_body": terminal is not None})
        if terminal is not None:
            replay.append(terminal)

        async def replaying_receive() -> Message:
            # The buffered body first, then the server's channel (disconnect detection for streaming responses).
            return replay.pop(0) if replay else await receive()

        await self.app(scope, replaying_receive, send)


class ControlCharacterGuardMiddleware:
    """Answers 422 for a NUL character in the URL path or query string before the application runs.

    PostgreSQL rejects NUL in text values, so a NUL in a filter or search parameter would otherwise end as a
    database error (500). Request bodies are checked by `RequestModel`; this covers the URL.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            loc = _nul_location(scope)
            if loc is not None:
                detail = {"loc": loc, "msg": "NUL characters are not allowed", "type": "nul_character"}
                await _error(scope, send, 422, "validation_failed", "The request failed validation.", [detail])
                return
        await self.app(scope, receive, send)


def _nul_location(scope: Scope) -> list[str] | None:
    if "\x00" in scope.get("path", ""):
        return ["path"]
    for pair in (scope.get("query_string") or b"").split(b"&"):
        key, _, value = pair.partition(b"=")
        name = unquote_to_bytes(key.replace(b"+", b" "))
        if b"\x00" in name:
            return ["query"]
        if b"\x00" in unquote_to_bytes(value.replace(b"+", b" ")):
            return ["query", name.decode("utf-8", "replace")[:64]]
    return None


async def _too_large(scope: Scope, send: Send) -> None:
    await _error(scope, send, 413, "request_too_large", "Request body is too large.", [])


async def _error(scope: Scope, send: Send, status: int, code: str, message: str, details: list[Any]) -> None:
    """The standard error envelope (app/api/errors.py) for responses sent before the application runs."""
    cid = (scope.get("state") or {}).get("correlation_id")
    body = json.dumps({"error": {"code": code, "message": message, "correlation_id": cid, "details": details}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class SlidingWindowRateLimiter:
    """In-process sliding-log limiter (RATE_LIMIT_BACKEND=memory, the default): exact, but per API process — with N
    replicas every limit is N times larger. More than one replica needs RATE_LIMIT_BACKEND=database
    (app.services.rate_limit) or a shared limit at the gateway."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        return self.check(key, limit, window_seconds).allowed

    async def acquire(self, key: str, limit: int, window_seconds: float) -> RateLimitDecision:
        return self.check(key, limit, window_seconds)

    def check(self, key: str, limit: int, window_seconds: float) -> RateLimitDecision:
        now = time.monotonic()
        bucket = self._hits[key]
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            # Admitted again once all but `limit - 1` hits are more than `window_seconds` old.
            blocking = bucket[len(bucket) - limit]
            return RateLimitDecision(allowed=False, retry_after=retry_after_seconds(blocking + window_seconds - now))
        bucket.append(now)
        if len(self._hits) > 50_000:  # bound memory under key churn
            self._hits.clear()
        return RateLimitDecision(allowed=True)


def client_ip(scope_or_request: Any) -> str | None:
    client = getattr(scope_or_request, "client", None)
    return client.host if client else None
