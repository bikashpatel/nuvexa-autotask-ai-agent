"""OpenTelemetry setup. Exporting is enabled only when OTEL_ENABLED=true.

The OTLP exporter reads the standard OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_EXPORTER_OTLP_HEADERS
variables, so the same configuration targets an OTel collector, Azure Monitor (via collector) or
Langfuse's OTLP endpoint (`https://<host>/api/public/otel`).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
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
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
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
