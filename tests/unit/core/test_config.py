"""Process configuration: validators, combinations and derived properties."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import AppEnv, AzureDevOpsMode, LLMProvider, Settings, _split_csv, get_settings
from tests.conftest import TEST_FERNET_KEY

AUDIT_KEY = "a" * 32
PROD: dict[str, Any] = {
    "app_env": AppEnv.PRODUCTION,
    "token_encryption_keys": TEST_FERNET_KEY,
    "audit_hmac_key": AUDIT_KEY,
}


def build(**values: Any) -> Settings:
    base: dict[str, Any] = {"database_url": "sqlite+aiosqlite:///./x.db"}
    base.update(values)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The developer's shell / CI may export configuration; these tests describe the code's defaults.
    for name in (
        "APP_ENV",
        "LLM_PROVIDER",
        "AZURE_DEVOPS_MODE",
        "COOKIE_SECURE",
        "EXPOSE_API_DOCS",
        "TOKEN_ENCRYPTION_KEYS",
        "AUDIT_HMAC_KEY",
        "CORS_ALLOWED_ORIGINS",
        "DEMO_MODE",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults() -> None:
    s = build()
    assert s.app_env is AppEnv.DEVELOPMENT
    assert s.llm_provider is LLMProvider.HEURISTIC
    assert s.azure_devops_mode is AzureDevOpsMode.MOCK
    assert s.cors_allowed_origins == []
    assert s.mail_poll_interval_seconds == 0
    assert s.is_production is False


# --------------------------------------------------------------------------- CSV parsing


def test_split_csv_helper() -> None:
    assert _split_csv(" a, ,b ,, c ") == ["a", "b", "c"]
    assert _split_csv("") == []
    assert _split_csv(["x"]) == ["x"]
    assert _split_csv(None) is None


def test_csv_lists_from_strings_and_lists() -> None:
    s = build(
        cors_allowed_origins="https://a.example, https://b.example",
        entra_allowed_tenant_ids=["t1", "t2"],
        bootstrap_admin_emails="Admin@Example.com , ops@EXAMPLE.org",
    )
    assert s.cors_allowed_origins == ["https://a.example", "https://b.example"]
    assert s.entra_allowed_tenant_ids == ["t1", "t2"]
    assert s.bootstrap_admin_emails == ["admin@example.com", "ops@example.org"]


def test_csv_lists_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://x.example,https://y.example")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAILS", "Boss@Example.com")
    s = build()
    assert s.cors_allowed_origins == ["https://x.example", "https://y.example"]
    assert s.bootstrap_admin_emails == ["boss@example.com"]


# --------------------------------------------------------------------------- URLs


@pytest.mark.parametrize("field", ["app_base_url", "oauth_redirect_uri", "graph_base_url", "azure_devops_base_url"])
def test_urls_are_validated_and_trailing_slash_removed(field: str) -> None:
    s = build(**{field: "https://host.example/path/"})
    assert getattr(s, field) == "https://host.example/path"
    with pytest.raises(ValidationError):
        build(**{field: "not a url"})
    with pytest.raises(ValidationError):
        build(**{field: "ftp-less-host"})


# --------------------------------------------------------------------------- production requirements


def test_production_requires_encryption_keys() -> None:
    with pytest.raises(ValidationError, match="TOKEN_ENCRYPTION_KEYS is required in production"):
        build(**{**PROD, "token_encryption_keys": None})


@pytest.mark.parametrize("key", [None, "short", "x" * 31])
def test_production_requires_a_strong_audit_key(key: str | None) -> None:
    with pytest.raises(ValidationError, match="AUDIT_HMAC_KEY"):
        build(**{**PROD, "audit_hmac_key": key})


def test_production_rejects_insecure_cookies() -> None:
    with pytest.raises(ValidationError, match="COOKIE_SECURE cannot be false in production"):
        build(**PROD, cookie_secure=False)


def test_production_requires_https_cors_origins() -> None:
    with pytest.raises(ValidationError, match="CORS origins must use https in production"):
        build(**PROD, cors_allowed_origins="https://ok.example,http://plain.example")
    assert build(**PROD, cors_allowed_origins="https://ok.example").cors_allowed_origins == ["https://ok.example"]


def test_valid_production() -> None:
    s = build(**{**PROD, "app_env": "production"})
    assert s.is_production
    assert s.secure_cookies is True
    assert s.api_docs_enabled is False
    assert s.demo_login_enabled is False


@pytest.mark.parametrize(
    "origin", ["https://app.example.com", "http://localhost:5173", "https://[::1]:8443", "http://10.0.0.1"]
)
def test_exact_cors_origins_accepted(origin: str) -> None:
    assert build(cors_allowed_origins=origin).cors_allowed_origins == [origin]


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "null",
        "https://app.example.com/",
        "https://app.example.com/path",
        "ftp://x.example",
        "https://u:p@x.example",
    ],
)
def test_wildcard_and_malformed_cors_origins_rejected(origin: str) -> None:
    with pytest.raises(ValidationError, match="must be an exact origin"):
        build(cors_allowed_origins=origin)


# --------------------------------------------------------------------------- LLM providers


def test_openai_requires_key_and_model() -> None:
    message = "LLM_PROVIDER=openai requires OPENAI_API_KEY and LLM_MODEL"
    with pytest.raises(ValidationError, match=message):
        build(llm_provider="openai")
    with pytest.raises(ValidationError, match=message):
        build(llm_provider="openai", openai_api_key="sk-test")
    with pytest.raises(ValidationError, match=message):
        build(llm_provider="openai", llm_model="gpt-test")
    s = build(llm_provider="openai", openai_api_key="sk-test", llm_model="gpt-test")
    assert isinstance(s.openai_api_key, SecretStr)
    assert s.llm_model_label == "gpt-test"


AZURE_OK = {
    "llm_provider": "azure_openai",
    "azure_openai_endpoint": "https://res.openai.azure.com",
    "azure_openai_deployment": "dep-1",
    "azure_openai_api_version": "2026-01-01",
}


@pytest.mark.parametrize(
    ("missing_field", "env_name"),
    [
        ("azure_openai_endpoint", "AZURE_OPENAI_ENDPOINT"),
        ("azure_openai_deployment", "AZURE_OPENAI_DEPLOYMENT"),
        ("azure_openai_api_version", "AZURE_OPENAI_API_VERSION"),
    ],
)
def test_azure_openai_reports_each_missing_field(missing_field: str, env_name: str) -> None:
    values = {**AZURE_OK, "azure_openai_api_key": "k"}
    values.pop(missing_field)
    with pytest.raises(ValidationError) as info:
        build(**values)
    message = str(info.value)
    assert env_name in message
    assert "AZURE_OPENAI_API_KEY" not in message


def test_azure_openai_requires_key_unless_entra() -> None:
    with pytest.raises(ValidationError, match=r"AZURE_OPENAI_API_KEY \(or AZURE_OPENAI_USE_ENTRA_ID=true\)"):
        build(**AZURE_OK)
    with_entra = build(**AZURE_OK, azure_openai_use_entra_id=True)
    assert with_entra.llm_model_label == "dep-1"
    with_key = build(**AZURE_OK, azure_openai_api_key="k")
    assert with_key.azure_openai_api_key is not None


def test_azure_openai_lists_all_missing_fields_together() -> None:
    with pytest.raises(ValidationError) as info:
        build(llm_provider="azure_openai")
    message = str(info.value)
    for name in (
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_API_KEY",
    ):
        assert name in message


def test_llm_model_label_per_provider() -> None:
    assert build().llm_model_label == "heuristic-rules-v1"
    # Bypass validation to exercise the "unknown" fallbacks of the derived label.
    openai = build().model_copy(update={"llm_provider": LLMProvider.OPENAI, "llm_model": None})
    assert openai.llm_model_label == "unknown"
    azure = build().model_copy(update={"llm_provider": LLMProvider.AZURE_OPENAI, "azure_openai_deployment": None})
    assert azure.llm_model_label == "unknown"


# --------------------------------------------------------------------------- Azure DevOps modes


@pytest.mark.parametrize("mode", ["pat", "entra_delegated"])
def test_real_ado_modes_require_organization(mode: str) -> None:
    with pytest.raises(ValidationError, match="AZURE_DEVOPS_ORGANIZATION is required unless AZURE_DEVOPS_MODE=mock"):
        build(azure_devops_mode=mode, azure_devops_pat="p")


def test_pat_mode_requires_pat() -> None:
    with pytest.raises(ValidationError, match="AZURE_DEVOPS_MODE=pat requires AZURE_DEVOPS_PAT"):
        build(azure_devops_mode="pat", azure_devops_organization="org")
    s = build(azure_devops_mode="pat", azure_devops_organization="org", azure_devops_pat="p")
    assert s.azure_devops_mode is AzureDevOpsMode.PAT


def test_entra_delegated_mode_needs_no_pat() -> None:
    s = build(azure_devops_mode="entra_delegated", azure_devops_organization="org")
    assert s.azure_devops_pat is None


# --------------------------------------------------------------------------- derived properties


@pytest.mark.parametrize(
    ("env", "cookie_secure", "expected"),
    [
        (AppEnv.DEVELOPMENT, None, False),
        (AppEnv.TEST, None, False),
        (AppEnv.DEVELOPMENT, True, True),
        (AppEnv.TEST, False, False),
    ],
)
def test_secure_cookies(env: AppEnv, cookie_secure: bool | None, expected: bool) -> None:
    assert build(app_env=env, cookie_secure=cookie_secure).secure_cookies is expected


def test_secure_cookies_default_in_production() -> None:
    assert build(**PROD).secure_cookies is True


@pytest.mark.parametrize(
    ("env", "expose", "expected"),
    [
        (AppEnv.DEVELOPMENT, None, True),
        (AppEnv.TEST, None, True),
        (AppEnv.PRODUCTION, None, False),
        (AppEnv.PRODUCTION, True, True),
        (AppEnv.DEVELOPMENT, False, False),
    ],
)
def test_api_docs_enabled(env: AppEnv, expose: bool | None, expected: bool) -> None:
    base = PROD if env is AppEnv.PRODUCTION else {"app_env": env}
    assert build(**base, expose_api_docs=expose).api_docs_enabled is expected


@pytest.mark.parametrize(("demo", "expected"), [(True, True), (False, False)])
def test_demo_login_enabled_outside_production(demo: bool, expected: bool) -> None:
    assert build(demo_mode=demo).demo_login_enabled is expected
    prod = build(**PROD, demo_mode=demo)
    assert prod.demo_login_enabled is False


def test_entra_configured() -> None:
    assert build().entra_configured is False
    assert build(entra_client_id="cid").entra_configured is False
    assert build(entra_client_id="cid", entra_client_secret="sec").entra_configured is True


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql+psycopg://u:p@h/db", True),
        ("postgresql://u:p@h/db", True),
        ("sqlite+aiosqlite:///./x.db", False),
    ],
)
def test_is_postgres(url: str, expected: bool) -> None:
    assert build(database_url=url).is_postgres is expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_pool_size", 0),
        ("session_ttl_hours", 0),
        ("max_request_bytes", 10),
        ("worker_concurrency", 0),
        ("job_lease_seconds", 5),
        ("llm_max_completion_tokens", 10),
        ("mail_poll_interval_seconds", -1),
    ],
)
def test_numeric_bounds(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        build(**{field: value})


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./cached.db")
    try:
        first = get_settings()
        assert first is get_settings()
        assert first.database_url == "sqlite+aiosqlite:///./cached.db"
    finally:
        get_settings.cache_clear()
