"""
X Omni -- the response contract for one assistant turn.

A turn produces two different things, and this module keeps them apart:

* the conversational answer X wrote (``assistant_text``), which is what the
  chat shows as X's reply and the only thing voice playback reads
  (``spoken_text``); and
* the evidence and execution record behind it -- source documents, OCR and
  table extractions, Calibration IQ rows, ScrapeX results, receipts, statuses
  -- which stays attached to the reply for audit but is presented collapsed.

Nothing here writes, rewrites, or summarizes the answer. ``assistant_text`` is
exactly the text the model produced (or the fixed receipt-grounded prose Core
already emits for media and website turns); the contract only describes it
and the cards that came with it. Classification is by card type, never by the
wording of the request or the answer.

Live failure this exists for (2026-09-17, conversation 257): X retrieved the
Hyundai/Kia/Genesis front radar bumper chart, the research card rendered its
flattened OCR open in the conversation, and the reply itself read like a
search-result summary.
"""

from __future__ import annotations

import re
from typing import Any, Optional

CONTRACT_VERSION = "x-omni.response.v1"

PRESENTATION_PRIMARY = "primary"
PRESENTATION_EVIDENCE = "evidence"

# Cards that *are* the deliverable or need Otis to act: media he asked for, a
# page he asked to open, live camera controls, approvals, and the everyday
# widgets whose card is the answer. Everything else a tool returns -- search
# results, research findings, Calibration IQ reads and receipts, ScrapeX
# results, inventories, background-job records, capability listings -- is
# evidence and is presented collapsed. Unknown card types default to evidence
# so a new tool can never dump machine data into the conversation.
#
# ui/src/lib/responsePresentation.js mirrors this set for stored messages that
# predate the presentation stamp; a UI test keeps the two identical.
PRIMARY_ARTIFACT_TYPES = frozenset(
    {
        "adas_si_document",
        "approval",
        "approval_request",
        "calendar",
        "camera_event_history",
        "camera_footage_analysis",
        "camera_motion_clip",
        "camera_observation",
        "camera_request",
        "camera_snapshot",
        "exterior_camera_request",
        "generated_image",
        "generated_video",
        "image_generation_status",
        "shell_result",
        "tasks",
        "video_generation_status",
        "weather",
        "website_preview",
    }
)

EVIDENCE_DEFAULT_STATE = "collapsed"

_ATTENTION_STATUSES = frozenset(
    {
        "authentication_required",
        "blocked",
        "error",
        "failed",
        "indeterminate",
        "not_executed",
        "partial",
        "partial_success",
        "read_failed",
        "unavailable",
        "unverified_result",
    }
)

_EVIDENCE_LABELS = {
    "adas_map_sweep": "ADAS Map sweep",
    "adas_si_inventory": "ADAS SI inventory",
    "adas_si_record": "ADAS SI record",
    "adas_si_records": "ADAS SI records",
    "adas_si_research": "SI research",
    "adas_si_results": "ADAS SI search",
    "automotive_knowledge": "Knowledge records",
    "calibration_iq_receipt": "Calibration IQ receipt",
    "calibration_iq_ro": "Calibration IQ repair order",
    "calibration_iq_ros": "Calibration IQ list",
    "calibration_iq_status": "Calibration IQ status",
    "calibration_iq_summary": "Calibration IQ count",
    "calibration_iq_work_prep": "Calibration IQ work prep",
    "capabilities": "Capabilities",
    "execution_receipt": "Execution receipt",
    "research_findings": "Research",
    "research_provider": "Service information research",
    "scrapex": "ScrapeX",
    "web_research": "Web research",
}


def artifact_presentation(artifact_type: Any) -> str:
    """How a card of this type is presented: the deliverable, or evidence."""

    name = str(artifact_type or "").strip().casefold()
    return PRESENTATION_PRIMARY if name in PRIMARY_ARTIFACT_TYPES else PRESENTATION_EVIDENCE


def present_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    """Stamp a card with its presentation level, in place, and return it."""

    if isinstance(artifact, dict):
        artifact["presentation"] = artifact_presentation(artifact.get("type"))
    return artifact


def present_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for artifact in artifacts:
        present_artifact(artifact)
    return artifacts


def _needs_attention(data: Any) -> bool:
    """A failed, partial, blocked, or unverified record must not look settled."""

    if not isinstance(data, dict):
        return False
    for key in ("status", "stage", "outcome"):
        value = data.get(key)
        if isinstance(value, str) and value.strip().casefold() in _ATTENTION_STATUSES:
            return True
    return data.get("success") is False or data.get("verified") is False


def _bounded(value: Any, limit: int = 240) -> Optional[str]:
    text = " ".join(str(value or "").split())
    return text[:limit] if text else None


def _source_entry(**fields: Any) -> Optional[dict[str, Any]]:
    entry = {key: value for key, value in fields.items() if value not in (None, "")}
    return entry if entry.get("title") or entry.get("url") else None


