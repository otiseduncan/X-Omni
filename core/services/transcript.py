"""X Omni -- conversation transcript export.

A conversation that went wrong should not be a dead end. Before this, the UI
could only ever reopen the most recent conversation, so the practical options
after a bad exchange were to keep arguing with it or abandon it -- there was
no way to set it aside, carry it somewhere else, and have it examined.

Export renders one whole conversation into a portable document: every
message, the cards X attached to them, the files the operator sent, and the
tool calls that actually ran. Markdown is for reading and for pasting into
another assistant; JSON is for a program that wants the structure back.

Nothing here reformats or interprets what was said. A transcript that
paraphrased the exchange would be useless as evidence about the exchange.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

# A transcript is for reading, so a single tool result is trimmed rather than
# allowed to bury the conversation under one 200 KB payload.
MAX_TOOL_RESULT_CHARS = 4_000
MAX_ARTIFACT_CHARS = 4_000

ROLE_LABELS = {
    "user": "Otis",
    "assistant": "X",
    "system": "System",
    "tool": "Tool",
}


def _timestamp(value: object) -> str:
    text = str(value or "").strip()
    return text or "unknown time"


def _slug(value: object, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return (slug[:60] or fallback)


def conversation_title(meta: dict) -> str:
    title = str(meta.get("title") or "").strip()
    return title or f"Conversation {meta.get('id')}"


def export_filename(meta: dict, fmt: str) -> str:
    """A filename that says which conversation this is, without guesswork."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    extension = "json" if fmt == "json" else "md"
    return (
        f"x-omni-conversation-{meta.get('id')}-"
        f"{_slug(meta.get('title'), 'transcript')}-{stamp}.{extension}"
    )


def _trim(text: str, limit: int) -> str:
    body = str(text or "")
    if len(body) <= limit:
        return body
    return f"{body[:limit].rstrip()}\n... [trimmed, {len(body) - limit:,} more characters]"


def _dump(value: Any, limit: int) -> str:
    try:
        rendered = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = str(value)
    return _trim(rendered, limit)


def _artifact_summary(artifact: dict) -> str:
    """One stored card, rendered for a reader rather than for a parser."""
    kind = str(artifact.get("type") or "artifact")
    data = artifact.get("data")
    if not isinstance(data, dict):
        return f"- **{kind}**"
    if kind == "attachment":
        size = data.get("size_label") or f"{int(data.get('bytes') or 0):,} bytes"
        label = data.get("kind_label") or data.get("kind") or "file"
        return f"- **attachment** `{data.get('filename')}` ({label}, {size})"
    if kind == "camera_observation":
        return f"- **camera observation** — {str(data.get('description') or '').strip()[:400]}"
    headline = data.get("summary") or data.get("message") or data.get("status")
    if isinstance(headline, str) and headline.strip():
        return f"- **{kind}** — {headline.strip()[:400]}"
    return f"- **{kind}**\n\n```json\n{_dump(data, MAX_ARTIFACT_CHARS)}\n```"


def _attachment_line(record: dict) -> str:
    pages = record.get("page_count")
    parts = [
        f"`{record.get('filename')}`",
        str(record.get("kind") or "file"),
        f"{int(record.get('byte_count') or 0):,} bytes",
    ]
    if pages:
        parts.append(f"{int(pages)} page(s)")
    parts.append(f"read via {record.get('extraction_method') or 'unknown'}")
    if record.get("truncated"):
        parts.append("**truncated**")
    line = f"- id {record.get('id')}: " + ", ".join(parts)
    note = str(record.get("note") or "").strip()
    if note:
        line += f"\n  - Note: {note}"
    return line


def _tool_call_block(call: dict) -> str:
    status = str(call.get("status") or "unknown")
    lines = [
        f"#### `{call.get('tool_name')}` — {status}"
        + (f" (approval {call['approval_id']})" if call.get("approval_id") else ""),
        f"*{_timestamp(call.get('created_at'))}*",
        "",
        "Arguments:",
        "",
        f"```json\n{_dump(call.get('args'), MAX_TOOL_RESULT_CHARS)}\n```",
    ]
    if call.get("result") is not None:
        lines += [
            "",
            "Result:",
            "",
            f"```json\n{_dump(call.get('result'), MAX_TOOL_RESULT_CHARS)}\n```",
        ]
    return "\n".join(lines)


