"""CORS: only explicit, well-formed origins; never a credentialed wildcard."""

from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pydantic import ValidationError

from app.db.session import Database
from app.main import create_app
from tests.conftest import TEST_FERNET_KEY, make_settings

SQLITE = "sqlite+aiosqlite://"


@pytest.mark.parametrize(
    "origins",
    [
        "*",
        "https://app.example.com,*",
        "null",
        "https://app.example.com/path",
        "https://app.example.com/",
        "javascript:alert(1)",
        "app.example.com",
        "https://user:pass@app.example.com",
        "https://*.example.com",
    ],
)
def test_unsafe_cors_origins_are_rejected(origins: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(SQLITE, cors_allowed_origins=origins)


def test_well_formed_origins_are_accepted() -> None:
    settings = make_settings(SQLITE, cors_allowed_origins="https://app.example.com, http://localhost:5173")
    assert settings.cors_allowed_origins == ["https://app.example.com", "http://localhost:5173"]


def test_production_requires_https_origins() -> None:
    with pytest.raises(ValidationError):
        make_settings(
            SQLITE,
            app_env="production",
            demo_mode=False,
            token_encryption_keys=TEST_FERNET_KEY,
            audit_hmac_key="a" * 64,
            cors_allowed_origins="http://app.example.com",
        )


async def test_preflight_reflects_only_configured_origins(database_url: str) -> None:
    # Arrange
    settings = make_settings(database_url, cors_allowed_origins="https://app.example.com")
    app = create_app(settings, Database(settings))
    preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-csrf-token"}

    # Act
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            allowed = await client.options(
                "/api/v1/emails/sync", headers={**preflight, "Origin": "https://app.example.com"}
            )
            evil = await client.options("/api/v1/emails/sync", headers={**preflight, "Origin": "https://evil.example"})

    # Assert
    assert allowed.headers.get("access-control-allow-origin") == "https://app.example.com"
    assert allowed.headers.get("access-control-allow-credentials") == "true"
    assert "access-control-allow-origin" not in evil.headers
