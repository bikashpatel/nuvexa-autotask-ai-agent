"""OpenTelemetry setup. Exporting is enabled only when OTEL_ENABLED=true.

The OTLP exporter reads the standard OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_EXPORTER_OTLP_HEADERS
variables, so the same configuration targets an OTel collector, Azure Monitor (via collector) or
Langfuse's OTLP endpoint (`https://<host>/api/public/otel`).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Span, Status, StatusCode

from app import __version__
from app.core.config import Settings
from app.observability.logging import get_logger

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

log = get_logger(__name__)
_configured = False

tracer = trace.get_tracer("nuvexa")


def configure_tracing(settings: Settings, service_name: str | None = None) -> None:
    global _configured  # noqa: PLW0603 — process-wide, idempotent initialization
    if _configured or not settings.otel_enabled:
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create(
        {
            "service.name": service_name or settings.otel_service_name,
            "service.version": __version__,
            "deployment.environment.name": settings.app_env.value,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(SanitizingSpanExporter(OTLPSpanExporter())))
    trace.set_tracer_provider(provider)
    # Header capture is off by default in these instrumentations, so tokens are not exported.
    HTTPXClientInstrumentor().instrument()
    _configured = True
    log.info("tracing_configured", service_name=service_name or settings.otel_service_name)


def instrument_app(app: FastAPI, engine: AsyncEngine, settings: Settings) -> None:
    if not settings.otel_enabled:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls="health")
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)


@contextmanager
def start_span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Span]:
    """Start a span; attributes must already be sanitized (ids, counts, categories — no content)."""
    # The SDK defaults would export the exception message and stack trace (possibly email content or
    # provider error text); only the exception type is recorded below.
    with tracer.start_as_current_span(name, record_exception=False, set_status_on_exception=False) as span:
        for key, value in (attributes or {}).items():
            if value is not None:
                span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


# URL-valued attributes (old and new semantic conventions) keep scheme, host and path only: query strings
# carry OAuth codes, search terms and Graph delta tokens; userinfo can carry credentials.
_URL_ATTRIBUTES = frozenset({"http.url", "url.full", "http.target"})
_DROPPED_ATTRIBUTES = frozenset({"url.query", "http.request.body", "http.response.body"})
_DROPPED_PREFIXES = ("http.request.header.", "http.response.header.")
_URL_USERINFO = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@]*@")


def _without_query(value: str) -> str:
    return _URL_USERINFO.sub(r"\1", value.split("#", 1)[0].split("?", 1)[0])


def _clean_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if key in _DROPPED_ATTRIBUTES or key.startswith(_DROPPED_PREFIXES):
            continue
        cleaned[key] = _without_query(value) if key in _URL_ATTRIBUTES and isinstance(value, str) else value
    return cleaned


def _clean_event(event: Event) -> Event:
    if event.name == "exception":  # the type only: messages and stack traces carry untrusted content
        kept = {"exception.type": (event.attributes or {}).get("exception.type", "Exception")}
        return Event("exception", kept, timestamp=event.timestamp)
    return Event(event.name, _clean_attributes(event.attributes), timestamp=event.timestamp)


def _clean_status(span: ReadableSpan, events: Sequence[Event]) -> Status:
    if span.status.status_code is not StatusCode.ERROR:
        return span.status
    types = [str((e.attributes or {}).get("exception.type")) for e in events if e.name == "exception"]
    return Status(StatusCode.ERROR, types[-1] if types else None)


def sanitize_span(span: ReadableSpan) -> ReadableSpan:
    events = [_clean_event(event) for event in span.events]
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=_clean_attributes(span.attributes),
        events=events,
        links=span.links,
        kind=span.kind,
        status=_clean_status(span, events),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class SanitizingSpanExporter(SpanExporter):
    """Exports every span (ours and the auto-instrumentations') without query strings, captured headers or
    exception text, so a trace backend never receives OAuth codes, tokens, search terms or email content."""

    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.inner.export([sanitize_span(span) for span in spans])

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self.inner.force_flush(timeout_millis)