def to_markdown(
    meta: dict,
    messages: list[dict],
    attachments: Optional[list[dict]] = None,
    tool_calls: Optional[list[dict]] = None,
) -> str:
    """Render one conversation as a readable, self-describing transcript."""
    attachments = attachments or []
    tool_calls = tool_calls or []
    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        f"# {conversation_title(meta)}",
        "",
        "> X Omni conversation transcript. Every message below is reproduced as "
        "it was stored — nothing is summarized or rewritten.",
        "",
        f"- **Conversation ID:** {meta.get('id')}",
        f"- **Started:** {_timestamp(meta.get('started_at'))}",
        f"- **Last activity:** {_timestamp(meta.get('updated_at'))}",
        f"- **Messages:** {len(messages)}",
        f"- **Exported:** {exported_at}",
        "",
    ]

    if attachments:
        lines += [
            "## Files attached in this conversation",
            "",
            "X read each of these into text at upload time; the text is what "
            "the model saw, and it is quoted inline in the messages below.",
            "",
            *[_attachment_line(record) for record in attachments],
            "",
        ]

    calls_by_message: dict[Any, list[dict]] = {}
    for call in tool_calls:
        calls_by_message.setdefault(call.get("message_id"), []).append(call)

    lines += ["## Transcript", ""]
    for message in messages:
        role = str(message.get("role") or "unknown")
        label = ROLE_LABELS.get(role, role.title())
        worker = str(message.get("worker_used") or "").strip()
        heading = f"### {label}"
        if worker and role == "assistant":
            heading += f" ({worker})"
        lines += [heading, f"*{_timestamp(message.get('created_at'))}*", ""]

        content = str(message.get("content") or "").strip()
        lines.append(content if content else "*(no message text)*")
        lines.append("")

        artifacts = message.get("artifacts")
        if isinstance(artifacts, list) and artifacts:
            lines += ["**Cards attached to this message:**", ""]
            lines += [
                _artifact_summary(item)
                for item in artifacts
                if isinstance(item, dict)
            ]
            lines.append("")

        related = calls_by_message.get(message.get("id")) or []
        if related:
            lines += ["**Tools called on this turn:**", ""]
            for call in related:
                lines += [_tool_call_block(call), ""]

    orphans = calls_by_message.get(None) or []
    if orphans:
        lines += ["## Tool calls not bound to a message", ""]
        for call in orphans:
            lines += [_tool_call_block(call), ""]

    return "\n".join(lines).rstrip() + "\n"


def to_json(
    meta: dict,
    messages: list[dict],
    attachments: Optional[list[dict]] = None,
    tool_calls: Optional[list[dict]] = None,
) -> str:
    """Structured export, for a program that wants the conversation back."""
    payload = {
        "format": "x-omni-conversation-export",
        "format_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "conversation": {
            "id": meta.get("id"),
            "title": conversation_title(meta),
            "started_at": meta.get("started_at"),
            "updated_at": meta.get("updated_at"),
        },
        "attachments": attachments or [],
        "messages": [
            {
                "id": message.get("id"),
                "role": message.get("role"),
                "content": message.get("content"),
                "worker_used": message.get("worker_used"),
                "created_at": message.get("created_at"),
                "artifacts": message.get("artifacts") or [],
            }
            for message in messages
        ],
        "tool_calls": tool_calls or [],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def export(
    meta: dict,
    messages: list[dict],
    attachments: Optional[list[dict]] = None,
    tool_calls: Optional[list[dict]] = None,
    *,
    fmt: str = "markdown",
) -> str:
    if fmt == "json":
        return to_json(meta, messages, attachments, tool_calls)
    return to_markdown(meta, messages, attachments, tool_calls)
