from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.integrations.microsoft_graph.normalize import normalize_graph_message, parse_graph_datetime, usable_message_id

FULL = {
    "id": "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0Ac=",
    "internetMessageId": "<abc123@mail.example.com>",
    "conversationId": "AAQkConversation=",
    "changeKey": "CQAAABYAAAA",
    "subject": "  Weekly\r\nreport  ",
    "from": {"emailAddress": {"name": "Ada Example", "address": "ada@example.com"}},
    "sender": {"emailAddress": {"name": "Delegate", "address": "delegate@example.com"}},
    "toRecipients": [
        {"emailAddress": {"name": "Support", "address": "support@example.com"}},
        {"emailAddress": {}},
        "garbage",
    ],
    "ccRecipients": [{"emailAddress": {"address": "cc@example.org"}}],
    "receivedDateTime": "2026-10-20T08:30:00Z",
    "sentDateTime": "2026-10-20T08:29:12.1234567Z",
    "body": {"contentType": "text", "content": "Line one\r\n\r\n\r\n\r\nLine\u200b two"},
    "bodyPreview": "Line one",
    "hasAttachments": True,
    "importance": "High",
    "isRead": True,
    "webLink": "https://outlook.office365.com/owa/?ItemID=AAk&exvsurl=1",
}


def test_full_text_message():
    email = normalize_graph_message(FULL)
    assert email.provider_message_id == FULL["id"]
    assert email.internet_message_id == "<abc123@mail.example.com>"
    assert email.conversation_id == "AAQkConversation="
    assert email.change_key == "CQAAABYAAAA"
    assert email.subject == "Weekly report"
    assert email.sender is not None
    assert (email.sender.name, email.sender.address) == ("Ada Example", "ada@example.com")
    assert [r.address for r in email.to_recipients] == ["support@example.com"]
    assert [r.address for r in email.cc_recipients] == ["cc@example.org"]
    assert email.received_at == datetime(2026, 10, 20, 8, 30, tzinfo=UTC)
    assert email.sent_at is not None
    assert email.sent_at.tzinfo is not None
    assert email.sent_at.replace(microsecond=0) == datetime(2026, 10, 20, 8, 29, 12, tzinfo=UTC)
    assert email.body_text == "Line one\n\nLine two"
    assert email.original_body_content_type == "text"
    assert email.has_attachments is True
    assert email.attachments == []
    assert email.importance == "high"
    assert email.is_read is True
    assert email.web_link == FULL["webLink"]


def test_html_body_is_converted_and_marked():
    email = normalize_graph_message(
        {
            "id": "m-html",
            "receivedDateTime": "2026-10-20T08:30:00+02:00",
            "body": {
                "contentType": "html",
                "content": "<html><body><p>Hello <b>team</b></p><script>alert(1)</script><p>Bye</p></body></html>",
            },
        }
    )
    assert email.original_body_content_type == "html"
    assert "alert" not in email.body_text
    assert "Hello team" in email.body_text
    assert "Bye" in email.body_text
    assert email.received_at == datetime(2026, 10, 20, 6, 30, tzinfo=UTC)
    assert email.body_preview == email.body_text


def test_missing_fields_use_safe_defaults():
    fallback = datetime(2026, 1, 1, tzinfo=UTC)
    email = normalize_graph_message({"id": "bare"}, fallback_received_at=fallback)
    assert email.subject == ""
    assert email.sender is None
    assert email.to_recipients == []
    assert email.received_at == fallback
    assert email.sent_at is None
    assert email.body_text == ""
    assert email.has_attachments is False
    assert email.importance is None
    assert email.is_read is False
    assert email.web_link is None


def test_sender_fallback_string_booleans_and_unsafe_links():
    email = normalize_graph_message(
        {
            "id": "m2",
            "sentDateTime": "2026-10-20T08:00:00Z",
            "sender": {"emailAddress": {"name": "Only Sender", "address": "only@example.com"}},
            "isRead": "true",
            "importance": "urgent!!",
            "webLink": "javascript:alert(1)",
        }
    )
    assert email.sender is not None
    assert email.sender.address == "only@example.com"
    assert email.received_at == datetime(2026, 10, 20, 8, 0, tzinfo=UTC)  # falls back to sentDateTime
    assert email.is_read is True
    assert email.importance is None
    assert email.web_link is None


