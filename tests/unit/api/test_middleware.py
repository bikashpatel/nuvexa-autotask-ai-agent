"""Pure-ASGI middleware: correlation ids, security headers, request size limits, rate limiter."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.api import middleware
from app.api.middleware import (
    SECURITY_HEADERS,
    CorrelationIdMiddleware,
    RequestSizeLimitMiddleware,
    SlidingWindowRateLimiter,
    client_ip,
)
from app.core.context import get_correlation_id

Message = dict[str, Any]
App = Callable[
    [dict[str, Any], Callable[[], Awaitable[Message]], Callable[[Message], Awaitable[None]]], Awaitable[None]
]
HEX32 = re.compile(r"^[0-9a-f]{32}$")


def http_scope(
    path: str = "/api/v1/x", headers: list[tuple[bytes, bytes]] | None = None, **extra: Any
) -> dict[str, Any]:
    return {"type": "http", "method": "POST", "path": path, "headers": headers or [], **extra}


def body_receiver(*chunks: bytes) -> Callable[[], Awaitable[Message]]:
    messages = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    messages = messages or [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive() -> Message:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    return receive


class Sent:
    def __init__(self) -> None:
        self.messages: list[Message] = []

    async def __call__(self, message: Message) -> None:
        self.messages.append(message)

    @property
    def start(self) -> Message:
        return next(m for m in self.messages if m["type"] == "http.response.start")

    @property
    def headers(self) -> list[tuple[bytes, bytes]]:
        return list(self.start["headers"])

    def header(self, name: bytes) -> list[bytes]:
        return [v for k, v in self.headers if k.lower() == name]

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.messages if m["type"] == "http.response.body")


def respond(
    status: int = 200, headers: list[tuple[bytes, bytes]] | None = None, seen: dict[str, Any] | None = None
) -> App:
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if seen is not None:
            seen["cid"] = get_correlation_id()
            seen["state"] = dict(scope.get("state") or {})
        await send({"type": "http.response.start", "status": status, "headers": headers or []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app


# --------------------------------------------------------------------------- correlation id


async def test_valid_request_id_is_propagated() -> None:
    seen: dict[str, Any] = {}
    sent = Sent()
    scope = http_scope(headers=[(b"x-request-id", b"client-req-0001")])
    await CorrelationIdMiddleware(respond(seen=seen))(scope, body_receiver(), sent)
    assert seen["cid"] == "client-req-0001"
    assert seen["state"]["correlation_id"] == "client-req-0001"
    assert sent.header(b"x-request-id") == [b"client-req-0001"]
    assert get_correlation_id() is None  # reset after the request


@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.binary(max_size=200))
async def test_request_id_header_values(raw: bytes) -> None:
    seen: dict[str, Any] = {}
    sent = Sent()
    await CorrelationIdMiddleware(respond(seen=seen))(
        http_scope(headers=[(b"x-request-id", raw)]), body_receiver(), sent
    )
    (echoed,) = sent.header(b"x-request-id")
    value = raw.decode("latin-1")
    if re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", value):
        assert echoed == raw
    else:
        assert HEX32.match(echoed.decode())  # replaced, never reflected
    assert seen["cid"] == echoed.decode()


async def test_missing_request_id_gets_a_fresh_one() -> None:
    sent = Sent()
    await CorrelationIdMiddleware(respond())(http_scope(), body_receiver(), sent)
    (cid,) = sent.header(b"x-request-id")
    assert HEX32.match(cid.decode())


async def test_security_headers_added_once_and_app_values_win() -> None:
    sent = Sent()
    app_headers = [(b"X-Frame-Options", b"SAMEORIGIN"), (b"content-type", b"text/plain")]
    await CorrelationIdMiddleware(respond(headers=app_headers))(http_scope(), body_receiver(), sent)
    names = [k.lower() for k, _ in sent.headers]
    for name, value in SECURITY_HEADERS:
        assert names.count(name) == 1, name
        if name != b"x-frame-options":
            assert sent.header(name) == [value]
    assert sent.header(b"x-frame-options") == [b"SAMEORIGIN"]
    assert sent.body == b"ok"


async def test_non_http_scopes_pass_through_untouched() -> None:
    calls: list[tuple[Any, Any, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        calls.append((scope, receive, send))

    scope, receive, send = {"type": "lifespan"}, body_receiver(), Sent()
    await CorrelationIdMiddleware(app)(scope, receive, send)
    await RequestSizeLimitMiddleware(app, max_bytes=1)(scope, receive, send)
    assert calls == [(scope, receive, send), (scope, receive, send)]
    assert "state" not in scope


@pytest.fixture
def http_log(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    monkeypatch.setattr(
        middleware, "log", SimpleNamespace(info=lambda event, **kw: records.append({"event": event, **kw}))
    )
    return records


async def test_requests_are_logged_except_health_probes(http_log: list[dict[str, Any]]) -> None:
    await CorrelationIdMiddleware(respond(status=201))(http_scope("/api/v1/emails"), body_receiver(), Sent())
    for probe in ("/api/v1/health/live", "/api/v1/health/ready"):
        await CorrelationIdMiddleware(respond())(http_scope(probe), body_receiver(), Sent())
    (record,) = http_log
    assert record["event"] == "http_request"
    assert (record["method"], record["path"], record["status"]) == ("POST", "/api/v1/emails", 201)
    assert isinstance(record["duration_ms"], int)


async def test_context_is_reset_and_logged_when_the_app_raises(http_log: list[dict[str, Any]]) -> None:
    async def failing(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await CorrelationIdMiddleware(failing)(http_scope(), body_receiver(), Sent())
    assert get_correlation_id() is None
    assert http_log[0]["status"] is None


# --------------------------------------------------------------------------- request size


def reader(seen: dict[str, Any]) -> App:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        seen["body"] = body
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"read"})

    return app


def assert_413(sent: Sent, cid: str | None) -> None:
    assert sent.start["status"] == 413
    assert (b"content-type", b"application/json") in sent.headers
    assert sent.header(b"content-length") == [str(len(sent.body)).encode()]
    assert json.loads(sent.body) == {
        "error": {
            "code": "request_too_large",
            "message": "Request body is too large.",
            "correlation_id": cid,
            "details": [],
        }
    }


async def test_declared_content_length_over_limit_is_rejected_without_reading() -> None:
    seen: dict[str, Any] = {}
    sent = Sent()
    scope = http_scope(headers=[(b"content-length", b"2048")], state={"correlation_id": "cid-abcdefgh"})
    await RequestSizeLimitMiddleware(reader(seen), max_bytes=1024)(scope, body_receiver(b"x"), sent)
    assert seen == {}
    assert_413(sent, "cid-abcdefgh")


@pytest.mark.parametrize("declared", [b"1024", b"10", b"not-a-number", b""])
async def test_bodies_within_limit_pass(declared: bytes) -> None:
    seen: dict[str, Any] = {}
    sent = Sent()
    scope = http_scope(headers=[(b"content-length", declared)])
    await RequestSizeLimitMiddleware(reader(seen), max_bytes=1024)(scope, body_receiver(b"a" * 512, b"b" * 512), sent)
    assert seen["body"] == b"a" * 512 + b"b" * 512
    assert sent.start["status"] == 200


async def test_streamed_body_over_limit_is_rejected() -> None:
    seen: dict[str, Any] = {}
    sent = Sent()
    # Chunked upload: no content-length, the limit is enforced while the app reads.
    await RequestSizeLimitMiddleware(reader(seen), max_bytes=1024)(
        http_scope(), body_receiver(b"a" * 600, b"b" * 600, b"c"), sent
    )
    assert "body" not in seen
    assert_413(sent, None)


async def test_lying_content_length_is_still_enforced() -> None:
    seen: dict[str, Any] = {}
    sent = Sent()
    scope = http_scope(headers=[(b"content-length", b"10")])
    await RequestSizeLimitMiddleware(reader(seen), max_bytes=1024)(scope, body_receiver(b"x" * 2000), sent)
    assert_413(sent, None)


async def test_non_body_messages_are_not_counted() -> None:
    seen: dict[str, Any] = {}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen["first"] = await receive()
        await send({"type": "http.response.start", "status": 204, "headers": []})

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    await RequestSizeLimitMiddleware(app, max_bytes=1024)(http_scope(), receive, Sent())
    assert seen["first"] == {"type": "http.disconnect"}


# --------------------------------------------------------------------------- rate limiter


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(middleware.time, "monotonic", lambda: now[0])
    return now


def test_sliding_window(clock: list[float]) -> None:
    limiter = SlidingWindowRateLimiter()
    assert limiter.allow("k", 2, 10)
    clock[0] += 1
    assert limiter.allow("k", 2, 10)
    clock[0] += 1
    assert not limiter.allow("k", 2, 10)  # 2 hits in the last 10 s
    assert limiter.allow("other", 2, 10)  # keys are independent
    clock[0] = 1010.0
    assert not limiter.allow("k", 2, 10)  # first hit exactly 10 s ago still counts
    clock[0] = 1010.5
    assert limiter.allow("k", 2, 10)  # first hit expired
    assert not limiter.allow("k", 2, 10)
    clock[0] = 1100.0
    assert limiter.allow("k", 2, 10)


def test_denied_requests_do_not_extend_the_window(clock: list[float]) -> None:
    limiter = SlidingWindowRateLimiter()
    assert limiter.allow("k", 1, 5)
    for _ in range(10):
        clock[0] += 0.4
        assert not limiter.allow("k", 1, 5)
    clock[0] = 1005.1
    assert limiter.allow("k", 1, 5)


def test_memory_is_bounded_under_key_churn(clock: list[float]) -> None:
    limiter = SlidingWindowRateLimiter()
    for i in range(50_000):
        limiter.allow(f"k{i}", 1, 60)
    assert len(limiter._hits) == 50_000
    assert not limiter.allow("k0", 1, 60)
    assert limiter.allow("one-more", 1, 60)  # 50_001st key → table cleared
    assert len(limiter._hits) == 0
    assert limiter.allow("k0", 1, 60)


def test_client_ip() -> None:
    assert client_ip(SimpleNamespace(client=SimpleNamespace(host="10.1.2.3"))) == "10.1.2.3"
    assert client_ip(SimpleNamespace(client=None)) is None
    assert client_ip(object()) is None
