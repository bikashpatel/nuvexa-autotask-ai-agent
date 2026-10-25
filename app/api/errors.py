"""Map exceptions to the consistent error schema."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.middleware import SECURITY_HEADERS
from app.core.context import get_correlation_id
from app.core.errors import AppError
from app.observability.logging import get_logger
from app.services.audit import record_permission_denied
from app.services.authz import AuditedPermissionDenied

log = get_logger(__name__)


def _body(
    code: str, message: str, details: list[dict[str, object]] | None = None, correlation_id: str | None = None
) -> dict[str, object]:
    cid = correlation_id or get_correlation_id()
    return {"error": {"code": code, "message": message, "correlation_id": cid, "details": details or []}}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        if isinstance(exc, AuditedPermissionDenied):
            await record_permission_denied(request.app.state.db.session_factory, exc)
        if exc.status_code >= 500:
            # Not `code=`: the log redaction masks every key named `code` (OAuth authorization codes).
            log.error("app_error", error_code=exc.code, error=exc.message)
        message = exc.message if exc.status_code < 500 or exc.code != "internal_error" else exc.public_message
        return JSONResponse(status_code=exc.status_code, content=_body(exc.code, message, exc.details))

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": [str(p) for p in err.get("loc", ())], "msg": str(err.get("msg", "")), "type": err.get("type")}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422, content=_body("validation_failed", "The request failed validation.", details)
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed", 400: "bad_request"}.get(exc.status_code, "http_error")
        return JSONResponse(status_code=exc.status_code, content=_body(code, str(exc.detail)))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Starlette calls this from its outermost middleware, after CorrelationIdMiddleware has reset the context
        # and bypassing its header wrapper: recover the request's id from the scope it stored it in.
        cid = get_correlation_id() or getattr(request.state, "correlation_id", None)
        log.exception("unhandled_error", error_type=type(exc).__name__, correlation_id=cid)
        headers = {name.decode(): value.decode() for name, value in SECURITY_HEADERS}
        if cid:
            headers["x-request-id"] = cid
        body = _body("internal_error", "An unexpected error occurred.", correlation_id=cid)
        return JSONResponse(status_code=500, content=body, headers=headers)
