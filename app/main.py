"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.errors import register_error_handlers
from app.api.middleware import CorrelationIdMiddleware, RequestSizeLimitMiddleware, SlidingWindowRateLimiter
from app.api.routes import actions, auth, emails, health, integrations, misc, workflows
from app.core.config import Settings, get_settings
from app.db.session import Database
from app.observability.logging import configure_logging, get_logger
from app.observability.tracing import configure_tracing, instrument_app

API_PREFIX = "/api/v1"
log = get_logger(__name__)


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)
    configure_tracing(settings, f"{settings.otel_service_name}-api")
    db = database or Database(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        worker_task: asyncio.Task[None] | None = None
        worker_stop = None
        if settings.run_embedded_worker:
            from app.workers.runner import worker_context

            ready = asyncio.Event()

            async def _run_worker() -> None:
                nonlocal worker_stop
                async with worker_context(db, settings) as worker:
                    worker_stop = worker.stop
                    ready.set()
                    await worker.run_forever()

            worker_task = asyncio.create_task(_run_worker())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ready.wait(), timeout=30)
            log.info("embedded_worker_started")
        log.info(
            "api_started",
            version=__version__,
            env=settings.app_env.value,
            demo_mode=settings.demo_mode,
            llm_provider=settings.llm_provider.value,
            azure_devops_mode=settings.azure_devops_mode.value,
        )
        try:
            yield
        finally:
            if worker_task is not None:
                if worker_stop is not None:
                    worker_stop()
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(worker_task, timeout=35)
            await db.dispose()

    docs = settings.api_docs_enabled
    app = FastAPI(
        title="NUVEXA-AutoTask-AI-Agent API",
        version=__version__,
        description="Agentic email-to-work automation with human approval (Elarxsys).",
        lifespan=lifespan,
        docs_url=f"{API_PREFIX}/docs" if docs else None,
        redoc_url=None,
        openapi_url=f"{API_PREFIX}/openapi.json" if docs else None,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.rate_limiter = SlidingWindowRateLimiter()

    register_error_handlers(app)
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["content-type", "x-csrf-token", "x-request-id"],
        )
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(CorrelationIdMiddleware)

    api = APIRouter(prefix=API_PREFIX)
    for module in (health, auth, integrations, emails, workflows, actions, misc):
        api.include_router(module.router)
    app.include_router(api)
    instrument_app(app, db.engine, settings)
    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn --factory app.main:app_factory`."""
    return create_app()
