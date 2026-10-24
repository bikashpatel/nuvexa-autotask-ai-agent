from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from app.core.errors import IntegrationError
from app.core.timeutil import utcnow
from app.domain.enums import FailureCategory
from app.integrations.microsoft_graph.base import GRAPH_MAIL_SCOPES
from app.integrations.microsoft_graph.mail_client import GraphMailboxProvider
from tests.unit.integrations.conftest import FAKE_ACCESS_TOKEN

GRAPH = "https://graph.microsoft.com/v1.0"
INITIAL = f"{GRAPH}/me/mailFolders/inbox/messages/delta"
NEXT_1 = f"{GRAPH}/me/mailfolders('AQMkInbox')/messages/delta?$skiptoken=page-2"
NEXT_2 = f"{GRAPH}/me/mailfolders('AQMkInbox')/messages/delta?$skiptoken=page-3"
DELTA = f"{GRAPH}/me/mailfolders('AQMkInbox')/messages/delta?$deltatoken=round-1"


def message(mid: str, subject: str = "Hello", **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": mid,
        "subject": subject,
        "receivedDateTime": "2026-10-20T08:30:00Z",
        "from": {"emailAddress": {"name": "Ada Example", "address": "ada@example.com"}},
        "body": {"contentType": "text", "content": f"Body of {mid}"},
        "isRead": False,
    }
    data.update(extra)
    return data


def provider(settings: Any, token_provider: Any, http_client: Any, sleep: Any) -> GraphMailboxProvider:
    return GraphMailboxProvider(token_provider, settings, http_client=http_client, sleep=sleep)


@respx.mock
async def test_delta_round_follows_next_links_until_delta_link(graph_settings, token_provider, http_client, sleep):
    route = respx.get(host="graph.microsoft.com").mock(
        side_effect=[
            httpx.Response(200, json={"value": [message("m1"), message("m2")], "@odata.nextLink": NEXT_1}),
            httpx.Response(
                200,
                json={
                    "value": [message("m3"), {"id": "m-gone", "@removed": {"reason": "deleted"}}],
                    "@odata.deltaLink": DELTA,
                },
            ),
        ]
    )
    result = await provider(graph_settings, token_provider, http_client, sleep).fetch_changes(
        None, max_messages=100, initial_days=14
    )

    assert [e.provider_message_id for e in result.upserts] == ["m1", "m2", "m3"]
    assert result.removed_provider_ids == ["m-gone"]
    assert result.delta_link == DELTA
    assert result.truncated is False
    assert token_provider.calls == [GRAPH_MAIL_SCOPES]

    first, second = (call.request for call in route.calls)
    assert str(first.url).startswith(INITIAL)
    select = first.url.params["$select"].split(",")
    assert {"subject", "body", "from", "receivedDateTime", "hasAttachments"} <= set(select)
    assert "attachments" not in select
    flt = first.url.params["$filter"]
    assert flt.startswith("receivedDateTime ge ")
    since = flt.removeprefix("receivedDateTime ge ")
    assert since.endswith("Z")
    assert abs((utcnow() - timedelta(days=14) - datetime.fromisoformat(since)).total_seconds()) < 120
    assert str(second.url) == NEXT_1
    for request in (first, second):
        assert request.headers["Authorization"] == f"Bearer {FAKE_ACCESS_TOKEN}"
        prefer = request.headers["Prefer"]
        assert 'IdType="ImmutableId"' in prefer
        assert 'outlook.body-content-type="text"' in prefer
        assert "odata.maxpagesize=50" in prefer


@respx.mock
async def test_removed_after_upsert_in_same_round_wins(graph_settings, token_provider, http_client, sleep):
    respx.get(host="graph.microsoft.com").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [message("m1"), message("m2"), {"id": "m1", "@removed": {"reason": "deleted"}}],
                "@odata.deltaLink": DELTA,
            },
        )
    )
    result = await provider(graph_settings, token_provider, http_client, sleep).fetch_changes(
        None, max_messages=10, initial_days=1
    )
    assert [e.provider_message_id for e in result.upserts] == ["m2"]
    assert result.removed_provider_ids == ["m1"]


@respx.mock
async def test_truncates_at_max_messages_and_resumes_from_pending_next_link(
    graph_settings, token_provider, http_client, sleep
):
    route = respx.get(host="graph.microsoft.com").mock(
        side_effect=[
            httpx.Response(200, json={"value": [message("m1"), message("m2")], "@odata.nextLink": NEXT_1}),
            httpx.Response(200, json={"value": [message("m3")], "@odata.nextLink": NEXT_2}),
            httpx.Response(200, json={"value": [], "@odata.deltaLink": DELTA}),
        ]
    )
    graph = provider(graph_settings, token_provider, http_client, sleep)
    first = await graph.fetch_changes(None, max_messages=2, initial_days=7)
    assert first.truncated is True
    assert first.delta_link == NEXT_1
    assert [e.provider_message_id for e in first.upserts] == ["m1", "m2"]
    assert "odata.maxpagesize=2" in route.calls[0].request.headers["Prefer"]
    assert route.call_count == 1

    second = await graph.fetch_changes(first.delta_link, max_messages=2, initial_days=7)
    assert second.truncated is False
    assert second.delta_link == DELTA
    assert [e.provider_message_id for e in second.upserts] == ["m3"]
    assert [str(c.request.url) for c in route.calls[1:]] == [NEXT_1, NEXT_2]


