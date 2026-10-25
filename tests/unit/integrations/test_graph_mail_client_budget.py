"""A Graph sync round is bounded in time and body bytes, and resumes where it stopped.

Without a budget a round could page for a long time (up to 500 pages, each with retries) and hold up to
`MAIL_SYNC_MAX_MESSAGES` full bodies in memory. A round that reaches either budget stops after the current
page and stores the pending `nextLink`, exactly like the message-count limit.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError

from app.integrations.microsoft_graph.mail_client import GraphMailboxProvider
from tests.conftest import make_settings
from tests.unit.integrations.test_graph_mail_client import DELTA, NEXT_1, NEXT_2, message


class FakeClock:
    def __init__(self, step: float) -> None:
        self.now = 1_000.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step  # every reading is one page later
        return value


def _settings(graph_settings: Any, **overrides: Any) -> Any:
    return graph_settings.model_copy(update=overrides)


@respx.mock
async def test_a_round_that_runs_out_of_time_stops_at_the_pending_page_and_resumes(
    graph_settings, token_provider, http_client, sleep
):
    route = respx.get(host="graph.microsoft.com").mock(
        side_effect=[
            httpx.Response(200, json={"value": [message("m1")], "@odata.nextLink": NEXT_1}),
            httpx.Response(200, json={"value": [message("m2")], "@odata.nextLink": NEXT_2}),
            httpx.Response(200, json={"value": [message("m3")], "@odata.deltaLink": DELTA}),
        ]
    )
    settings = _settings(graph_settings, mail_sync_round_seconds=100)
    graph = GraphMailboxProvider(token_provider, settings, http_client=http_client, sleep=sleep, clock=FakeClock(60))
    first = await graph.fetch_changes(None, max_messages=200, initial_days=7)
    assert first.truncated is True
    assert first.delta_link == NEXT_2  # nothing on the pending page was consumed
    assert [e.provider_message_id for e in first.upserts] == ["m1", "m2"]
    assert route.call_count == 2

    second = await graph.fetch_changes(first.delta_link, max_messages=200, initial_days=7)
    assert (second.truncated, second.delta_link) == (False, DELTA)
    assert [e.provider_message_id for e in second.upserts] == ["m3"]


@respx.mock
async def test_a_round_that_reaches_the_body_byte_budget_stops_at_the_pending_page(
    graph_settings, token_provider, http_client, sleep
):
    big = "x" * 40_000
    respx.get(host="graph.microsoft.com").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "value": [
                        message("m1", body={"contentType": "text", "content": big}),
                        message("m2", body={"contentType": "text", "content": big}),
                    ],
                    "@odata.nextLink": NEXT_1,
                },
            ),
            httpx.Response(200, json={"value": [message("m3")], "@odata.deltaLink": DELTA}),
        ]
    )
    settings = _settings(graph_settings, mail_sync_max_body_bytes=50_000)
    graph = GraphMailboxProvider(token_provider, settings, http_client=http_client, sleep=sleep)
    result = await graph.fetch_changes(None, max_messages=200, initial_days=7)
    assert result.truncated is True
    assert result.delta_link == NEXT_1
    assert [e.provider_message_id for e in result.upserts] == ["m1", "m2"]


@respx.mock
async def test_rounds_within_both_budgets_are_unchanged(graph_settings, token_provider, http_client, sleep):
    respx.get(host="graph.microsoft.com").mock(
        side_effect=[
            httpx.Response(200, json={"value": [message("m1")], "@odata.nextLink": NEXT_1}),
            httpx.Response(200, json={"value": [message("m2")], "@odata.deltaLink": DELTA}),
        ]
    )
    graph = GraphMailboxProvider(
        token_provider, graph_settings, http_client=http_client, sleep=sleep, clock=FakeClock(1)
    )
    result = await graph.fetch_changes(None, max_messages=200, initial_days=7)
    assert (result.truncated, result.delta_link) == (False, DELTA)


@pytest.mark.parametrize(("field", "value"), [("mail_sync_round_seconds", 0), ("mail_sync_max_body_bytes", 1023)])
def test_budgets_are_validated(database_url: str, field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(database_url, **{field: value})


def test_budget_defaults_fit_inside_the_job_lease(database_url: str) -> None:
    settings = make_settings(database_url)
    assert 0 < settings.mail_sync_round_seconds < settings.job_lease_seconds
    assert settings.mail_sync_max_body_bytes >= 1024