def test_attachment_metadata_never_reads_content():
    email = normalize_graph_message(
        {
            "id": "m3",
            "receivedDateTime": "2026-10-20T08:30:00Z",
            "attachments": [
                {"name": "log.txt", "contentType": "text/plain", "size": 120, "contentBytes": "c2VjcmV0"},
                {"name": "inline.png", "contentType": "image/png", "size": True, "isInline": True},
            ],
        }
    )
    assert email.has_attachments is True
    assert [(a.name, a.content_type, a.size, a.is_inline) for a in email.attachments] == [
        ("log.txt", "text/plain", 120, False),
        ("inline.png", "image/png", None, True),
    ]
    assert "c2VjcmV0" not in email.model_dump_json()


@pytest.mark.parametrize("bad", [{}, {"id": ""}, {"id": 42}, {"id": "x" * 600}])
def test_message_without_usable_id_is_rejected(bad):
    with pytest.raises(ValueError, match="id"):
        normalize_graph_message(bad)


def test_parse_graph_datetime_handles_invalid_values():
    assert parse_graph_datetime("not a date") is None
    assert parse_graph_datetime(None) is None
    parsed = parse_graph_datetime("2026-10-20T08:30:00")  # naive → assumed UTC
    assert parsed == datetime(2026, 10, 20, 8, 30, tzinfo=UTC)


@pytest.mark.parametrize("bad_id", ["m\x00", "m\u202e1", "m-\ud800", "line\nbreak"])
def test_message_ids_with_control_characters_are_rejected(bad_id):
    # Regression (Prompt 5): the provider id is stored verbatim, so one PostgreSQL cannot store is unusable.
    assert usable_message_id(bad_id) is False
    with pytest.raises(ValueError, match="id"):
        normalize_graph_message({"id": bad_id})
    assert usable_message_id("AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0Ac=") is True


def test_identifier_fields_are_cleaned_and_bounded():
    # Regression (Prompt 5): NUL (rejected by PostgreSQL) and other control characters never reach the database.
    email = normalize_graph_message(
        {
            "id": "m4",
            "internetMessageId": "<abc\x00@example.com>" + "i" * 2_000,
            "conversationId": "\x00\x07  conv\r\n id\u200b ",
            "changeKey": "\x00\x00",
            "from": {"emailAddress": {"address": " ada\x00@exa\tmple.com "}},
            "toRecipients": [{"emailAddress": {"address": "to\x00@example.com"}}, {"emailAddress": {"address": 7}}],
            "attachments": [{"name": "a.txt", "contentType": 'text/plain\x00; name="x"\r\nX-Injected: 1'}],
        }
    )
    assert email.internet_message_id == ("<abc @example.com>" + "i" * 2_000)[:998]
    assert email.conversation_id == "conv id"
    assert email.change_key is None  # nothing usable left
    assert email.sender is not None
    assert email.sender.address == "ada @exa mple.com"
    assert [r.address for r in email.to_recipients] == ["to @example.com"]
    assert email.attachments[0].content_type == 'text/plain ; name="x" X-Injected: 1'


@pytest.mark.parametrize(
    "link",
    [
        "https://outlook.office.com/mail/\x00id",
        "https://outlook.office.com/mail/\r\nSet-Cookie: x",
        "https://outlook.office.com/mail/a b",
        "https://outlook.office.com/mail/" + "a" * 5_000,
    ],
    ids=["nul", "crlf", "space", "too-long"],
)
def test_web_links_with_control_characters_whitespace_or_absurd_length_are_dropped(link):
    assert normalize_graph_message({"id": "m5", "webLink": link}).web_link is None
    kept = normalize_graph_message({"id": "m5", "webLink": "https://outlook.office.com/mail/id"}).web_link
    assert kept == "https://outlook.office.com/mail/id"