@respx.mock
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (410, {"error": {"code": "SyncStateNotFound", "message": "gone"}}),
        (400, {"error": {"code": "syncStateNotFound", "message": "The sync state is invalid"}}),
        (400, {"error": {"code": "resyncRequired", "message": "resync"}}),
    ],
)
async def test_expired_delta_token_maps_to_delta_expired(
    graph_settings, token_provider, http_client, sleep, status, body
):
    respx.get(host="graph.microsoft.com").mock(return_value=httpx.Response(status, json=body))
    with pytest.raises(IntegrationError) as info:
        await provider(graph_settings, token_provider, http_client, sleep).fetch_changes(
            DELTA, max_messages=10, initial_days=14
        )
    assert info.value.category == "delta_expired"
    assert info.value.status == status


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
async def test_unauthorized_maps_to_authorization(graph_settings, token_provider, http_client, sleep, status):
    route = respx.get(host="graph.microsoft.com").mock(
        return_value=httpx.Response(status, json={"error": {"code": "InvalidAuthenticationToken"}})
    )
    with pytest.raises(IntegrationError) as info:
        await provider(graph_settings, token_provider, http_client, sleep).fetch_changes(
            None, max_messages=10, initial_days=14
        )
    assert info.value.category == FailureCategory.AUTHORIZATION
    assert info.value.retryable is False
    assert route.call_count == 1
    assert FAKE_ACCESS_TOKEN not in info.value.message


@respx.mock
async def test_throttling_is_retried_honouring_retry_after(graph_settings, token_provider, http_client, sleep):
    route = respx.get(host="graph.microsoft.com").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"code": "TooManyRequests"}}),
            httpx.Response(503, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"value": [message("m1")], "@odata.deltaLink": DELTA}),
        ]
    )
    result = await provider(graph_settings, token_provider, http_client, sleep).fetch_changes(
        None, max_messages=10, initial_days=14
    )
    assert route.call_count == 3
    assert sleep.calls == [7.0, 2.0]
    assert [e.provider_message_id for e in result.upserts] == ["m1"]


@respx.mock
async def test_persistent_throttling_surfaces_rate_limited(graph_settings, token_provider, http_client, sleep):
    respx.get(host="graph.microsoft.com").mock(return_value=httpx.Response(429, headers={"Retry-After": "1"}))
    with pytest.raises(IntegrationError) as info:
        await provider(graph_settings, token_provider, http_client, sleep).fetch_changes(
            None, max_messages=10, initial_days=14
        )
    assert info.value.category == FailureCategory.RATE_LIMITED
    assert info.value.retryable is True


@respx.mock
async def test_foreign_host_next_link_is_not_followed(graph_settings, token_provider, http_client, sleep):
    evil = respx.get(host="attacker.example.net").mock(return_value=httpx.Response(200, json={}))
    respx.get(host="graph.microsoft.com").mock(
        return_value=httpx.Response(
            200,
            json={"value": [message("m1")], "@odata.nextLink": "https://attacker.example.net/v1.0/steal?$skiptoken=x"},
        )
    )
    with pytest.raises(IntegrationError) as info:
        await provider(graph_settings, token_provider, http_client, sleep).fetch_changes(
            None, max_messages=10, initial_days=14
        )
    assert info.value.category == FailureCategory.VALIDATION
    assert not evil.called


@pytest.mark.parametrize(
    "link",
    [
        "https://attacker.example.net/v1.0/me/messages/delta?$deltatoken=x",
        "http://graph.microsoft.com/v1.0/me/messages/delta?$deltatoken=x",
        "https://user@graph.microsoft.com/v1.0/me/messages/delta",
        "https://graph.microsoft.com/beta/me/messages/delta",
        "demo:cursor:10",
    ],
)
async def test_stored_link_outside_graph_forces_resync(graph_settings, token_provider, sleep, link):
    # Nothing is mocked: any outgoing request would fail the test.
    with respx.mock, pytest.raises(IntegrationError) as info:
        await GraphMailboxProvider(token_provider, graph_settings, sleep=sleep).fetch_changes(
            link, max_messages=10, initial_days=14
        )
    assert info.value.category == "delta_expired"
    assert token_provider.calls == []


@respx.mock
async def test_entries_without_id_are_skipped(graph_settings, token_provider, http_client, sleep):
    respx.get(host="graph.microsoft.com").mock(
        return_value=httpx.Response(
            200, json={"value": [{"subject": "no id"}, "junk", message("m1")], "@odata.deltaLink": DELTA}
        )
    )
    result = await provider(graph_settings, token_provider, http_client, sleep).fetch_changes(
        None, max_messages=10, initial_days=14
    )
    assert [e.provider_message_id for e in result.upserts] == ["m1"]


@respx.mock
async def test_page_without_links_is_an_error(graph_settings, token_provider, http_client, sleep):
    respx.get(host="graph.microsoft.com").mock(return_value=httpx.Response(200, json={"value": []}))
    with pytest.raises(IntegrationError) as info:
        await provider(graph_settings, token_provider, http_client, sleep).fetch_changes(
            None, max_messages=10, initial_days=14
        )
    assert info.value.retryable is True
