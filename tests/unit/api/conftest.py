"""Minimal FastAPI app harness for API-layer unit tests (real database, no routers from the product)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import register_error_handlers
from app.api.middleware import CorrelationIdMiddleware, SlidingWindowRateLimiter
from app.core.config import Settings
from app.db.models import User
from app.db.session import Database
from app.services.auth_service import IssuedSession, SessionService

ClientFactory = Callable[..., httpx.AsyncClient]


@pytest.fixture
def bare_app(settings: Settings, db: Database) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.db = db
    app.state.rate_limiter = SlidingWindowRateLimiter()
    register_error_handlers(app)
    app.add_middleware(CorrelationIdMiddleware)
    return app


@pytest.fixture
async def make_client(bare_app: FastAPI) -> AsyncIterator[ClientFactory]:
    clients: list[httpx.AsyncClient] = []

    def _make(
        app: FastAPI | None = None, *, client_ip: str = "203.0.113.7", raise_app_exceptions: bool = True
    ) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(
            app=app or bare_app, client=(client_ip, 50000), raise_app_exceptions=raise_app_exceptions
        )
        client = httpx.AsyncClient(transport=transport, base_url="http://test")
        clients.append(client)
        return client

    yield _make
    for client in clients:
        await client.aclose()


async def issue_session(session: AsyncSession, settings: Settings, user: User) -> IssuedSession:
    issued = await SessionService(settings).issue(session, user, ip="203.0.113.7", user_agent="pytest")
    await session.commit()
    return issued
