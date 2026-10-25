"""OpenTelemetry setup (no network: exporter, processor and instrumentors are replaced)."""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app import __version__
from app.observability import tracing
from tests.conftest import make_settings


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []


@pytest.fixture
def otel(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Patch every side-effecting OpenTelemetry entry point used by configure_tracing/instrument_app."""
    rec = _Recorder()
    monkeypatch.setattr(tracing, "_configured", False)

    class FakeExporter:
        def __init__(self) -> None:
            rec.calls.append(("exporter", None))

    class FakeBatch:
        def __init__(self, exporter: Any) -> None:
            rec.calls.append(("batch", type(exporter).__name__))

        def on_start(self, *_a: Any, **_k: Any) -> None: ...

        def on_end(self, *_a: Any) -> None: ...

        def shutdown(self) -> None: ...

        def force_flush(self, *_a: Any) -> bool:
            return True

    class FakeHttpx:
        def instrument(self) -> None:
            rec.calls.append(("httpx", None))

    class FakeFastAPI:
        @staticmethod
        def instrument_app(app: Any, excluded_urls: str) -> None:
            rec.calls.append(("fastapi", excluded_urls))

    class FakeSQLAlchemy:
        def instrument(self, engine: Any) -> None:
            rec.calls.append(("sqlalchemy", engine))

    monkeypatch.setattr("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", FakeExporter)
    monkeypatch.setattr("opentelemetry.sdk.trace.export.BatchSpanProcessor", FakeBatch)
    monkeypatch.setattr("opentelemetry.instrumentation.httpx.HTTPXClientInstrumentor", FakeHttpx)
    monkeypatch.setattr("opentelemetry.instrumentation.fastapi.FastAPIInstrumentor", FakeFastAPI)
    monkeypatch.setattr("opentelemetry.instrumentation.sqlalchemy.SQLAlchemyInstrumentor", FakeSQLAlchemy)
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", lambda provider: rec.calls.append(("provider", provider)))
    return rec


def test_configure_tracing_disabled_is_a_noop(otel: _Recorder) -> None:
    tracing.configure_tracing(make_settings("sqlite+aiosqlite:///x.db", otel_enabled=False), "svc")
    assert otel.calls == []
    assert tracing._configured is False


def test_configure_tracing_enabled(otel: _Recorder) -> None:
    settings = make_settings("sqlite+aiosqlite:///x.db", otel_enabled=True, otel_service_name="nuvexa")
    tracing.configure_tracing(settings, "nuvexa-api")
    names = [name for name, _ in otel.calls]
    assert names == ["exporter", "batch", "provider", "httpx"]
    # Spans are sanitized before the OTLP exporter sees them (tests/security/test_trace_export_redaction.py).
    assert ("batch", "SanitizingSpanExporter") in otel.calls
    provider = next(value for name, value in otel.calls if name == "provider")
    assert isinstance(provider, TracerProvider)
    attrs = dict(provider.resource.attributes)
    assert attrs["service.name"] == "nuvexa-api"
    assert attrs["service.version"] == __version__
    assert attrs["deployment.environment.name"] == "test"
    assert tracing._configured is True

    # Idempotent: a second call does nothing.
    tracing.configure_tracing(settings, "other")
    assert [name for name, _ in otel.calls] == names


def test_configure_tracing_defaults_service_name(otel: _Recorder) -> None:
    tracing.configure_tracing(make_settings("sqlite+aiosqlite:///x.db", otel_enabled=True, otel_service_name="svc"))
    provider = next(value for name, value in otel.calls if name == "provider")
    assert provider.resource.attributes["service.name"] == "svc"


def test_instrument_app_disabled(otel: _Recorder) -> None:
    tracing.instrument_app(object(), object(), make_settings("sqlite+aiosqlite:///x.db", otel_enabled=False))  # type: ignore[arg-type]
    assert otel.calls == []


def test_instrument_app_enabled(otel: _Recorder) -> None:
    class Engine:
        sync_engine = "sync-engine"

    tracing.instrument_app(object(), Engine(), make_settings("sqlite+aiosqlite:///x.db", otel_enabled=True))  # type: ignore[arg-type]
    assert otel.calls == [("fastapi", "health"), ("sqlalchemy", "sync-engine")]


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "tracer", provider.get_tracer("test"))
    return exporter


def test_start_span_sets_non_null_attributes(spans: InMemorySpanExporter) -> None:
    with tracing.start_span("nuvexa.test", {"nuvexa.count": 3, "nuvexa.skip": None, "nuvexa.kind": "x"}) as span:
        assert span.is_recording()
    (finished,) = spans.get_finished_spans()
    assert finished.name == "nuvexa.test"
    assert dict(finished.attributes or {}) == {"nuvexa.count": 3, "nuvexa.kind": "x"}
    assert finished.status.status_code is StatusCode.UNSET


def test_start_span_without_attributes(spans: InMemorySpanExporter) -> None:
    with tracing.start_span("bare"):
        pass
    (finished,) = spans.get_finished_spans()
    assert not finished.attributes


def test_start_span_records_error_status_and_reraises(spans: InMemorySpanExporter) -> None:
    with pytest.raises(KeyError), tracing.start_span("failing"):
        raise KeyError("x")
    (finished,) = spans.get_finished_spans()
    assert finished.status.status_code is StatusCode.ERROR
    assert finished.status.description == "KeyError"


def test_start_span_never_exports_exception_text(spans: InMemorySpanExporter) -> None:
    # Regression: SDK defaults replaced the sanitized status with "<type>: <message>" and attached the
    # message + stack trace as an "exception" span event, exporting untrusted content.
    with pytest.raises(ValueError, match="customer"), tracing.start_span("failing"):
        raise ValueError("customer subject line and secret")
    (finished,) = spans.get_finished_spans()
    assert finished.status.description == "ValueError"
    assert not finished.events
    assert "customer" not in str(finished.to_json())


def test_start_span_works_with_default_noop_tracer() -> None:
    with tracing.start_span("noop", {"a": 1}) as span:
        span.set_attribute("b", 2)
