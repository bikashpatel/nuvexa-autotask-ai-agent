"""Demo mailbox delta semantics/determinism and Graph message normalization edge cases."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.errors import IntegrationError
from app.integrations.microsoft_graph import demo_mailbox
from app.integrations.microsoft_graph.demo_mailbox import DemoMailboxProvider, load_demo_fixture
from app.integrations.microsoft_graph.normalize import normalize_graph_message, parse_graph_datetime

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------------------------
# Demo mailbox
# ---------------------------------------------------------------------------------------------


async def test_max_messages_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_messages"):
        await DemoMailboxProvider(now=NOW).fetch_changes(None, max_messages=0, initial_days=1)


@pytest.mark.parametrize(
    ("cursor", "max_messages", "ids", "next_cursor", "truncated"),
    [
        ("demo:cursor:0", 200, ["demo-msg-001", "demo-msg-002"], "demo:cursor:2", False),  # batch size, not round 1
        ("demo:cursor:0010", 200, ["demo-msg-011", "demo-msg-012"], "demo:cursor:12", False),  # leading zeros
        ("demo:cursor:10", 1, ["demo-msg-011"], "demo:cursor:11", True),
        ("demo:cursor:13", 1, ["demo-msg-014"], "demo:cursor:14", False),  # bound hit exactly at the end
        ("demo:cursor:13", 5, ["demo-msg-014"], "demo:cursor:14", False),
        ("demo:cursor:14", 5, [], "demo:cursor:14", False),
    ],
)
async def test_cursor_semantics(cursor, max_messages, ids, next_cursor, truncated) -> None:
    result = await DemoMailboxProvider(now=NOW).fetch_changes(cursor, max_messages=max_messages, initial_days=1)
    assert [e.provider_message_id for e in result.upserts] == ids
    assert result.delta_link == next_cursor
    assert result.truncated is truncated
    assert result.removed_provider_ids == []


@pytest.mark.parametrize(
    "cursor", ["demo:cursor:15", "demo:cursor:12345", "demo:cursor:", "DEMO:CURSOR:1", " demo:cursor:1"]
)
async def test_more_invalid_cursors(cursor: str) -> None:
    with pytest.raises(IntegrationError) as info:
        await DemoMailboxProvider(now=NOW).fetch_changes(cursor, max_messages=5, initial_days=1)
    assert info.value.category == "delta_expired"


@settings(max_examples=30, deadline=None)
@given(st.lists(st.integers(min_value=1, max_value=6), min_size=1, max_size=12))
async def test_any_paging_sequence_delivers_each_message_exactly_once_in_order(bounds: list[int]) -> None:
    mailbox = DemoMailboxProvider(now=NOW)
    delivered: list[str] = []
    cursor: str | None = None
    for bound in [*bounds, *([200] * 20)]:
        result = await mailbox.fetch_changes(cursor, max_messages=bound, initial_days=14)
        delivered += [e.provider_message_id for e in result.upserts]
        cursor = result.delta_link
    assert delivered == [f"demo-msg-{i:03d}" for i in range(1, mailbox.total_messages + 1)]


async def test_rendering_is_deterministic_and_normalized() -> None:
    a = await DemoMailboxProvider(now=NOW).fetch_changes(None, max_messages=200, initial_days=1)
    b = await DemoMailboxProvider(now=NOW.astimezone(timezone(timedelta(hours=-5)))).fetch_changes(
        None, max_messages=200, initial_days=1
    )
    assert [e.model_dump() for e in a.upserts] == [e.model_dump() for e in b.upserts]
    first = a.upserts[0]
    assert first.internet_message_id == "<demo-msg-001@demo.example.com>"
    assert first.change_key == "demo-msg-001-v1"
    assert first.conversation_id is not None
    assert first.conversation_id.startswith("demo-conv-")
    assert first.sent_at == first.received_at - timedelta(minutes=1)
    assert first.web_link is None
    for email in a.upserts:
        assert email.to_recipients  # defaults to the demo mailbox when the fixture omits "to"
        assert "{{" not in email.subject
        assert "{{" not in email.body_text
        assert email.importance in {"low", "normal", "high"}


async def test_default_now_is_current_and_minute_aligned() -> None:
    mailbox = DemoMailboxProvider()
    assert mailbox._now.second == 0
    assert mailbox._now.microsecond == 0
    assert abs((datetime.now(UTC) - mailbox._now).total_seconds()) < 120
    naive = DemoMailboxProvider(now=datetime(2026, 10, 25, 12, 0))
    assert naive._now.tzinfo is UTC


@pytest.mark.parametrize("text", ['["not", "an", "object"]', '{"messages": {"a": 1}}', '{"mailbox": {}}'])
def test_malformed_fixture_is_rejected(monkeypatch, text: str) -> None:
    class FakeFile:
        def read_text(self, encoding: str) -> str:
            return text

    class FakeRoot:
        def joinpath(self, path: str) -> FakeFile:
            assert path == "fixtures/demo_mailbox.json"
            return FakeFile()

    monkeypatch.setattr(demo_mailbox.resources, "files", lambda package: FakeRoot())
    with pytest.raises(ValueError, match="malformed"):
        load_demo_fixture.__wrapped__()


def test_fixture_is_cached() -> None:
    assert load_demo_fixture() is load_demo_fixture()


def test_fixture_defaults_when_optional_keys_are_missing(monkeypatch) -> None:
    fixture = {"messages": [{"id": "m1", "from": {"name": "A", "address": "a@example.com"}}]}
    monkeypatch.setattr(demo_mailbox, "load_demo_fixture", lambda: fixture)
    mailbox = DemoMailboxProvider(now=NOW)
    assert (mailbox._first_round, mailbox._batch, mailbox._mailbox) == (10, 2, {})
    rendered = mailbox._render(fixture["messages"][0])
    assert rendered["receivedDateTime"] == "2026-10-25T12:00:00Z"
    assert rendered["toRecipients"] == [{"emailAddress": {"name": None, "address": None}}]
    assert rendered["body"] == {"contentType": "text", "content": ""}
    assert rendered["hasAttachments"] is False
    email = normalize_graph_message(rendered)
    assert email.to_recipients == []  # empty recipients are dropped by the normalizer


# ---------------------------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------------------------


def test_recipient_and_attachment_shapes() -> None:
    email = normalize_graph_message(
        {
            "id": "m",
            "from": {"emailAddress": "flat string"},
            "sender": {"emailAddress": {"name": "Del\u200begate\u0007", "address": "  "}},
            "toRecipients": [{"emailAddress": None}, {"emailAddress": {"address": "a@example.com"}}],
            "ccRecipients": "not a list",
            "attachments": [
                "junk",
                {"name": None, "size": -1, "contentType": "x" * 300},
                {"name": "  ", "size": 10, "isInline": "TRUE"},
                {"name": "ok.pdf", "size": 1.5, "contentType": 7},
            ],
        }
    )
    assert email.sender is not None
    assert (email.sender.name, email.sender.address) == ("Delegate", None)
    assert [r.address for r in email.to_recipients] == ["a@example.com"]
    assert email.cc_recipients == []
    assert [(a.name, a.size, a.is_inline) for a in email.attachments] == [
        ("(unnamed attachment)", None, False),
        ("(unnamed attachment)", 10, True),
        ("ok.pdf", None, False),
    ]
    assert email.attachments[0].content_type == "x" * 255
    assert email.attachments[2].content_type is None
    assert email.has_attachments is True


def test_field_limits() -> None:
    email = normalize_graph_message(
        {
            "id": "i" * 512,
            "internetMessageId": "<" + "x" * 2000 + ">",
            "conversationId": "c" * 600,
            "changeKey": "  ",
            "from": {"emailAddress": {"name": "N" * 400, "address": "a" * 400}},
            "toRecipients": [{"emailAddress": {"address": f"u{i}@example.com"}} for i in range(150)],
            "attachments": [{"name": f"f{i}"} for i in range(80)],
            "bodyPreview": "p" * 900,
        }
    )
    assert len(email.provider_message_id) == 512
    assert email.internet_message_id is not None
    assert len(email.internet_message_id) == 998
    assert email.conversation_id == "c" * 512
    assert email.change_key is None
    assert email.sender is not None
    assert (len(email.sender.name or ""), len(email.sender.address or "")) == (320, 320)
    assert len(email.to_recipients) == 100
    assert len(email.attachments) == 50
    assert email.body_preview == "p" * 500


@pytest.mark.parametrize(
    ("body", "text", "kind"),
    [
        ({"contentType": "HTML", "content": "<p>A</p><style>p{}</style><p>B &amp; C</p>"}, "A\n\nB & C", "html"),
        ({"contentType": "text", "content": "Line\r\nTwo\u200b"}, "Line\nTwo", "text"),
        ({"content": "no type"}, "no type", "text"),
        ({"contentType": "html", "content": None}, "", "html"),
        ("not a mapping", "", "text"),
    ],
)
def test_body_variants(body: Any, text: str, kind: str) -> None:
    email = normalize_graph_message({"id": "m", "body": body})
    assert email.body_text == text
    assert email.original_body_content_type == kind
    assert email.body_preview == text  # no bodyPreview -> derived from the body


def test_preview_and_subject_are_normalized_independently() -> None:
    email = normalize_graph_message(
        {
            "id": "m",
            "subject": "  Re:\u00a0\u200bQ3\n\treport  ",
            "body": {"contentType": "html", "content": "<p>Full body</p>"},
            "bodyPreview": "Preview\u200b text",
        }
    )
    assert email.subject == "Re: Q3 report"
    assert email.body_preview == "Preview text"
    assert email.body_text == "Full body"


@pytest.mark.parametrize(
    ("link", "kept"),
    [
        ("https://outlook.office.com/mail/id/1", True),
        ("http://outlook.office.com/mail/id/1", False),
        ("https:///no-host", False),
        ("data:text/html,<script>", False),
        ("", False),
        (42, False),
    ],
)
def test_web_link_filter(link: Any, kept: bool) -> None:
    email = normalize_graph_message({"id": "m", "webLink": link})
    assert (email.web_link == link) is kept


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-20T08:30:00Z", datetime(2026, 10, 20, 8, 30, tzinfo=UTC)),
        ("2026-10-20T14:00:00+05:30", datetime(2026, 10, 20, 8, 30, tzinfo=UTC)),
        ("2026-10-20T08:30:00.1234567Z", datetime(2026, 10, 20, 8, 30, 0, 123456, tzinfo=UTC)),
        ("2026-10-20", datetime(2026, 10, 20, tzinfo=UTC)),
        ("2026-13-01T00:00:00Z", None),
        ("", None),
        (1729413000, None),
        (["2026-10-20"], None),
    ],
)
def test_parse_graph_datetime(value: Any, expected: datetime | None) -> None:
    assert parse_graph_datetime(value) == expected


def test_timestamp_fallbacks() -> None:
    sent = normalize_graph_message({"id": "m", "receivedDateTime": "garbage", "sentDateTime": "2026-10-20T08:00:00Z"})
    assert sent.received_at == datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    before = datetime.now(UTC)
    now = normalize_graph_message({"id": "m"})
    assert before <= now.received_at <= datetime.now(UTC)
    assert now.received_at.tzinfo is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("TRUE", True), (True, True), ("false", False), (1, False), (None, False), ("yes", False)],
)
def test_boolean_coercion(raw: Any, expected: bool) -> None:
    email = normalize_graph_message({"id": "m", "isRead": raw, "hasAttachments": raw})
    assert email.is_read is expected
    assert email.has_attachments is expected


@settings(max_examples=100, deadline=None)
@given(
    st.dictionaries(
        st.sampled_from(
            ["subject", "from", "toRecipients", "body", "bodyPreview", "attachments", "webLink", "importance"]
        ),
        st.recursive(
            st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=20)),
            lambda children: st.one_of(
                st.lists(children, max_size=3), st.dictionaries(st.text(max_size=8), children, max_size=3)
            ),
            max_leaves=8,
        ),
    )
)
def test_normalizer_never_raises_for_arbitrary_fields(fields: dict[str, Any]) -> None:
    email = normalize_graph_message({"id": "m", **fields})
    assert email.provider_message_id == "m"
    assert email.received_at.tzinfo is not None
    assert all(unicodedata.category(c)[0] != "C" or c in "\n\t" for c in email.body_text)
    assert email.importance in {None, "low", "normal", "high"}
