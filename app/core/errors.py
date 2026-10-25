"""Application error hierarchy. API handlers map these to the consistent error schema."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code: int = 500
    code: str = "internal_error"
    public_message: str = "An unexpected error occurred."

    def __init__(self, message: str | None = None, *, details: list[dict[str, Any]] | None = None) -> None:
        self.message = message or self.public_message
        self.details = details or []
        self.headers: dict[str, str] = {}  # extra response headers (e.g. Retry-After)
        super().__init__(self.message)


class ConfigurationError(AppError):
    status_code = 500
    code = "configuration_error"
    public_message = "The server is not configured for this operation."


class AuthenticationRequired(AppError):
    status_code = 401
    code = "authentication_required"
    public_message = "Authentication is required."


class PermissionDenied(AppError):
    status_code = 403
    code = "permission_denied"
    public_message = "You do not have permission to perform this action."


class CsrfFailed(PermissionDenied):
    code = "csrf_failed"
    public_message = "CSRF validation failed."


class NotFound(AppError):
    status_code = 404
    code = "not_found"
    public_message = "The requested resource was not found."


class Conflict(AppError):
    """State conflict: stale version, invalid transition, duplicate."""

    status_code = 409
    code = "conflict"
    public_message = "The resource is not in a state that allows this operation."


class InvalidStateTransition(Conflict):
    code = "invalid_state_transition"


class StaleVersion(Conflict):
    code = "stale_version"
    public_message = "The resource was modified. Reload and review the latest version."


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"
    public_message = "The request failed validation."


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"
    public_message = "Too many requests. Try again later."

    def __init__(self, message: str | None = None, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        if retry_after is not None:  # whole seconds (RFC 9110 delay-seconds), never 0: "retry now" would be wrong
            self.headers["Retry-After"] = str(max(1, retry_after))


class IntegrationError(AppError):
    """An external integration failed. `category` drives retry decisions."""

    status_code = 502
    code = "integration_error"
    public_message = "An external service request failed."

    def __init__(
        self,
        message: str | None = None,
        *,
        category: str = "unknown",
        retryable: bool = False,
        status: int | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.category = category
        self.retryable = retryable
        self.status = status


class IntegrationNotConfigured(IntegrationError):
    status_code = 409
    code = "integration_not_configured"
    public_message = "The integration is not configured or connected."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message, category="not_configured", retryable=False)
