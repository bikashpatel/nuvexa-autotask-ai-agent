"""Exception → error-schema mapping."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.api import errors as errors_module
from app.api.middleware import SECURITY_HEADERS
from app.core.errors import (
    AppError,
    ConfigurationError,
    Conflict,
    IntegrationError,
    NotFound,
    RateLimited,
    ValidationFailed,
)
from app.db.models import AuditEvent, Tenant
from app.db.session import Database
from app.domain.enums import AuditOutcome, UserRole
from app.observability.logging import _redact_processor
from app.services.authz import AuditedPermissionDenied
from app.services.identity import actor_from_user
from tests.conftest import UserFactory
from tests.unit.api.conftest import ClientFactory

CID = "req-errors-0001"


class Item(BaseModel):
    count: int
    name: str


@pytest.fixture
def raising_app(bare_app: FastAPI) -> FastAPI:
    holder: dict[str, Exception] = {}
    bare_app.state.raise_next = holder

    @bare_app.get("/raise")
    async def _raise() -> None:
        raise holder["exc"]

    @bare_app.post("/items")
    async def _items(item: Item) -> Item:
        return item

    @bare_app.get("/teapot")
    async def _teapot() -> None:
        raise HTTPException(status_code=418, detail="short and stout")

    @bare_app.get("/bad")
    async def _bad() -> None:
        raise HTTPException(status_code=400, detail="bad things")

    return bare_app


def set_exc(app: FastAPI, exc: Exception) -> None:
    app.state.raise_next["exc"] = exc


async def _get(make_client: ClientFactory, app: FastAPI, path: str = "/raise", **kw: Any) -> Any:
    client = make_client(app, raise_app_exceptions=False)
    return await client.get(path, headers={"x-request-id": CID}, **kw)


@pytest.mark.parametrize(
    ("exc", "status", "code", "message"),
    [
        (NotFound("Email not found."), 404, "not_found", "Email not found."),
        (Conflict("Busy.", details=[{"msg": "x"}]), 409, "conflict", "Busy."),
        (ValidationFailed(), 422, "validation_failed", "The request failed validation."),
        (RateLimited(), 429, "rate_limited", "Too many requests. Try again later."),
        (IntegrationError("Azure DevOps returned 503."), 502, "integration_error", "Azure DevOps returned 503."),
        (ConfigurationError("Entra ID is not configured."), 500, "configuration_error", "Entra ID is not configured."),
        (AppError("database password=hunter2 leaked"), 500, "internal_error", "An unexpected error occurred."),
    ],
)
async def test_app_errors(
    raising_app: FastAPI, make_client: ClientFactory, exc: AppError, status: int, code: str, message: str
) -> None:
    set_exc(raising_app, exc)
    resp = await _get(make_client, raising_app)
    assert resp.status_code == status
    assert resp.json() == {"error": {"code": code, "message": message, "correlation_id": CID, "details": exc.details}}
    assert resp.headers["x-request-id"] == CID


@pytest.mark.parametrize(("exc", "retry_after"), [(RateLimited(retry_after=7), "7"), (RateLimited(), None)])
async def test_rate_limited_responses_carry_retry_after(
    raising_app: FastAPI, make_client: ClientFactory, exc: RateLimited, retry_after: str | None
) -> None:
    set_exc(raising_app, exc)
    resp = await _get(make_client, raising_app)
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limited"
    assert resp.headers.get("retry-after") == retry_after


async def test_5xx_app_errors_are_logged(
    raising_app: FastAPI, make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged: list[dict[str, Any]] = []

    class Log:
        def error(self, event: str, **kw: Any) -> None:
            logged.append({"event": event, **kw})

    monkeypatch.setattr(errors_module, "log", Log())
    set_exc(raising_app, NotFound())
    await _get(make_client, raising_app)
    assert logged == []
    set_exc(raising_app, IntegrationError("down"))
    await _get(make_client, raising_app)
    assert logged == [{"event": "app_error", "error_code": "integration_error", "error": "down"}]
    # Regression (Prompt 5): the error code is logged under a key the redaction processor leaves readable.
    assert _redact_processor(None, "error", dict(logged[0])) == logged[0]


async def test_permission_denied_is_audited(
    raising_app: FastAPI, make_client: ClientFactory, db: Database, tenant: Tenant, make_user: UserFactory
) -> None:
    viewer = await make_user(tenant, UserRole.VIEWER)
    exc = AuditedPermissionDenied(
        "Missing permission 'action:approve'.",
        actor=actor_from_user(viewer, "203.0.113.7"),
        resource_type="action",
        resource_id="00000000-0000-0000-0000-000000000001",
        permission="action:approve",
    )
    set_exc(raising_app, exc)
    resp = await _get(make_client, raising_app)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "permission_denied"
    assert resp.json()["error"]["message"] == "Missing permission 'action:approve'."
    async with db.session() as s:
        (audit,) = (await s.execute(select(AuditEvent))).scalars().all()
    assert (audit.event_type, audit.outcome, audit.actor_user_id) == (
        "permission_denied",
        AuditOutcome.DENIED,
        viewer.id,
    )
    assert audit.correlation_id == CID
    assert audit.details == {"permission": "action:approve", "reason": "Missing permission 'action:approve'."}
    assert str(audit.action_id) == "00000000-0000-0000-0000-000000000001"


async def test_request_validation_details(raising_app: FastAPI, make_client: ClientFactory) -> None:
    client = make_client(raising_app)
    resp = await client.post("/items", json={"count": "many"}, headers={"x-request-id": CID})
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert (body["code"], body["message"], body["correlation_id"]) == (
        "validation_failed",
        "The request failed validation.",
        CID,
    )
    by_loc = {tuple(d["loc"]): d for d in body["details"]}
    assert set(by_loc) == {("body", "count"), ("body", "name")}
    assert by_loc[("body", "name")]["type"] == "missing"
    assert all(isinstance(d["msg"], str) and d["msg"] for d in body["details"])
    assert "input" not in str(body["details"])  # submitted values are not echoed back


@pytest.mark.parametrize(
    ("method", "path", "status", "code", "message"),
    [
        ("GET", "/nope", 404, "not_found", "Not Found"),
        ("DELETE", "/items", 405, "method_not_allowed", "Method Not Allowed"),
        ("GET", "/bad", 400, "bad_request", "bad things"),
        ("GET", "/teapot", 418, "http_error", "short and stout"),
    ],
)
async def test_http_exceptions(
    raising_app: FastAPI, make_client: ClientFactory, method: str, path: str, status: int, code: str, message: str
) -> None:
    resp = await make_client(raising_app).request(method, path, headers={"x-request-id": CID})
    assert resp.status_code == status
    assert resp.json() == {"error": {"code": code, "message": message, "correlation_id": CID, "details": []}}


async def test_unhandled_exception_is_generic_and_keeps_correlation_id(
    raising_app: FastAPI, make_client: ClientFactory
) -> None:
    # Regression: Starlette runs this handler after CorrelationIdMiddleware reset the context and outside its
    # header wrapper, so 500s had correlation_id=null and no x-request-id / security headers.
    set_exc(raising_app, KeyError("secret internal detail"))
    resp = await _get(make_client, raising_app)
    assert resp.status_code == 500
    assert resp.json() == {
        "error": {
            "code": "internal_error",
            "message": "An unexpected error occurred.",
            "correlation_id": CID,
            "details": [],
        }
    }
    assert "secret" not in resp.text
    assert resp.headers["x-request-id"] == CID
    for name, value in SECURITY_HEADERS:
        assert resp.headers[name.decode()] == value.decode()


async def test_unhandled_exception_without_middleware_has_no_request_id(
    settings: Any, db: Database, make_client: ClientFactory
) -> None:
    app = FastAPI()
    app.state.db = db
    errors_module.register_error_handlers(app)

    @app.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("x")

    resp = await make_client(app, raise_app_exceptions=False).get("/boom")
    assert resp.status_code == 500
    assert resp.json()["error"]["correlation_id"] is None
    assert "x-request-id" not in resp.headers
    assert resp.headers["cache-control"] == "no-store"


async def test_unhandled_exception_is_logged_with_correlation_id(
    raising_app: FastAPI, make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged: list[dict[str, Any]] = []

    class Log:
        def exception(self, event: str, **kw: Any) -> None:
            logged.append({"event": event, **kw})

    monkeypatch.setattr(errors_module, "log", Log())
    set_exc(raising_app, ValueError("x"))
    await _get(make_client, raising_app)
    assert logged == [{"event": "unhandled_error", "error_type": "ValueError", "correlation_id": CID}]


def test_body_helper_uses_context_by_default() -> None:
    from app.core.context import set_correlation_id

    set_correlation_id("ctx-body-0001")
    try:
        assert errors_module._body("c", "m")["error"]["correlation_id"] == "ctx-body-0001"  # type: ignore[index]
        assert errors_module._body("c", "m", correlation_id="explicit")["error"]["correlation_id"] == "explicit"  # type: ignore[index]
    finally:
        set_correlation_id(None)
    assert errors_module._body("c", "m", [{"a": 1}]) == {
        "error": {"code": "c", "message": "m", "correlation_id": None, "details": [{"a": 1}]}
    }
