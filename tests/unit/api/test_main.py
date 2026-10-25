"""Application factory: routing, docs, CORS, middleware wiring, lifespan with the embedded worker."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pydantic import SecretStr
from sqlalchemy import func, select

from app import main as main_module
from app.api.middleware import SlidingWindowRateLimiter
from app.core.config import AppEnv, RateLimitBackend, Settings
from app.db.models import RateLimitCounter
from app.db.session import Database
from app.main import API_PREFIX, app_factory, create_app
from app.services.rate_limit import DatabaseRateLimiter
from tests.support.rate_limit import RecordingLog

EXPECTED_ROUTES = {
    ("GET", "/api/v1/health/live"),
    ("GET", "/api/v1/health/ready"),
    ("POST", "/api/v1/auth/demo-login"),
    ("GET", "/api/v1/auth/me"),
    ("GET", "/api/v1/emails"),
    ("POST", "/api/v1/emails/sync"),
    ("GET", "/api/v1/workflows/{run_id}"),
    ("POST", "/api/v1/actions/{action_id}/approve"),
    ("POST", "/api/v1/actions/{action_id}/execute"),
    ("GET", "/api/v1/settings"),
    ("GET", "/api/v1/integrations"),
}


def _routes(app: Any) -> set[tuple[str, str]]:
    # The OpenAPI schema lists every mounted operation regardless of how routers are included.
    return {(method.upper(), path) for path, ops in app.openapi()["paths"].items() for method in ops}


@pytest.fixture
def no_side_effects(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"logging": [], "tracing": [], "instrument": []}
    monkeypatch.setattr(main_module, "configure_logging", lambda *a: calls["logging"].append(a))
    monkeypatch.setattr(main_module, "configure_tracing", lambda *a: calls["tracing"].append(a))
    monkeypatch.setattr(main_module, "instrument_app", lambda *a: calls["instrument"].append(a))
    return calls


async def _client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def test_every_route_is_under_the_api_prefix(settings: Settings, db: Database, no_side_effects: Any) -> None:
    app = create_app(settings, db)
    routes = _routes(app)
    assert routes >= EXPECTED_ROUTES
    assert len(routes) > 30
    for _method, path in routes:
        assert path.startswith(f"{API_PREFIX}/"), path


def test_factory_wiring(settings: Settings, db: Database, no_side_effects: dict[str, list[Any]]) -> None:
    app = create_app(settings, db)
    assert app.state.settings is settings
    assert app.state.db is db
    assert isinstance(app.state.rate_limiter, SlidingWindowRateLimiter)
    assert no_side_effects["logging"] == [(settings.log_level, settings.log_format)]
    assert no_side_effects["tracing"] == [(settings, "nuvexa-api")]
    assert no_side_effects["instrument"] == [(app, db.engine, settings)]
    names = [m.cls.__name__ for m in app.user_middleware]
    # Correlation id is outermost, so the guard's and the size limit's error envelopes carry it.
    assert names == ["CorrelationIdMiddleware", "ControlCharacterGuardMiddleware", "RequestSizeLimitMiddleware"]


async def test_database_rate_limit_backend_wiring(settings: Settings, db: Database, no_side_effects: Any) -> None:
    audit_key = "k" * 32
    cfg = settings.model_copy(
        update={"rate_limit_backend": RateLimitBackend.DATABASE, "audit_hmac_key": SecretStr(audit_key)}
    )
    app = create_app(cfg, db)

    limiter = app.state.rate_limiter
    assert isinstance(limiter, DatabaseRateLimiter)
    # Bucket digests are keyed with AUDIT_HMAC_KEY, which every replica shares.
    assert limiter.digest("auth:ip:192.0.2.1", 60) == DatabaseRateLimiter(db.engine, secret=audit_key.encode()).digest(
        "auth:ip:192.0.2.1", 60
    )
    assert (await limiter.acquire("auth:ip:192.0.2.1", 1, 60)).allowed
    async with db.session() as s:  # counted in the application database
        assert (await s.execute(select(func.count()).select_from(RateLimitCounter))).scalar_one() == 1


@pytest.mark.parametrize(
    ("env", "backend", "enabled", "warned"),
    [
        (AppEnv.PRODUCTION, RateLimitBackend.MEMORY, True, True),
        (AppEnv.PRODUCTION, RateLimitBackend.DATABASE, True, False),
        (AppEnv.PRODUCTION, RateLimitBackend.MEMORY, False, False),
        (AppEnv.DEVELOPMENT, RateLimitBackend.MEMORY, True, False),
    ],
)
async def test_production_startup_warns_when_rate_limits_are_per_process(
    settings: Settings,
    db: Database,
    no_side_effects: Any,
    env: AppEnv,
    backend: RateLimitBackend,
    enabled: bool,
    warned: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = RecordingLog()
    monkeypatch.setattr(main_module, "log", log)
    cfg = settings.model_copy(update={"app_env": env, "rate_limit_backend": backend, "rate_limit_enabled": enabled})
    async with LifespanManager(create_app(cfg, db)):
        pass

    [started] = log.named("api_started")
    assert started["rate_limit_backend"] == backend.value
    warnings = log.named("rate_limits_are_per_process")
    assert len(warnings) == (1 if warned else 0)
    if warned:
        assert warnings[0]["log_level"] == "warning"
        assert "RATE_LIMIT_BACKEND=database" in warnings[0]["hint"]


async def test_middleware_is_active(settings: Settings, db: Database, no_side_effects: Any) -> None:
    app = create_app(settings.model_copy(update={"max_request_bytes": 1024}), db)
    async for client in _client(app):
        live = await client.get("/api/v1/health/live", headers={"x-request-id": "main-test-0001"})
        assert live.status_code == 200
        assert live.json()["status"] == "ok"
        assert live.headers["x-request-id"] == "main-test-0001"
        assert live.headers["x-content-type-options"] == "nosniff"
        too_big = await client.post("/api/v1/auth/demo-login", content=b"x" * 2048)
        assert too_big.status_code == 413
        assert too_big.json()["error"]["correlation_id"] == too_big.headers["x-request-id"]
        unknown = await client.get("/api/v1/does-not-exist")
        assert unknown.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize(("expose", "expected"), [(True, 200), (False, 404), (None, 200)])
async def test_api_docs_toggle(
    settings: Settings, db: Database, no_side_effects: Any, expose: bool | None, expected: int
) -> None:
    app = create_app(settings.model_copy(update={"expose_api_docs": expose}), db)
    async for client in _client(app):
        assert (await client.get("/api/v1/docs")).status_code == expected
        assert (await client.get("/api/v1/openapi.json")).status_code == expected
        assert (await client.get("/docs")).status_code == 404
    assert app.redoc_url is None


async def test_cors_only_when_origins_configured(settings: Settings, db: Database, no_side_effects: Any) -> None:
    preflight = {"Origin": "https://app.example.com", "Access-Control-Request-Method": "POST"}
    plain = create_app(settings, db)
    assert "CORSMiddleware" not in [m.cls.__name__ for m in plain.user_middleware]
    async for client in _client(plain):
        resp = await client.options("/api/v1/auth/demo-login", headers=preflight)
        assert "access-control-allow-origin" not in resp.headers

    cors = create_app(settings.model_copy(update={"cors_allowed_origins": ["https://app.example.com"]}), db)
    async for client in _client(cors):
        resp = await client.options("/api/v1/auth/demo-login", headers=preflight)
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "https://app.example.com"
        assert resp.headers["access-control-allow-credentials"] == "true"
        other = await client.options("/api/v1/auth/demo-login", headers={**preflight, "Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in other.headers


class _FakeWorker:
    def __init__(self) -> None:
        self.events: list[str] = []
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self.events.append("stop")
        self._stopped.set()

    async def run_forever(self) -> None:
        self.events.append("run")
        await self._stopped.wait()
        self.events.append("finished")


def _track_dispose(db: Database, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    disposed: list[bool] = []
    real_dispose = db.dispose

    async def dispose() -> None:
        disposed.append(True)
        await real_dispose()

    monkeypatch.setattr(db, "dispose", dispose)
    return disposed


async def test_lifespan_runs_and_stops_the_embedded_worker(
    settings: Settings, db: Database, no_side_effects: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _FakeWorker()
    seen: list[tuple[Any, Any]] = []

    @contextlib.asynccontextmanager
    async def fake_context(database: Database, cfg: Settings) -> AsyncIterator[_FakeWorker]:
        seen.append((database, cfg))
        worker.events.append("enter")
        try:
            yield worker
        finally:
            worker.events.append("exit")

    monkeypatch.setattr("app.workers.runner.worker_context", fake_context)
    disposed = _track_dispose(db, monkeypatch)
    cfg = settings.model_copy(update={"run_embedded_worker": True})
    app = create_app(cfg, db)
    async with LifespanManager(app):
        await asyncio.sleep(0)
        assert worker.events == ["enter", "run"]
        assert seen == [(db, cfg)]
    assert worker.events == ["enter", "run", "stop", "finished", "exit"]
    assert disposed == [True]


async def test_lifespan_without_worker_only_disposes(
    settings: Settings, db: Database, no_side_effects: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_a: Any) -> None:
        raise AssertionError("worker must not start")

    monkeypatch.setattr("app.workers.runner.worker_context", explode)
    disposed = _track_dispose(db, monkeypatch)
    async with LifespanManager(create_app(settings, db)):
        pass
    assert disposed == [True]


async def test_lifespan_checks_the_database_privileges_before_serving(
    settings: Settings, db: Database, no_side_effects: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks: list[tuple[Any, Settings, str]] = []

    async def check(engine: Any, cfg: Settings, *, process: str) -> list[str]:
        checks.append((engine, cfg, process))
        return ["superuser"]  # findings are only logged (warning); the API still starts

    monkeypatch.setattr(main_module, "check_runtime_privileges", check)
    app = create_app(settings, db)
    assert checks == []
    async for client in _client(app):
        assert checks == [(db.engine, settings, "api")]
        assert (await client.get(f"{API_PREFIX}/health/live")).status_code == 200
    assert len(checks) == 1


class _FastAsyncio:
    """asyncio proxy for app.main whose wait_for timeouts are capped (startup/shutdown waits of 30 s+)."""

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    @staticmethod
    async def wait_for(aw: Any, timeout: float) -> Any:  # noqa: ASYNC109 - mirrors asyncio.wait_for
        return await asyncio.wait_for(aw, timeout=min(timeout, 0.05))


async def test_lifespan_tolerates_a_worker_that_never_becomes_ready(
    settings: Settings, db: Database, no_side_effects: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()

    @contextlib.asynccontextmanager
    async def hanging_context(database: Database, cfg: Settings) -> AsyncIterator[_FakeWorker]:
        entered.set()
        await asyncio.Event().wait()  # e.g. the checkpointer cannot connect
        yield _FakeWorker()  # pragma: no cover

    monkeypatch.setattr("app.workers.runner.worker_context", hanging_context)
    monkeypatch.setattr(main_module, "asyncio", _FastAsyncio())
    disposed = _track_dispose(db, monkeypatch)
    app = create_app(settings.model_copy(update={"run_embedded_worker": True}), db)
    async with LifespanManager(app):
        assert entered.is_set()  # the API still starts after the readiness timeout
    assert disposed == [True]  # shutdown did not hang: the bounded wait cancelled the worker task


def test_app_factory_uses_process_settings(
    settings: Settings, no_side_effects: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    app = app_factory()
    try:
        assert app.state.settings is settings
        assert isinstance(app.state.db, Database)
        assert app.title == "NUVEXA-AutoTask-AI-Agent API"
    finally:
        app.state.db.engine.sync_engine.dispose()


def test_create_app_defaults(settings: Settings, no_side_effects: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    app = create_app()
    try:
        assert app.state.settings is settings
        assert app.state.db.settings is settings
    finally:
        app.state.db.engine.sync_engine.dispose()
