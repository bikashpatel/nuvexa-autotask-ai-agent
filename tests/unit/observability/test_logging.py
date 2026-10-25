"""Structured logging: redaction, correlation ids, renderer configuration."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.context import set_correlation_id
from app.observability.logging import (
    _MAX_STRING,
    _SAFE_KEYS,
    NOISY_LOGGERS,
    REDACTED,
    _add_correlation_id,
    _is_sensitive_key,
    _redact_processor,
    configure_logging,
    get_logger,
    redact_text,
    redact_value,
)

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpM"


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "access_token",
        "refresh_token",
        "client_secret",
        "Client-Secret",
        "password",
        "passwd",
        "Authorization",
        "cookie",
        "set_cookie",
        "api_key",
        "API-KEY",
        "apikey",
        "code_verifier",
        "code",
        "assertion",
        "credential",
        "credentials",
        "body",
        "body_text",
        "content",
        "private_key",
        "azure_devops_pat",
    ],
)
def test_sensitive_keys(key: str) -> None:
    assert _is_sensitive_key(key)


@pytest.mark.parametrize(
    "key", [*sorted(_SAFE_KEYS), "error_code", "status", "subject_length", "message_id", "path", "user_id"]
)
def test_non_sensitive_keys(key: str) -> None:
    assert not _is_sensitive_key(key)


def test_redact_value_nested_structures() -> None:
    value = {
        "user": "u1",
        "headers": {"Authorization": "Bearer abc", "accept": "json"},
        "items": [{"password": "p", "ok": 1}, ("refresh_token", {"api_key": "k"})],
        "content_hash": "abc",
        "input_tokens": 12,
        "count": 3,
        "none": None,
    }
    out = redact_value(value)
    assert out["user"] == "u1"
    assert out["headers"] == {"Authorization": REDACTED, "accept": "json"}
    assert out["items"][0] == {"password": REDACTED, "ok": 1}
    assert out["items"][1] == ["refresh_token", {"api_key": REDACTED}]  # tuples become lists
    assert out["content_hash"] == "abc"
    assert out["input_tokens"] == 12
    assert out["count"] == 3
    assert out["none"] is None
    assert value["headers"]["Authorization"] == "Bearer abc"  # input untouched


def test_redact_value_depth_limit() -> None:
    deep: object = "leaf"
    for _ in range(10):
        deep = {"k": deep}
    out = redact_value(deep)
    cursor = out
    for _ in range(7):
        assert isinstance(cursor, dict)
        cursor = cursor["k"]
    assert cursor == "[TRUNCATED]"
    assert redact_value("x", depth=7) == "[TRUNCATED]"
    assert redact_value("x", depth=6) == "x"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Authorization: Bearer abc.DEF-123_~+/xyz==", f"Authorization: Bearer {REDACTED}"),
        ("bearer    token123", f"Bearer {REDACTED}"),
        (f"jwt={JWT} end", f"jwt={REDACTED} end"),
        ("no secrets here", "no secrets here"),
    ],
)
def test_redact_value_scrubs_bearer_and_jwt(raw: str, expected: str) -> None:
    assert redact_value(raw) == expected


def test_redact_value_truncates_long_strings() -> None:
    out = redact_value("a" * (_MAX_STRING + 50))
    assert out == "a" * _MAX_STRING + "…[truncated]"
    exact = "b" * _MAX_STRING
    assert redact_value(exact) == exact


@given(st.text(alphabet="ABCDEFabcdef0123456789-._~+/", min_size=1, max_size=60))
def test_bearer_secret_never_survives(secret: str) -> None:
    assert redact_value(f"header Bearer {secret}") == f"header Bearer {REDACTED}"


def test_redact_processor_keeps_metadata_and_scrubs_values() -> None:
    event = {
        "event": "evt",
        "level": "info",
        "timestamp": "t",
        "logger": "l",
        "token": "secret",
        "details": {"cookie": "c", "n": 1},
        "note": "Bearer abc",
    }
    out = _redact_processor(None, "info", event)
    assert out["event"] == "evt"
    assert (out["level"], out["timestamp"], out["logger"]) == ("info", "t", "l")
    assert out["token"] == REDACTED
    assert out["details"] == {"cookie": REDACTED, "n": 1}
    assert out["note"] == f"Bearer {REDACTED}"


def test_redact_processor_scrubs_tokens_in_the_message_itself() -> None:
    # Regression: the `event` text of stdlib/library log records bypassed redaction entirely.
    out = _redact_processor(None, "warning", {"event": f"call failed: Authorization: Bearer abc123 {JWT}"})
    assert "abc123" not in out["event"]
    assert JWT not in out["event"]
    assert out["event"].startswith("call failed: Authorization: Bearer [REDACTED]")


def test_add_correlation_id() -> None:
    set_correlation_id("cid-12345678")
    try:
        assert _add_correlation_id(None, "info", {"event": "e"})["correlation_id"] == "cid-12345678"
        explicit = _add_correlation_id(None, "info", {"event": "e", "correlation_id": "mine"})
        assert explicit["correlation_id"] == "mine"
    finally:
        set_correlation_id(None)
    assert "correlation_id" not in _add_correlation_id(None, "info", {"event": "e"})


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        yield
    finally:
        configure_logging("INFO", "console")
        root.handlers[:] = handlers
        root.setLevel(level)


def _json_lines(text: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def test_configure_logging_json_end_to_end(capsys: pytest.CaptureFixture[str], restore_logging: None) -> None:
    configure_logging("debug", "json")
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert len(root.handlers) == 1
    for noisy in ("httpx", "httpcore", "msal", "openai", "urllib3", "uvicorn.access"):
        assert logging.getLogger(noisy).level == logging.WARNING

    set_correlation_id("corr-abcdefgh")
    try:
        get_logger("unit.test").info("thing_happened", access_token="tok", body_text="hello", count=2)
        logging.getLogger("third.party").warning("sent Authorization: Bearer leaked-value")
    finally:
        set_correlation_id(None)
    lines = _json_lines(capsys.readouterr().out)
    first = next(line for line in lines if line["event"] == "thing_happened")
    assert first["access_token"] == REDACTED
    assert first["body_text"] == REDACTED
    assert first["count"] == 2
    assert first["level"] == "info"
    assert first["logger"] == "unit.test"
    assert first["correlation_id"] == "corr-abcdefgh"
    assert str(first["timestamp"]).endswith("Z")
    foreign = next(line for line in lines if line.get("logger") == "third.party")
    assert "leaked-value" not in str(foreign["event"])
    assert foreign["level"] == "warning"


def test_configure_logging_console(capsys: pytest.CaptureFixture[str], restore_logging: None) -> None:
    configure_logging("WARNING", "console")
    assert logging.getLogger().level == logging.WARNING
    log = get_logger("console.test")
    log.info("hidden_info_event")
    log.warning("visible_warning_event", password="pw")
    out = capsys.readouterr().out
    assert "hidden_info_event" not in out
    assert "visible_warning_event" in out
    assert "pw" not in out.replace("password", "")
    assert not _json_lines(out)


# --- Prompt 5: more credential shapes, tracebacks rendered before redaction, quiet HTTP client loggers --------

PAT52 = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrst"  # 52 base32 characters, synthetic
PAT84 = "A1" * 38 + "AZDO" + "WXYZ"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Authorization: Basic OnBhdDEyMzQ1Njc4OTA=", f"Authorization: Basic {REDACTED}"),
        ("basic   dXNlcjpwYXNzd29yZA==", f"Basic {REDACTED}"),
        ("Basic authentication failed", "Basic authentication failed"),  # prose: no digit, "+", "/" or "="
        ("key sk-proj-AbCdEfGhIjKlMnOpQrSt here", f"key {REDACTED} here"),
        ("sk-short", "sk-short"),
        (f"pat {PAT52}.", f"pat {REDACTED}."),
        (f"token={PAT84}", f"token={REDACTED}"),
        ("password=hunter2hunter2&next=1", f"password={REDACTED}&next=1"),
        ('{"client_secret": "abcd1234"}', f'{{"client_secret": "{REDACTED}"}}'),
        ("api-key: 0123456789abcdef", f"api-key: {REDACTED}"),
        ("PAT=abcd1234; other", f"PAT={REDACTED}; other"),
        ("access_token: [REDACTED]", "access_token: [REDACTED]"),  # already redacted: left alone
        ("refresh_token=abc", "refresh_token=abc"),  # values shorter than 4 characters are not secrets
    ],
)
def test_redact_text_scrubs_credential_shapes(raw: str, expected: str) -> None:
    assert redact_text(raw) == expected
    assert redact_value(raw) == expected


def test_rendered_exception_is_redacted_but_not_shortened_like_values() -> None:
    text = "Traceback ...\n" + "x" * (_MAX_STRING * 3) + "\nRuntimeError: Bearer abc123"
    out = _redact_processor(None, "error", {"event": "e", "exception": text})["exception"]
    assert out.endswith(f"RuntimeError: Bearer {REDACTED}")
    assert len(out) > _MAX_STRING
    assert _redact_processor(None, "error", {"event": "e", "exception": {"token": "t"}})["exception"] == {
        "token": REDACTED
    }


def test_exc_info_is_rendered_before_redaction(capsys: pytest.CaptureFixture[str], restore_logging: None) -> None:
    configure_logging("INFO", "json")
    try:
        raise RuntimeError(f"call failed: Authorization: Bearer leaked-value {JWT}")
    except RuntimeError:
        get_logger("unit.test").exception("boom")
    [line] = [line for line in _json_lines(capsys.readouterr().out) if line["event"] == "boom"]
    assert "RuntimeError" in str(line["exception"])
    assert "leaked-value" not in str(line["exception"])
    assert JWT not in str(line["exception"])


def test_http_client_loggers_are_quiet(restore_logging: None) -> None:
    configure_logging("DEBUG", "json")
    assert {"httpx2", "httpcore2", "httpx", "httpcore", "openai"} <= set(NOISY_LOGGERS)
    for name in NOISY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


def test_get_logger_returns_bound_logger() -> None:
    log = get_logger("x")
    assert hasattr(log, "info")
    assert hasattr(log, "bind")
