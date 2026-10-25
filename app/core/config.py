"""Process-level configuration loaded from environment variables (and an optional `.env`).

Tenant-level, user-editable configuration lives in the `app_settings` table
(see `app.schemas.settings.TenantSettings`); this module only holds deployment configuration.
"""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class LLMProvider(StrEnum):
    HEURISTIC = "heuristic"  # deterministic, offline demo/test agent
    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"


class AzureDevOpsMode(StrEnum):
    MOCK = "mock"
    PAT = "pat"
    ENTRA_DELEGATED = "entra_delegated"


class MailMode(StrEnum):
    DEMO = "demo"  # synthetic mailbox only
    GRAPH = "graph"  # Microsoft Graph (demo mailbox still available when DEMO_MODE=true)


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CsvList = Annotated[list[str], NoDecode]

# `\Z`, not `$`: `$` also matches before a trailing newline (same bug class as `safe_redirect_path`).
_ORIGIN_RE = re.compile(
    r"^https?://(localhost|\[[0-9a-fA-F:]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)(:\d{1,5})?\Z"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -------------------------------------------------------------------------
    app_env: AppEnv = AppEnv.DEVELOPMENT
    app_name: str = "NUVEXA-AutoTask-AI-Agent"
    # Public URL of the dashboard; used for post-login redirects.
    app_base_url: str = "http://localhost:5173"
    demo_mode: bool = True
    expose_api_docs: bool | None = None  # None → enabled except in production
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    log_email_content: bool = False

    # --- Database ----------------------------------------------------------------------------
    database_url: str = "postgresql+psycopg://nuvexa:nuvexa@localhost:5432/nuvexa"
    database_echo: bool = False
    database_pool_size: int = Field(default=5, ge=1, le=100)
    # PostgreSQL session limits (resource-exhaustion guard); 0 disables.
    database_statement_timeout_ms: int = Field(default=30_000, ge=0)
    database_lock_timeout_ms: int = Field(default=10_000, ge=0)
    # Least-privilege role that api/worker connect as (app/db/privileges.py). `nuvexa migrate`, connected as the
    # schema owner, grants it DML-only privileges; at startup in production a different role is reported.
    # A plain lowercase identifier (it is interpolated, quoted, into GRANT statements). Unset = no grants step.
    database_runtime_role: str | None = Field(default=None, pattern=r"^[a-z_][a-z0-9_]{0,62}$")

    # --- HTTP / session security --------------------------------------------------------------
    session_cookie_name: str = "nuvexa_session"
    csrf_cookie_name: str = "nuvexa_csrf"
    session_ttl_hours: int = Field(default=12, ge=1, le=24 * 30)
    session_idle_minutes: int = Field(default=120, ge=5)
    cookie_secure: bool | None = None  # None → secure unless development
    cors_allowed_origins: CsvList = Field(default_factory=list)
    max_request_bytes: int = Field(default=1_048_576, ge=1024)
    rate_limit_enabled: bool = True
    # Comma-separated Fernet keys; first key encrypts, all keys decrypt (rotation).
    token_encryption_keys: SecretStr | None = None
    # HMAC key for tamper-evident audit events (≥ 32 bytes). Required in production.
    audit_hmac_key: SecretStr | None = None

    # --- AI provider -------------------------------------------------------------------------
    llm_provider: LLMProvider = LLMProvider.HEURISTIC
    llm_model: str | None = None  # OpenAI model name
    llm_temperature: float | None = None  # omitted when None (some reasoning models reject it)
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_max_email_chars: int = Field(default=12_000, ge=500)
    # Overrides the per-step completion budget (reasoning models may need more).
    llm_max_completion_tokens: int | None = Field(default=None, ge=256, le=128_000)
    llm_input_cost_per_1m_tokens: float | None = None
    llm_output_cost_per_1m_tokens: float | None = None
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: SecretStr | None = None
    azure_openai_api_version: str | None = None
    azure_openai_deployment: str | None = None
    azure_openai_use_entra_id: bool = False

    # --- Microsoft Entra ID / Graph -----------------------------------------------------------
    mail_mode: MailMode = MailMode.DEMO
    entra_tenant_id: str = "organizations"
    entra_client_id: str | None = None
    entra_client_secret: SecretStr | None = None
    entra_authority_host: str = "https://login.microsoftonline.com"
    entra_allowed_tenant_ids: CsvList = Field(default_factory=list)
    oauth_redirect_uri: str = "http://localhost:5173/api/v1/auth/callback"
    bootstrap_admin_emails: CsvList = Field(default_factory=list)
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    graph_timeout_seconds: float = Field(default=30.0, gt=0)
    mail_sync_initial_days: int = Field(default=14, ge=1, le=365)
    mail_sync_max_messages: int = Field(default=200, ge=1, le=5000)
    # Per-round budgets: a round that reaches one stops after the current page and resumes there next time.
    mail_sync_round_seconds: float = Field(default=120.0, gt=0)
    mail_sync_max_body_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    mail_poll_interval_seconds: int = Field(default=0, ge=0)  # 0 = manual sync only

    # --- Azure DevOps ------------------------------------------------------------------------
    azure_devops_mode: AzureDevOpsMode = AzureDevOpsMode.MOCK
    azure_devops_base_url: str = "https://dev.azure.com"
    azure_devops_organization: str | None = None
    azure_devops_pat: SecretStr | None = None
    azure_devops_default_project: str | None = None
    azure_devops_default_work_item_type: str = "Task"
    azure_devops_default_area_path: str | None = None
    azure_devops_default_iteration_path: str | None = None
    azure_devops_api_version: str = "7.1"
    azure_devops_timeout_seconds: float = Field(default=20.0, gt=0)
    azure_devops_max_retries: int = Field(default=3, ge=0, le=6)

    # --- Worker ------------------------------------------------------------------------------
    run_embedded_worker: bool = False
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)
    worker_concurrency: int = Field(default=4, ge=1, le=64)
    job_max_attempts: int = Field(default=3, ge=1, le=20)
    job_lease_seconds: int = Field(default=300, ge=30)

    # --- Telemetry ---------------------------------------------------------------------------
    otel_enabled: bool = False
    otel_service_name: str = "nuvexa"  # processes report "<name>-api" / "<name>-worker"
    # Standard OTEL_EXPORTER_OTLP_* variables are read directly by the OpenTelemetry SDK.

    _split = field_validator(
        "cors_allowed_origins", "entra_allowed_tenant_ids", "bootstrap_admin_emails", mode="before"
    )(_split_csv)

    @field_validator("bootstrap_admin_emails")
    @classmethod
    def _lower_emails(cls, value: list[str]) -> list[str]:
        return [v.lower() for v in value]

    @field_validator("cors_allowed_origins")
    @classmethod
    def _validate_origins(cls, value: list[str]) -> list[str]:
        """Only bare `scheme://host[:port]` origins: no wildcard, `null`, path, credentials or other schemes."""
        for origin in value:
            if not _ORIGIN_RE.match(origin):
                raise ValueError(f"CORS origin {origin!r} must be an exact origin like https://app.example.com")
        return value

    @field_validator("app_base_url", "oauth_redirect_uri", "graph_base_url", "azure_devops_base_url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        HttpUrl(value)
        return value.rstrip("/")

    @model_validator(mode="after")
    def _validate_combinations(self) -> Self:
        if self.app_env is AppEnv.PRODUCTION:
            if self.audit_hmac_key is None or len(self.audit_hmac_key.get_secret_value()) < 32:
                raise ValueError("AUDIT_HMAC_KEY (at least 32 characters) is required in production")
            insecure = [o for o in self.cors_allowed_origins if not o.startswith("https://")]
            if insecure:
                raise ValueError(f"CORS origins must use https in production: {insecure}")
            if self.token_encryption_keys is None:
                raise ValueError("TOKEN_ENCRYPTION_KEYS is required in production")
            if self.cookie_secure is False:
                raise ValueError("COOKIE_SECURE cannot be false in production")
        if self.llm_provider is LLMProvider.OPENAI and not (self.openai_api_key and self.llm_model):
            raise ValueError("LLM_PROVIDER=openai requires OPENAI_API_KEY and LLM_MODEL")
        if self.llm_provider is LLMProvider.AZURE_OPENAI:
            missing = [
                name
                for name, val in (
                    ("AZURE_OPENAI_ENDPOINT", self.azure_openai_endpoint),
                    ("AZURE_OPENAI_DEPLOYMENT", self.azure_openai_deployment),
                    ("AZURE_OPENAI_API_VERSION", self.azure_openai_api_version),
                )
                if not val
            ]
            if not self.azure_openai_use_entra_id and not self.azure_openai_api_key:
                missing.append("AZURE_OPENAI_API_KEY (or AZURE_OPENAI_USE_ENTRA_ID=true)")
            if missing:
                raise ValueError(f"LLM_PROVIDER=azure_openai requires {', '.join(missing)}")
        if self.azure_devops_mode is not AzureDevOpsMode.MOCK and not self.azure_devops_organization:
            raise ValueError("AZURE_DEVOPS_ORGANIZATION is required unless AZURE_DEVOPS_MODE=mock")
        if self.azure_devops_mode is AzureDevOpsMode.PAT and not self.azure_devops_pat:
            raise ValueError("AZURE_DEVOPS_MODE=pat requires AZURE_DEVOPS_PAT")
        return self

    # --- Derived ---------------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnv.PRODUCTION

    @property
    def api_docs_enabled(self) -> bool:
        return self.expose_api_docs if self.expose_api_docs is not None else not self.is_production

    @property
    def demo_login_enabled(self) -> bool:
        return self.demo_mode and not self.is_production

    @property
    def secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.app_env is not AppEnv.DEVELOPMENT and self.app_env is not AppEnv.TEST

    @property
    def entra_configured(self) -> bool:
        return bool(self.entra_client_id and self.entra_client_secret)

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def llm_model_label(self) -> str:
        if self.llm_provider is LLMProvider.AZURE_OPENAI:
            return self.azure_openai_deployment or "unknown"
        if self.llm_provider is LLMProvider.OPENAI:
            return self.llm_model or "unknown"
        return "heuristic-rules-v1"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
