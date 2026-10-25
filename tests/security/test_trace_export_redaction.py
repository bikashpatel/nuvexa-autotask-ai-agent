"""Trace export never carries secrets or personal data (OTEL_ENABLED=true).

The real FastAPI and httpx instrumentations record full URLs (query strings: OAuth authorization codes,
search terms, Graph delta tokens) and, for unhandled exceptions, the message and stack trace. Every span
leaves the process through `tracing.SanitizingSpanExporter`, which keeps paths, ids, status codes and the
exception type only.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.api.errors import register_error_handlers
from app.observability import tracing

AUTH_CODE = "AUTHCODE-" + "Q" * 24
DELTA_TOKEN = "DELTA-" + "Z" * 24
SEARCH = "salary review for dana"
EMAIL = "customer@example.com"
BEARER = "Bearer eyJhbGciOiJIUzI1NiJ9." + "e30" + ".signature"
NEEDLES = [AUTH_CODE, DELTA_TOKEN, SEARCH, EMAIL, BEARER, "Traceback", "boom.py"]


@pytest.fixture
def exported() -> Iterator[tuple[InMemorySpanExporter, TracerProvider]]:
    inner = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(tracing.SanitizingSpanExporter(inner)))
    yield inner, provider
    provider.shutdown()


def _app(provider: TracerProvider) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/api/v1/auth/callback")
    async def callback(code: str, state: str) -> dict[str, bool]:
        return {"ok": bool(code and state)}

    @app.get("/api/v1/emails")
    async def emails(q: str = "") -> dict[str, int]:
        return {"total": len(q)}

    @app.get("/api/v1/boom")
    async def boom() -> dict[str, bool]:
        raise RuntimeError(f"failed for {EMAIL} with {BEARER}")

    FastAPIInstrumentor.instrument_app(app, excluded_urls="health", tracer_provider=provider)
    return app


def _blob(exporter: InMemorySpanExporter) -> str:
    return "\n".join(span.to_json() for span in exporter.get_finished_spans())


async def test_server_spans_never_export_query_strings_or_exception_text(
    exported: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    exporter, provider = exported
    transport = httpx.ASGITransport(app=_app(provider), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/v1/auth/callback", params={"code": AUTH_CODE, "state": "s"})).status_code == 200
        assert (await client.get("/api/v1/emails", params={"q": SEARCH})).status_code == 200
        assert (await client.get("/api/v1/boom")).status_code == 500
    spans = exporter.get_finished_spans()
    assert spans
    blob = _blob(exporter)
    leaked = [needle for needle in NEEDLES if needle in blob]
    assert not leaked, f"exported to the trace backend: {leaked}"
    # Still useful: route, method, status and the exception type survive.
    assert "/api/v1/auth/callback" in blob
    assert "RuntimeError" in blob
    assert any(
        (s.attributes or {}).get("http.status_code") == 500
        or (s.attributes or {}).get("http.response.status_code") == 500
        for s in spans
    )


async def test_client_spans_never_export_query_strings(exported: tuple[InMemorySpanExporter, TracerProvider]) -> None:
    exporter, provider = exported
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})))
    HTTPXClientInstrumentor.instrument_client(client, tracer_provider=provider)
    try:
        url = f"https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken={DELTA_TOKEN}"
        await client.get(url)
        await client.get("https://user:pa55@dev.azure.com/org/_apis/projects?x=1#frag")
    finally:
        HTTPXClientInstrumentor.uninstrument_client(client)
        await client.aclose()
    blob = _blob(exporter)
    assert DELTA_TOKEN not in blob
    assert "pa55" not in blob
    assert "frag" not in blob
    assert "graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta" in blob  # host and path are kept


def test_configured_pipeline_sanitizes_before_the_otlp_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    # configure_tracing must wrap the real exporter, not just the test fixture.
    seen: list[object] = []

    class FakeOtlp:
        pass

    class FakeBatch:
        def __init__(self, exporter: object) -> None:
            seen.append(exporter)

        def on_start(self, *_a: object, **_k: object) -> None: ...

        def on_end(self, *_a: object) -> None: ...

        def shutdown(self) -> None: ...

        def force_flush(self, *_a: object) -> bool:
            return True

    class FakeHttpx:
        def instrument(self) -> None: ...

    monkeypatch.setattr(tracing, "_configured", False)
    monkeypatch.setattr("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", FakeOtlp)
    monkeypatch.setattr("opentelemetry.sdk.trace.export.BatchSpanProcessor", FakeBatch)
    monkeypatch.setattr("opentelemetry.instrumentation.httpx.HTTPXClientInstrumentor", FakeHttpx)
    monkeypatch.setattr("opentelemetry.trace.set_tracer_provider", lambda _p: None)
    from tests.conftest import make_settings

    tracing.configure_tracing(make_settings("sqlite+aiosqlite:///x.db", otel_enabled=True))
    [exporter] = seen
    assert isinstance(exporter, tracing.SanitizingSpanExporter)
    assert isinstance(exporter.inner, FakeOtlp)


def test_sanitize_span_drops_captured_headers_and_bodies_and_cleans_other_events() -> None:
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.trace import Status, StatusCode

    span = ReadableSpan(
        name="GET",
        attributes={
            "http.request.header.authorization": (BEARER,),
            "http.response.header.set_cookie": ("nuvexa_session=abc",),
            "url.query": f"code={AUTH_CODE}",
            "url.path": "/api/v1/emails",
            "http.status_code": 500,
        },
        events=[
            Event("note", {"url.full": f"https://h/p?code={AUTH_CODE}", "count": 2}),
            Event("exception", {"exception.message": EMAIL, "exception.stacktrace": "Traceback ..."}),
        ],
        status=Status(StatusCode.ERROR, f"RuntimeError: {EMAIL}"),
    )
    clean = tracing.sanitize_span(span)
    assert dict(clean.attributes or {}) == {"url.path": "/api/v1/emails", "http.status_code": 500}
    note, exception = clean.events
    assert dict(note.attributes or {}) == {"url.full": "https://h/p", "count": 2}
    assert dict(exception.attributes or {}) == {"exception.type": "Exception"}
    assert clean.status.description == "Exception"
    assert EMAIL not in clean.to_json()


def test_error_status_without_an_exception_event_keeps_no_description() -> None:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import Status, StatusCode

    clean = tracing.sanitize_span(ReadableSpan(name="x", status=Status(StatusCode.ERROR, f"failed for {EMAIL}")))
    assert clean.status.status_code is StatusCode.ERROR
    assert clean.status.description is None


def test_the_sanitizing_exporter_delegates_flush_and_shutdown() -> None:
    inner = InMemorySpanExporter()
    exporter = tracing.SanitizingSpanExporter(inner)
    assert exporter.force_flush(10) is True
    exporter.shutdown()
    assert inner._stopped  # the wrapped exporter was shut down
