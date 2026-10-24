"""Graph delta client: configuration guard, SSRF guard variants, response validation, error codes, paging bounds."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import httpx
import pytest
import respx

from app.core.errors import IntegrationError
from app.domain.enums import FailureCategory
from app.integrations.http import RetryPolicy
from app.integrations.microsoft_graph import mail_client as mail_module
from app.integrations.microsoft_graph.mail_client import GraphMailboxProvider
from tests.conftest import make_settings
from tests.unit.integrations.test_graph_mail_client import DELTA, GRAPH, NEXT_1, message, provider

ENTRA = {
    "mail_mode": "graph",
    "entra_client_id": "00000000-0000-0000-0000-000000000001",
    "entra_client_secret": "not-a-real-secret",
}


@pytest.mark.parametrize(
    ("base", "ok"),
    [
        ("https://graph.microsoft.com/v1.0", True),
        ("http://localhost:8080/v1.0", True),  # local Graph emulator
        ("http://graph.microsoft.com/v1.0", False),
    ],
)
def test_graph_base_url_must_be_https(database_url, token_provider, base: str, ok: bool) -> None:
    settings = make_settings(database_url, graph_base_url=base, **ENTRA)
    if ok:
        assert GraphMailboxProvider(token_provider, settings)._base == httpx.URL(base)
    else:
        with pytest.raises(IntegrationError) as info:
            GraphMailboxProvider(token_provider, settings)
        assert info.value.category == FailureCategory.CONFIGURATION


async def test_max_messages_must_be_positive(graph_settings, token_provider) -> None:
    with pytest.raises(ValueError, match="max_messages"):
        await GraphMailboxProvider(token_provider, graph_settings).fetch_changes(None, max_messages=0, initial_days=1)
    assert token_provider.calls == []


@respx.mock
async def test_owned_http_client_is_used_and_closed(graph_settings, token_provider, sleep) -> None:
    respx.get(host="graph.microsoft.com").mock(
        return_value=httpx.Response(200, json={"value": [message("m1")], "@odata.deltaLink": DELTA})
    )
    result = await GraphMailboxProvider(token_provider, graph_settings, sleep=sleep).fetch_changes(
        None, max_messages=1, initial_days=0
    )
    assert [e.provider_message_id for e in result.upserts] == ["m1"]
    request = respx.calls.last.request
    assert "odata.maxpagesize=1" in request.headers["Prefer"]
    assert request.headers["Accept"] == "application/json"


@pytest.mark.parametrize(
    "link",
    [
        "https://graph.microsoft.com.evil.example/v1.0/me/messages/delta",  # host suffix trick
        "https://graph.microsoft.com@evil.example/v1.0/me/messages/delta",  # userinfo trick
        "https://evil.example\\@graph.microsoft.com/v1.0/me/messages/delta",  # userinfo after backslash
        "https://graph.microsoft.com:8443/v1.0/me/messages/delta",  # other port
        "ftp://graph.microsoft.com/v1.0/me/messages/delta",
        "https://graph.microsoft.com/v1.0x/me/messages/delta",  # base path prefix without "/"
        "https://graph.microsoft.com/v1.0/../beta/me/messages/delta",  # normalized to /beta
        "https://graph.microsoft.com/v1.0",  # base itself, not below it
        "//evil.example/v1.0/me",
        "/v1.0/me/messages/delta",  # relative
    ],
)
async def test_stored_links_off_the_graph_origin_force_a_resync(graph_settings, token_provider, link: str) -> None:
    with respx.mock, pytest.raises(IntegrationError) as info:  # nothing mocked: no request may be sent
        await GraphMailboxProvider(token_provider, graph_settings).fetch_changes(link, max_messages=5, initial_days=1)
    assert info.value.category == "delta_expired"
    assert token_provider.calls == []


async def test_unparseable_stored_link_forces_a_resync(graph_settings, token_provider) -> None:
    with respx.mock, pytest.raises(IntegrationError) as info:
        await GraphMailboxProvider(token_provider, graph_settings).fetch_changes(
            "https://[::1/x", max_messages=5, initial_days=1
        )
    assert info.value.category == "delta_expired"


@pytest.mark.parametrize("key", ["@odata.nextLink", "@odata.deltaLink"])
@pytest.mark.parametrize(
    "link",
    ["https://graph.microsoft.com.evil.example/v1.0/me", "https://graph.microsoft.com:444/v1.0/me", "https://[::1/x"],
)
@respx.mock
async def test_paging_links_off_origin_are_refused(
    graph_settings, token_provider, http_client, sleep, key, link
) -> None:
    route = respx.get(host="graph.microsoft.com").mock(
        return_value=httpx.Response(200, json={"value": [message("m1")], key: link})
    )
    with pytest.raises(IntegrationError) as info:
        await provider(graph_settings, token_provider, http_client, sleep).fetch_changes(
            None, max_messages=10, initial_days=1
        )
    assert info.value.category == FailureCategory.VALIDATION
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("response", "message_part"),
    [
        (httpx.Response(200, content=b"<html>proxy</html>"), "non-JSON"),
        (httpx.Response(200, json=["not", "an", "object"]), "unexpected response"),
    ],
)
@respx.mock
async def test_malformed_pages_are_transient(
    graph_settings, token_provider, http_client, sleep, response, message_part
):
    respx.get(host="graph.microsoft.com").mock(return_value=response)
    with pytest.raises(IntegrationError) as info:
        await provider(graph_settings, token_provider, http_client, sleep).fetch_changes(
            None, max_messages=10, initial_days=1
        )
    assert info.value.category == FailureCategory.TRANSIENT
    assert info.value.retryable is True
    assert message_part in info.value.message


@pytest.mark.parametrize(
    ("response", "category", "retryable", "suffix"),
    [
        (
            httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}}),
            FailureCategory.NOT_FOUND,
            False,
            ", ErrorItemNotFound",
        ),
        (
            httpx.Response(400, json={"error": {"code": "bad code with spaces!"}}),
            FailureCategory.VALIDATION,
            False,
            "HTTP 400)",
        ),
        (httpx.Response(400, json={"error": "flat string"}), FailureCategory.VALIDATION, False, "HTTP 400)"),
        (httpx.Response(400, content=b"plain"), FailureCategory.VALIDATION, False, "HTTP 400)"),
        (httpx.Response(400, json=["x"]), FailureCategory.VALIDATION, False, "HTTP 400)"),
        (httpx.Response(410), "delta_expired", False, "HTTP 410)"),
        (
            httpx.Response(400, json={"error": {"code": "SyncStateInvalid"}}),
            "delta_expired",
            False,
            ", SyncStateInvalid",
        ),
        (
            httpx.Response(500, json={"error": {"code": "generalException"}}),
            FailureCategory.TRANSIENT,
            True,
            ", generalException",
        ),
        (httpx.Response(302), FailureCategory.CONFIGURATION, False, "HTTP 302)"),
    ],
)
@respx.mock
async def test_error_status_and_code_mapping(
    graph_settings, token_provider, http_client, sleep, response, category, retryable, suffix
) -> None:
    respx.get(host="graph.microsoft.com").mock(return_value=response)
    graph = GraphMailboxProvider(
        token_provider, graph_settings, http_client=http_client, sleep=sleep, retry_policy=RetryPolicy(max_retries=1)
    )
    with pytest.raises(IntegrationError) as info:
        await graph.fetch_changes(None, max_messages=10, initial_days=1)
    assert info.value.category == category
    assert info.value.retryable is retryable
    assert info.value.status == response.status_code
    assert suffix in info.value.message
    assert "spaces!" not in info.value.message  # unsafe provider codes are not echoed


@respx.mock
async def test_page_shapes_skips_and_span_attributes(graph_settings, token_provider, http_client, sleep, monkeypatch):
    recorded: list[dict[str, Any]] = []

    class Span:
        def __init__(self, attributes: dict[str, Any]) -> None:
            self.attributes = attributes

        def set_attribute(self, key: str, value: Any) -> None:
            self.attributes[key] = value

    @contextmanager
    def fake_span(name: str, attributes: dict[str, Any] | None = None):
        attrs = {"name": name, **(attributes or {})}
        recorded.append(attrs)
        yield Span(attrs)

    monkeypatch.setattr(mail_module, "start_span", fake_span)
    respx.get(host="graph.microsoft.com").mock(
        side_effect=[
            httpx.Response(200, json={"value": {"not": "a list"}, "@odata.nextLink": NEXT_1}),
            httpx.Response(
                200,
                json={
                    "value": [
                        {"@removed": {"reason": "deleted"}},  # removal without id -> skipped
                        {"id": "", "@removed": {}},
                        message("m1"),
                        {"id": "m2", "@removed": {"reason": "changed"}},
                        message("m2", subject="re-added"),  # later entries win
                    ],
                    "@odata.deltaLink": DELTA,
                },
            ),
        ]
    )
    result = await provider(graph_settings, token_provider, http_client, sleep).fetch_changes(
        DELTA, max_messages=10, initial_days=1
    )
    assert [e.provider_message_id for e in result.upserts] == ["m1", "m2"]
    assert result.upserts[1].subject == "re-added"
    assert result.removed_provider_ids == []
    span = recorded[0]
    assert span["graph.delta.resumed"] is True
    assert (span["graph.pages"], span["graph.upserts"], span["graph.removed"], span["graph.skipped"]) == (2, 2, 0, 2)
    assert span["graph.truncated"] is False
    assert span["http.response.status_code"] == 200
    assert not any("Body of" in str(v) for v in span.values())  # no content in telemetry


@respx.mock
async def test_page_limit_truncates_and_resumes(
    graph_settings, token_provider, http_client, sleep, monkeypatch
) -> None:
    monkeypatch.setattr(mail_module, "_MAX_PAGES_PER_CALL", 2)
    pages = iter(range(10))
    respx.get(host="graph.microsoft.com").mock(
        side_effect=lambda request: httpx.Response(
            200, json={"value": [], "@odata.nextLink": f"{GRAPH}/me/messages/delta?$skiptoken={next(pages)}"}
        )
    )
    result = await provider(graph_settings, token_provider, http_client, sleep).fetch_changes(
        None, max_messages=10, initial_days=1
    )
    assert result.truncated is True
    assert result.upserts == []
    assert result.delta_link == f"{GRAPH}/me/messages/delta?$skiptoken=1"
    assert respx.calls.call_count == 2


@respx.mock
async def test_transport_failures_surface_as_integration_errors(graph_settings, token_provider, http_client, sleep):
    respx.get(host="graph.microsoft.com").mock(side_effect=httpx.ConnectError("down"))
    graph = GraphMailboxProvider(
        token_provider, graph_settings, http_client=http_client, sleep=sleep, retry_policy=RetryPolicy(max_retries=2)
    )
    with pytest.raises(IntegrationError) as info:
        await graph.fetch_changes(None, max_messages=10, initial_days=1)
    assert info.value.category == FailureCategory.TRANSIENT
    assert info.value.retryable is True
    assert len(sleep.calls) == 2


@respx.mock
async def test_token_errors_propagate_before_any_request(graph_settings, http_client, sleep) -> None:
    class Failing:
        async def get_access_token(self, scopes: tuple[str, ...]) -> str:
            raise IntegrationError("reconnect", category=FailureCategory.AUTHORIZATION)

    route = respx.get(host="graph.microsoft.com").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(IntegrationError) as info:
        await GraphMailboxProvider(Failing(), graph_settings, http_client=http_client, sleep=sleep).fetch_changes(
            None, max_messages=10, initial_days=1
        )
    assert info.value.category == FailureCategory.AUTHORIZATION
    assert not route.called
