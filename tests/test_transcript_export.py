"""Conversation transcript export -- the way out of a conversation gone wrong."""

from __future__ import annotations

import json

from core.services import transcript


META = {
    "id": 42,
    "title": "Front camera calibration",
    "started_at": "2026-09-10 08:00:00",
    "updated_at": "2026-09-10 09:30:00",
}

MESSAGES = [
    {
        "id": 1,
        "role": "user",
        "content": "What is the target distance?",
        "worker_used": None,
        "created_at": "2026-09-10 08:00:01",
        "artifacts": [],
    },
    {
        "id": 2,
        "role": "assistant",
        "content": "1.5 metres from the emblem.",
        "worker_used": "omni",
        "created_at": "2026-09-10 08:00:20",
        "artifacts": [
            {
                "type": "attachment",
                "data": {
                    "filename": "spec.pdf",
                    "kind": "pdf",
                    "kind_label": "PDF",
                    "bytes": 2048,
                    "size_label": "2.0 KB",
                },
            }
        ],
    },
]

ATTACHMENTS = [
    {
        "id": 5,
        "filename": "spec.pdf",
        "kind": "pdf",
        "byte_count": 2048,
        "page_count": 4,
        "extraction_method": "pypdf",
        "truncated": True,
        "note": "1 page(s) required OCR; that text may contain errors.",
    }
]

TOOL_CALLS = [
    {
        "id": 1,
        "message_id": 2,
        "tool_name": "query_ciq",
        "args": {"ro": "12345"},
        "result": {"ok": True, "status": "open"},
        "status": "succeeded",
        "approval_id": None,
        "created_at": "2026-09-10 08:00:10",
        "completed_at": "2026-09-10 08:00:12",
    }
]


def test_markdown_reproduces_every_message_verbatim():
    rendered = transcript.to_markdown(META, MESSAGES, ATTACHMENTS, TOOL_CALLS)
    assert "# Front camera calibration" in rendered
    assert "What is the target distance?" in rendered
    assert "1.5 metres from the emblem." in rendered
    assert "**Conversation ID:** 42" in rendered
    assert "**Messages:** 2" in rendered


def test_markdown_labels_speakers_and_the_worker_that_answered():
    rendered = transcript.to_markdown(META, MESSAGES)
    assert "### Otis" in rendered
    assert "### X (omni)" in rendered


def test_markdown_lists_attachments_with_their_extraction_honesty():
    rendered = transcript.to_markdown(META, MESSAGES, ATTACHMENTS)
    assert "## Files attached in this conversation" in rendered
    assert "`spec.pdf`" in rendered
    assert "4 page(s)" in rendered
    assert "read via pypdf" in rendered
    assert "**truncated**" in rendered
    assert "required OCR" in rendered


def test_markdown_includes_the_tool_calls_that_actually_ran():
    rendered = transcript.to_markdown(META, MESSAGES, ATTACHMENTS, TOOL_CALLS)
    assert "**Tools called on this turn:**" in rendered
    assert "`query_ciq`" in rendered
    assert '"ro": "12345"' in rendered
    assert '"status": "open"' in rendered


def test_orphan_tool_calls_are_still_exported():
    orphan = [dict(TOOL_CALLS[0], message_id=None)]
    rendered = transcript.to_markdown(META, MESSAGES, [], orphan)
    assert "## Tool calls not bound to a message" in rendered
    assert "`query_ciq`" in rendered


def test_a_huge_tool_result_is_trimmed_not_dropped():
    noisy = [dict(TOOL_CALLS[0], result={"blob": "x" * 50_000})]
    rendered = transcript.to_markdown(META, MESSAGES, [], noisy)
    assert "trimmed" in rendered
    assert len(rendered) < 30_000


def test_empty_message_content_is_marked_not_silently_blank():
    blank = [dict(MESSAGES[0], content="")]
    assert "*(no message text)*" in transcript.to_markdown(META, blank)


def test_untitled_conversation_falls_back_to_its_id():
    assert transcript.conversation_title({"id": 7, "title": None}) == "Conversation 7"
    assert transcript.conversation_title({"id": 7, "title": "  "}) == "Conversation 7"


def test_json_export_round_trips_the_structure():
    payload = json.loads(transcript.to_json(META, MESSAGES, ATTACHMENTS, TOOL_CALLS))
    assert payload["format"] == "x-omni-conversation-export"
    assert payload["format_version"] == 1
    assert payload["conversation"]["id"] == 42
    assert len(payload["messages"]) == 2
    assert payload["messages"][1]["worker_used"] == "omni"
    assert payload["attachments"][0]["filename"] == "spec.pdf"
    assert payload["tool_calls"][0]["tool_name"] == "query_ciq"


def test_export_dispatches_on_format():
    assert transcript.export(META, MESSAGES, fmt="json").lstrip().startswith("{")
    assert transcript.export(META, MESSAGES, fmt="markdown").startswith("#")


def test_export_filename_identifies_the_conversation():
    name = transcript.export_filename(META, "markdown")
    assert name.startswith("x-omni-conversation-42-front-camera-calibration-")
    assert name.endswith(".md")
    assert transcript.export_filename(META, "json").endswith(".json")


def test_export_filename_survives_a_hostile_title():
    name = transcript.export_filename(
        {"id": 3, "title": "../../etc/passwd  <script>"}, "markdown"
    )
    assert "/" not in name and "\\" not in name and "<" not in name
    assert name.startswith("x-omni-conversation-3-")