def _sources_from(artifact_type: str, data: Any) -> list[dict[str, Any]]:
    """Document/page/link identities a card cites, read from its known shape."""

    if not isinstance(data, dict):
        return []
    entries: list[Optional[dict[str, Any]]] = []
    if artifact_type == "research_findings":
        for finding in data.get("findings") or []:
            if isinstance(finding, dict):
                entries.append(
                    _source_entry(
                        title=_bounded(finding.get("title")),
                        source=finding.get("source"),
                        page=finding.get("page"),
                        url=finding.get("url"),
                    )
                )
    elif artifact_type == "adas_si_results":
        for hit in data.get("results") or []:
            if isinstance(hit, dict):
                entries.append(
                    _source_entry(
                        title=_bounded(hit.get("title") or hit.get("source")),
                        source="adas_si",
                        page=hit.get("page"),
                        url=hit.get("url"),
                    )
                )
    elif artifact_type == "adas_si_document" and isinstance(data.get("document"), dict):
        document = data["document"]
        entries.append(
            _source_entry(
                title=_bounded(document.get("title")),
                source="adas_si",
                page=document.get("page"),
                url=document.get("url"),
            )
        )
    elif artifact_type == "web_research":
        for source in data.get("sources") or []:
            if isinstance(source, dict):
                entries.append(
                    _source_entry(
                        title=_bounded(source.get("title")),
                        source="web",
                        url=source.get("url"),
                    )
                )
    return [entry for entry in entries if entry]


def _receipt_from(artifact_type: str, data: Any) -> Optional[dict[str, Any]]:
    if artifact_type not in {"execution_receipt", "calibration_iq_receipt"}:
        return None
    if not isinstance(data, dict):
        return None
    receipt = {
        key: data.get(key)
        for key in ("tool_name", "status", "executed", "success", "approval_id")
        if data.get(key) is not None
    }
    return {"type": artifact_type, **receipt}


_FENCED_BLOCK_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_IMAGE_OR_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+|(?<!\w)/api/\S+", re.IGNORECASE)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"(\*\*\*|\*\*|__|~~|`)")


def spoken_text(assistant_text: str) -> str:
    """The speakable form of the answer: formatting removed, nothing added.

    Evidence never reaches voice because this is built from the answer alone.
    This step only drops what a voice cannot say usefully -- code blocks,
    Markdown table rows, link targets and bare URLs, and formatting markers.
    """

    text = str(assistant_text or "")
    if not text.strip():
        return ""
    text = _FENCED_BLOCK_RE.sub(" ", text)
    text = _TABLE_LINE_RE.sub("", text)
    text = _IMAGE_OR_LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub("", text)
    text = _HEADING_RE.sub("", text)
    text = _LIST_MARKER_RE.sub("", text)
    text = _EMPHASIS_RE.sub("", text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build_response(
    assistant_text: str,
    artifacts: Optional[list[dict[str, Any]]] = None,
    *,
    metrics: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Describe one finished turn as answer plus separately presented evidence."""

    answer = str(assistant_text or "")
    evidence: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    primary: list[dict[str, Any]] = []
    seen_sources: set[tuple[Any, ...]] = set()
    for index, artifact in enumerate(artifacts or []):
        if not isinstance(artifact, dict):
            continue
        artifact_type = str(artifact.get("type") or "unknown")
        data = artifact.get("data")
        presentation = artifact_presentation(artifact_type)
        if presentation == PRESENTATION_PRIMARY:
            primary.append({"artifact_index": index, "type": artifact_type})
            continue
        evidence.append(
            {
                "artifact_index": index,
                "type": artifact_type,
                "label": _EVIDENCE_LABELS.get(artifact_type, artifact_type.replace("_", " ")),
                "attention": _needs_attention(data),
            }
        )
        for source in _sources_from(artifact_type, data):
            key = (source.get("title"), source.get("page"), source.get("url"))
            if key not in seen_sources:
                seen_sources.add(key)
                sources.append(source)
        receipt = _receipt_from(artifact_type, data)
        if receipt:
            receipts.append(receipt)
    debug_metadata: dict[str, Any] = {"artifact_count": len(artifacts or [])}
    if isinstance(metrics, dict):
        debug_metadata["tools_selected"] = list(metrics.get("tools_selected") or [])
        debug_metadata["evidence_ids"] = list(metrics.get("evidence_ids") or [])
    return {
        "contract": CONTRACT_VERSION,
        "assistant_text": answer,
        "spoken_text": spoken_text(answer),
        "primary": primary,
        "evidence": evidence,
        "evidence_presentation": {
            "default_state": EVIDENCE_DEFAULT_STATE,
            "count": len(evidence),
            "attention": any(item["attention"] for item in evidence),
        },
        "sources": sources[:24],
        "tool_receipts": receipts,
        "debug_metadata": debug_metadata,
    }
