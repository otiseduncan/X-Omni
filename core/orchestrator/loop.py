"""
X Omni -- the turn loop.

One user message in, a stream of events out:
    {"type": "token",     "text": ...}
    {"type": "tool_start","name": ...,"args": ...}
    {"type": "tool_result","name": ...,"result": ...}
    {"type": "artifact",  "artifact": {...}}       inline chat card
    {"type": "approval",  "approval": {...}}       pauses, waits for operator
    {"type": "done",      "message_id": ...,"worker": ...}
    {"type": "error",     "message": ...}

Tool results become artifacts where a card exists for them, which is what
makes the UI chat-native: weather and calendar render inside the message
that produced them rather than in a separate panel.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any, Optional

from ..state.db import WebsiteRevisionConflict
from ..tools.registry import NeedsApproval, ToolBlocked, ToolError
from . import prompt as prompt_mod

log = logging.getLogger("xomni.loop")

MAX_TOOL_ROUNDS = 6

_EXTERIOR_CAMERA_SOURCE_RE = re.compile(
    r"\b(?:exterior|outside|outdoor|driveway|tris(?:\s+home)?)\s+camera\b"
    r"|\bcamera\s+(?:outside|outdoors|exterior)\b",
    re.IGNORECASE,
)
_EXTERIOR_CAMERA_ACTION_RE = re.compile(
    r"\b(?:look|looking|view|see|show|watch|inspect|check|use|open|start|turn|"
    r"stream|feed|frame|describe|tell|status|configured|connected|working)\b",
    re.IGNORECASE,
)
_WEBSITE_REFERENCE_RE = re.compile(
    r"\b(?:website|web\s*site|site|web\s*page|preview)\b",
    re.IGNORECASE,
)
_WEBSITE_EDIT_ACTION_RE = re.compile(
    r"\b(?:change|update|edit|modify|apply|convert|restyle|redesign|re-design)\b",
    re.IGNORECASE,
)
_WEBSITE_DESIGN_TARGET_RE = re.compile(
    r"\b(?:cards?|cords?|csrds?|sections?|colors?|palette|fonts?|typography|"
    r"buttons?|background|layout|header|footer|glass(?:morphism)?|translucent|frosted)\b",
    re.IGNORECASE,
)
_WEBSITE_MAKE_EDIT_RE = re.compile(r"\b(?:make|turn|set)\b", re.IGNORECASE)
_WEBSITE_CREATE_ACTION_RE = re.compile(
    r"\b(?:create|build|generate|design|render|display|make|show)\b",
    re.IGNORECASE,
)
_IMAGE_GENERATION_RE = re.compile(
    r"^\s*(?:please\s+)?(?:generate|create|make|render)\s+"
    r"(?:(?:an?\s+)?(?:image|picture|illustration|artwork|painting|portrait|drawing|"
    r"photograph)\b|(?=[^.!?\n]{0,120}\b(?:image|picture|illustration|artwork|painting|"
    r"portrait|drawing|photograph|figure\s+study)\b))",
    re.IGNORECASE,
)
_CIQ_COUNT_RE = re.compile(r"\b(?:how\s+many|count)\b", re.IGNORECASE)
_CIQ_LIST_RE = re.compile(
    r"\b(?:show|list|display|pull\s+up)\b",
    re.IGNORECASE,
)
_CIQ_SUBJECT_RE = re.compile(
    r"\b(?:cars?|vehicles?|repair\s+orders?|ros?|calibration\s+iq|active\s+work)\b",
    re.IGNORECASE,
)
_CIQ_DEICTIC_RE = re.compile(
    r"\b(?:those|them|these|that\s+list|the\s+results)\b",
    re.IGNORECASE,
)
_CIQ_PHASE_RE = re.compile(
    r"\bphase\s*(?:number\s*)?"
    r"(?P<phase>\d{1,2}|zero|one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.IGNORECASE,
)
_CIQ_ALL_WORK_RE = re.compile(
    r"\b(?:all\s+(?:work|cars?|vehicles?|repair\s+orders?)|"
    r"include\s+(?:completed|finished)|including\s+(?:completed|finished))\b",
    re.IGNORECASE,
)
_CIQ_NO_CALIBRATION_RE = re.compile(
    r"\bno\s+calibration\s+required\b",
    re.IGNORECASE,
)
_CIQ_COMPLETED_CATEGORY_RE = re.compile(
    r"\b(?:calibration\s+complete|completed)\b",
    re.IGNORECASE,
)
_CIQ_TERMINAL_WORK_RE = re.compile(
    r"\b(?:closed|finished|terminal)\s+(?:work|cars?|vehicles?|repair\s+orders?)\b",
    re.IGNORECASE,
)
_CIQ_SHOPS = (
    (re.compile(r"\bwarner\s+robins\b", re.IGNORECASE), "Warner Robins"),
    (re.compile(r"\bmacon\b", re.IGNORECASE), "Macon"),
    (re.compile(r"\bperry\b", re.IGNORECASE), "Perry"),
)
_CIQ_PHASE_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
}
_CIQ_CONTEXT_STRING_FILTERS = ("shop", "phase", "status", "insurance", "q")
_EXPLICIT_WEB_RESEARCH_RE = re.compile(
    r"\b(?:search|browse)\s+(?:the\s+)?(?:web|internet)\b"
    r"|\b(?:search|look\s+up|find)\b.{0,120}\b(?:online|on\s+the\s+(?:web|internet))\b"
    r"|\b(?:research|find)\b.{0,120}\b(?:sources?|literature|papers?|studies|court\s+cases?)\b"
    r"|\b(?:where|where's|wheres|were)\s+can\s+i\s+find\b.{0,120}\bonline\b"
    r"|\blook\s+(?:it|this|that)\s+up\b"
    r"|\bgoogle\s+(?:it|this|that|for)\b",
    re.IGNORECASE | re.DOTALL,
)
_WEB_SOURCE_REQUEST_RE = re.compile(
    r"\b(?:what|which)\s+(?:websites?|sites?|online\s+sources?)\b"
    r".{0,120}\b(?:information|discuss|cover|about)\b"
    r"|\bfind\s+(?:websites?|sites?|online\s+sources?|sources?)\b"
    r"|\bwhere\s+can\s+i\s+(?:read|learn)\s+about\b"
    r"|\bwhat\s+online\s+sources?\s+(?:cover|discuss|have)\b",
    re.IGNORECASE | re.DOTALL,
)
_CURRENT_INFORMATION_RE = re.compile(
    r"\b(?:current(?:ly)?|latest|newest|recent|breaking|right\s+now|today(?:'s)?|as\s+of\s+today)\b",
    re.IGNORECASE,
)
_CURRENT_INFORMATION_SUBJECT_RE = re.compile(
    r"\b(?:news|release|version|price|stock|market|office(?:holder)?|president|"
    r"governor|mayor|ceo|election|law|regulation|conflict|war|recall|score|"
    r"standings|public\s+schedule)\b",
    re.IGNORECASE,
)
_SPECIALIZED_CURRENT_LANE_RE = re.compile(
    r"\b(?:weather|forecast|temperature|radar|calendar|appointment|my\s+schedule|"
    r"repair\s+orders?|calibration\s+iq|adas)\b",
    re.IGNORECASE,
)
_WEB_ACCESS_DENIAL_RE = re.compile(
    r"\b(?:don't|do\s+not|doesn't|does\s+not|can't|cannot|unable\s+to)\b"
    r"[^.!?]{0,80}\b(?:access|browse|connect)\b[^.!?]{0,40}"
    r"\b(?:internet|web|external\s+websites?|online)\b"
    r"|\bno\s+(?:real[- ]?time\s+)?(?:internet|web)\s+access\b",
    re.IGNORECASE,
)
_WEB_RESEARCH_REFUSAL_RE = re.compile(
    r"^\s*(?:(?:i(?:'m|\s+am)\s+sorry|sorry)[,;:]?\s*)?(?:but\s+)?"
    r"(?:i\s+)?(?:cannot|can't|won't|am\s+unable\s+to)\s+"
    r"(?:provide|help|assist|comply|share|give|discuss|answer|support)\b",
    re.IGNORECASE,
)


def _safe_calibration_filters(raw: Any, *, result: Any = None) -> dict[str, Any]:
    """Keep only schema-compatible filter values from a prior CIQ artifact."""
    source = raw if isinstance(raw, dict) else {}
    safe: dict[str, Any] = {}
    for key in _CIQ_CONTEXT_STRING_FILTERS:
        value = source.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            safe[key] = str(value).strip()[:200]
    limit = source.get("limit")
    if isinstance(limit, int) and not isinstance(limit, bool):
        safe["limit"] = min(max(limit, 1), 100)
    include_completed = source.get("include_completed")
    if not isinstance(include_completed, bool) and isinstance(result, dict):
        include_completed = result.get("include_completed")
    if isinstance(include_completed, bool):
        safe["include_completed"] = include_completed
    terminal_only = source.get("terminal_only")
    if not isinstance(terminal_only, bool) and isinstance(result, dict):
        terminal_only = result.get("terminal_only")
    if terminal_only is True:
        safe["terminal_only"] = True
    return safe


def latest_calibration_iq_filters(history: list[dict]) -> dict[str, Any]:
    """Find the latest successful CIQ scope in this conversation only."""
    for message in reversed(history or []):
        artifacts = message.get("artifacts") or []
        if not isinstance(artifacts, list):
            continue
        for artifact in reversed(artifacts):
            if not isinstance(artifact, dict) or artifact.get("type") not in {
                "calibration_iq_summary",
                "calibration_iq_ros",
            }:
                continue
            data = artifact.get("data")
            if not isinstance(data, dict) or data.get("status") != "verified":
                continue
            return _safe_calibration_filters(data.get("filters"), result=data)
    return {}


def _explicit_calibration_filters(text: str) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    for pattern, label in _CIQ_SHOPS:
        if pattern.search(text):
            filters["shop"] = label
            break
    phase_match = _CIQ_PHASE_RE.search(text)
    if phase_match:
        token = phase_match.group("phase").casefold()
        filters["phase"] = (
            _CIQ_PHASE_WORDS[token] if token in _CIQ_PHASE_WORDS else str(int(token))
        )
    if _CIQ_ALL_WORK_RE.search(text):
        filters["include_completed"] = True
        filters["terminal_only"] = False
    elif _CIQ_NO_CALIBRATION_RE.search(text):
        filters["status"] = "No Calibration Required"
        filters["include_completed"] = True
        filters["terminal_only"] = True
    elif _CIQ_COMPLETED_CATEGORY_RE.search(text):
        filters["status"] = "Calibration Complete"
        filters["include_completed"] = True
        filters["terminal_only"] = True
    elif _CIQ_TERMINAL_WORK_RE.search(text):
        filters["include_completed"] = True
        filters["terminal_only"] = True
    elif re.search(r"\bactive\b", text, re.IGNORECASE):
        filters["include_completed"] = False
        filters["terminal_only"] = False
    return filters


def calibration_iq_read_request(
    user_message: object,
    history: list[dict],
) -> Optional[tuple[str, dict[str, Any]]]:
    """Deterministically route explicit CIQ counts/lists and deictic follow-ups."""
    text = str(user_message or "").strip()
    if not text:
        return None
    count_intent = bool(_CIQ_COUNT_RE.search(text))
    list_intent = bool(_CIQ_LIST_RE.search(text))
    if not count_intent and not list_intent:
        return None

    explicit = _explicit_calibration_filters(text)
    deictic = bool(_CIQ_DEICTIC_RE.search(text))
    inherited = latest_calibration_iq_filters(history) if deictic else {}
    if not (_CIQ_SUBJECT_RE.search(text) or explicit or inherited):
        return None

    args = {**inherited, **explicit}
    if "terminal_only" in explicit and "status" not in explicit:
        # An explicit active/all/generic-terminal scope replaces an inherited
        # exact terminal category instead of accidentally retaining it.
        args.pop("status", None)
    tool = "calibration_iq_summary" if count_intent else "calibration_iq_read"
    return tool, args


def _joined_breakdown(entries: list[tuple[str, Any]]) -> str:
    parts = [f"{count} {label}" for label, count in entries[:3]]
    if len(entries) > 3:
        parts.append(f"{len(entries) - 3} other statuses")
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def calibration_iq_result_summary(result: Any, *, listing: bool) -> str:
    """Fixed concise prose; the single inline card owns the row detail."""
    payload = result if isinstance(result, dict) else {}
    if payload.get("status") != "verified":
        return str(
            payload.get("message")
            or "Calibration IQ did not return a verified result."
        ).strip()[:600]

    count = int(payload.get("count") or 0)
    raw_filters = payload.get("filters")
    filters = raw_filters if isinstance(raw_filters, dict) else {}
    scope_parts = []
    if filters.get("shop"):
        scope_parts.append(str(filters["shop"]))
    if filters.get("phase") not in (None, ""):
        scope_parts.append(f"phase {filters['phase']}")
    scope = " in " + " ".join(scope_parts) if scope_parts else ""
    noun = "repair order" if count == 1 else "repair orders"
    active = (
        " terminal"
        if payload.get("terminal_only")
        else ("" if payload.get("include_completed") else " active")
    )

    if listing:
        shown = int(payload.get("shown_count") or 0)
        if payload.get("truncated"):
            return f"Showing {shown} of {count}{active} {noun}{scope}."
        return f"Showing all {count}{active} {noun}{scope}."

    lead = f"{count}{active} {noun}{scope}"
    by_status = (payload.get("breakdown") or {}).get("by_status") or {}
    breakdown = _joined_breakdown(list(by_status.items()))
    return f"{lead} — {breakdown}." if breakdown else f"{lead}."


def website_update_intent(user_message: object) -> bool:
    text = str(user_message or "").strip()
    if not text or not _WEBSITE_REFERENCE_RE.search(text):
        return False
    if _WEBSITE_EDIT_ACTION_RE.search(text):
        return True
    return bool(
        _WEBSITE_MAKE_EDIT_RE.search(text)
        and _WEBSITE_DESIGN_TARGET_RE.search(text)
    )


def website_generation_intent(user_message: object) -> bool:
    text = str(user_message or "").strip()
    return bool(
        text
        and _WEBSITE_REFERENCE_RE.search(text)
        and _WEBSITE_CREATE_ACTION_RE.search(text)
        and not website_update_intent(text)
    )


def website_result_summary(result: Any, *, update: bool) -> str:
    payload = result if isinstance(result, dict) else {}
    title = str(payload.get("title") or "website").strip()[:120]
    if payload.get("ok") is not True:
        detail = str(
            payload.get("message")
            or (
                "The website preview could not be revised."
                if update
                else "The website preview could not be generated."
            )
        ).strip()[:500]
        action = "update" if update else "generate"
        return f"I couldn't {action} the website preview. {detail}"
    if update and payload.get("changed") is False:
        return (
            f"No new revision was needed for {title}; the current chat preview already "
            "matches that edit. No files were written or deployed."
        )
    if update:
        if "cards.translucent_glass" in (payload.get("changes") or []):
            return (
                f"Updated {title} in the existing chat preview. All cards now use a "
                "translucent glass effect. No files were written or deployed."
            )
        return (
            f"Updated {title} in the existing chat preview. No files were written or deployed."
        )
    return (
        f"Generated {title} as a buffered website preview in chat. No files were written "
        "or deployed."
    )


def deterministic_read_tool(user_message: object) -> Optional[str]:
    """Route only explicit, high-confidence read-only intents.

    Model tool choice remains the general path. This narrow guard prevents an
    explicit exterior-camera observation request from being answered from stale
    context without first rendering the live, server-backed camera card.
    """

    text = str(user_message or "").strip()
    website_intent = website_update_intent(text) or website_generation_intent(text)
    if not text or not _EXTERIOR_CAMERA_SOURCE_RE.search(text):
        return "website_preview_generate" if website_intent else None
    if _EXTERIOR_CAMERA_ACTION_RE.search(text):
        return "exterior_camera_request"
    return "website_preview_generate" if website_intent else None


def image_generation_request(user_message: object) -> Optional[dict[str, Any]]:
    """Return bounded args for an unmistakable request to create visual media."""
    prompt = str(user_message or "").strip()
    if not prompt or not _IMAGE_GENERATION_RE.search(prompt):
        return None
    return {"prompt": prompt[:2000]}


def web_research_request(user_message: object) -> Optional[dict[str, Any]]:
    """Return bounded arguments for explicit or high-confidence current-web reads."""
    # Architectural invariant: conversation subject matter is runtime input only.
    # Persistent changes to policies, capabilities, prompts, routing, or source
    # code require an explicit owner-directed development change.
    text = str(user_message or "").strip()
    if not text:
        return None
    explicit = bool(
        _EXPLICIT_WEB_RESEARCH_RE.search(text)
        or _WEB_SOURCE_REQUEST_RE.search(text)
    )
    temporal = bool(
        _CURRENT_INFORMATION_RE.search(text)
        and _CURRENT_INFORMATION_SUBJECT_RE.search(text)
        and not _SPECIALIZED_CURRENT_LANE_RE.search(text)
    )
    if not explicit and not temporal:
        return None
    query = text[:400].strip()
    return {"query": query, "max_results": 6} if query else None


def web_research_result_is_verified(result: Any) -> bool:
    """Require a completed bounded external search before guarding synthesis."""
    payload = result if isinstance(result, dict) else {}
    sources = payload.get("sources")
    return bool(
        payload.get("ok") is True
        and payload.get("external_network") is True
        and payload.get("source_bounded") is True
        and isinstance(sources, list)
        and sources
    )


def web_research_fallback_summary(result: Any) -> str:
    """Replace a false capability denial after Core already ran live research."""
    payload = result if isinstance(result, dict) else {}
    sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    if sources:
        count = len(sources)
        noun = "source result" if count == 1 else "source results"
        return (
            f"I searched the live public web and found {count} {noun}. "
            "The source-linked research card contains the returned excerpts and links."
        )
    return (
        "I searched the live public web, but the configured providers returned no source "
        "results for that query. That is a search-result limitation, not a lack of web access."
    )

# Tool name -> card type the UI knows how to render.
ARTIFACT_FOR_TOOL = {
    "get_weather": "weather",
    "get_calendar": "calendar",
    "list_tasks": "tasks",
    "add_task": "task_added",
    "update_task_status": "task_updated",
    "system_status": "system_status",
    "list_directory": "directory",
    "search_files": "file_search",
    "web_research_current": "web_research",
    "website_preview_generate": "website_preview",
    "camera_request": "camera_request",
    "exterior_camera_request": "exterior_camera_request",
    "image_generation_status": "image_generation_status",
    "image_generate": "generated_image",
    "video_generation_status": "video_generation_status",
    "video_generate": "generated_video",
    "assistant_capabilities_read": "capabilities",
    "read_file": "file",
    "run_powershell": "shell_result",
    "write_file": "file_written",
    "create_calendar_event": "calendar_event_created",
    # field systems
    "adas_si_search": "adas_si_results",
    "adas_si_open": "adas_si_document",
    "adas_si_inventory": "adas_si_inventory",
    "adas_si_records": "adas_si_records",
    "adas_si_file_write": "file_written",
    "adas_si_record_write": "adas_si_record",
    "adas_si_record_modify": "adas_si_record",
    "calibration_iq_read": "calibration_iq_ros",
    "calibration_iq_summary": "calibration_iq_summary",
    "calibration_iq_ro": "calibration_iq_ro",
    "calibration_iq_status": "calibration_iq_status",
    "calibration_iq_update": "calibration_iq_receipt",
}


def tool_result_for_model(name: str, result: Any) -> Any:
    """Project large display artifacts into a compact, valid model result.

    The full artifact still goes to the chat stream and durable message store.
    In particular, feeding an entire generated HTML document back through a
    12K string slice can create malformed JSON and wastes context the model
    needs for its final explanation.
    """
    if name != "website_preview_generate" or not isinstance(result, dict):
        return result
    projected = {
        key: result.get(key)
        for key in (
            "ok",
            "status",
            "website_id",
            "revision",
            "parent_sha256",
            "changed",
            "changes",
            "title",
            "bytes",
            "sha256",
            "written_to_disk",
            "deployed",
            "preview",
            "message",
        )
        if key in result
    }
    if result.get("ok") is True:
        projected["assistant_instruction"] = (
            "Respond with a short summary only. The inline card already contains the full "
            "website preview and HTML; do not repeat or regenerate the code."
        )
    else:
        projected["assistant_instruction"] = (
            "State that the website preview was not updated and that the previous successful "
            "revision remains unchanged. Do not claim success."
        )
    return projected


def artifact_type_for_tool(name: str, result: Any) -> Optional[str]:
    """Choose media success cards only when result truth is self-consistent."""
    if name not in {"image_generate", "video_generate"}:
        return ARTIFACT_FOR_TOOL.get(name)
    if name == "video_generate":
        if not isinstance(result, dict):
            return "video_generation_status"
        lifecycle = result.get("lifecycle") or {}
        digest = str(result.get("sha256") or "")
        source_digest = str(result.get("source_sha256") or "")
        expected_url = f"/api/generated-videos/{digest}.mp4"
        duration_seconds = result.get("duration_seconds")
        frame_count = result.get("frame_count")
        num_bytes = result.get("bytes")
        width = result.get("width")
        height = result.get("height")
        common_verified = (
            result.get("ok") is True
            and result.get("status") == "completed"
            and result.get("executed") is True
            and result.get("success") is True
            and result.get("actual_video") is True
            and result.get("verified") is True
            and result.get("source_verified") is True
            and result.get("mime_type") == "video/mp4"
            and result.get("codec") == "h264"
            and result.get("pixel_format") == "yuv420p"
            and type(result.get("fps")) is int
            and result.get("fps") == 24
            and type(duration_seconds) is int
            and 2 <= duration_seconds <= 10
            and type(frame_count) is int
            and frame_count == duration_seconds * 24
            and type(num_bytes) is int
            and num_bytes > 0
            and type(width) is int
            and 64 <= width <= 4096
            and width % 2 == 0
            and type(height) is int
            and 64 <= height <= 4096
            and height % 2 == 0
            and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
            and re.fullmatch(r"[0-9a-f]{64}", source_digest) is not None
            and result.get("video_url") == expected_url
            and result.get("target") == expected_url
        )
        mode = result.get("mode")
        procedural_verified = (
            mode == "exact_source_animation"
            and result.get("actual_generation") is False
            and result.get("source_preserved") is True
            and result.get("source_conditioned") is False
            and result.get("provider") == "ffmpeg-exact-local"
            and result.get("render_kind") == "deterministic_exact_source_animation"
            and result.get("profile") == "hover_pulse"
            and lifecycle.get("mode") == "bounded_cpu_subprocess"
            and lifecycle.get("model_remained_available") is True
        )
        seed = result.get("seed")
        expected_wan_assets = {
            "wan2.2_ti2v_5B_fp16.safetensors": (
                9999658848,
                "456f901338bd9eadbded3828b819109a9b68e8a525ca5cf8d0049a69fcfeca1e",
            ),
            "umt5_xxl_fp8_e4m3fn_scaled.safetensors": (
                6735906897,
                "c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68",
            ),
            "wan2.2_vae.safetensors": (
                1409400960,
                "e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156",
            ),
        }
        wan_assets = result.get("model_assets")
        wan_assets_verified = (
            isinstance(wan_assets, dict)
            and set(wan_assets) == set(expected_wan_assets)
            and all(
                isinstance(wan_assets.get(filename), dict)
                and wan_assets[filename].get("verified") is True
                and wan_assets[filename].get("bytes") == expected[0]
                and wan_assets[filename].get("sha256") == expected[1]
                for filename, expected in expected_wan_assets.items()
            )
        )
        generative_verified = (
            mode == "image_to_video"
            and result.get("actual_generation") is True
            and result.get("source_preserved") is False
            and result.get("source_conditioned") is True
            and result.get("provider") == "comfyui-wan2.2-ti2v-5b-local"
            and result.get("render_kind") == "generative_image_to_video"
            and result.get("model_id") == "Wan2.2-TI2V-5B"
            and result.get("width") == 704
            and result.get("height") == 704
            and type(seed) is int
            and 0 <= seed < 2**53
            and re.fullmatch(r"[0-9a-f]{64}", str(result.get("prompt_sha256") or ""))
            is not None
            and lifecycle.get("mode") == "sequential_exclusive"
            and lifecycle.get("model_stopped") is True
            and lifecycle.get("model_restored") is True
            and type(lifecycle.get("gpu_indices")) is list
            and bool(lifecycle.get("gpu_indices"))
            and wan_assets_verified
        )
        verified = common_verified and (procedural_verified or generative_verified)
        return "generated_video" if verified else "video_generation_status"
    if not isinstance(result, dict):
        return "image_generation_status"
    lifecycle = result.get("lifecycle") or {}
    verified = (
        result.get("ok") is True
        and result.get("status") == "completed"
        and result.get("actual_generation") is True
        and result.get("verified") is True
        and result.get("image_url") == result.get("target")
        and lifecycle.get("model_restored") is True
    )
    return "generated_image" if verified else "image_generation_status"


def video_failure_summary(result: Any) -> str:
    """Return fixed, receipt-grounded prose for a failed protected video run."""
    if not isinstance(result, dict):
        return "Video generation failed. No verified playable video is being claimed."

    generation = result.get("generation")
    lifecycle = result.get("lifecycle")
    generation = generation if isinstance(generation, dict) else {}
    lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
    submit_state = generation.get("submit_state")
    may_have_generated = generation.get("may_have_generated")

    if submit_state == "not_attempted" and may_have_generated is False:
        if (
            result.get("stage") == "model_stop_readiness"
            and result.get("retryable") is True
            and lifecycle.get("model_stop_attempted") is True
            and lifecycle.get("model_stopped") is False
        ):
            return (
                "Video generation did not start because Omni's readiness check could not "
                "be completed. No video job was submitted, and Omni was not stopped."
            )
        return (
            "Video generation did not start. No video job was submitted, and no video "
            "is being claimed."
        )

    if submit_state == "indeterminate" or may_have_generated is True:
        if result.get("actual_video") is True and result.get("verified") is True:
            return (
                "A video file was generated, but final lifecycle verification failed. "
                "The receipt preserves that partial result; no playable success card is "
                "being claimed."
            )
        return (
            "Video generation may have begun, but no verified playable result is being "
            "claimed. The receipt records the cleanup and restoration outcome."
        )

    return "Video generation failed. No verified playable video is being claimed."


def image_failure_summary(result: Any) -> str:
    """Return fixed, receipt-grounded prose for a failed protected image run."""
    if not isinstance(result, dict):
        return "Image generation failed. No verified generated image is being claimed."

    if result.get("actual_generation") is True and result.get("verified") is True:
        return (
            "An image file was generated and verified, but final lifecycle or receipt "
            "verification failed. The receipt preserves that partial result; no generated-"
            "image success card is being claimed."
        )
    return "Image generation failed. No verified generated image is being claimed."


class Orchestrator:
    def __init__(self, router, client, registry, store, settings):
        self.router = router
        self.client = client
        self.registry = registry
        self.store = store
        self.settings = settings

    def _persist_website_turn(
        self,
        conversation_id: int,
        summary: str,
        artifacts: list[dict],
        result: dict,
        *,
        update: bool,
    ) -> tuple[int, str, list[dict], dict]:
        final_result = result
        final_summary = summary
        final_artifacts = artifacts
        try:
            if update and result.get("ok") is True and hasattr(
                self.store, "add_website_revision_message"
            ):
                message_id = self.store.add_website_revision_message(
                    conversation_id,
                    summary,
                    worker_used=self.router.active_name,
                    artifacts=artifacts,
                    website_id=str(result.get("website_id") or ""),
                    expected_parent_sha256=str(result.get("parent_sha256") or ""),
                )
            else:
                message_id = self.store.add_message(
                    conversation_id,
                    "assistant",
                    summary,
                    worker_used=self.router.active_name,
                    artifacts=artifacts,
                )
        except WebsiteRevisionConflict:
            final_result = {
                "ok": False,
                "status": "revision_conflict",
                "changed": False,
                "website_id": result.get("website_id"),
                "revision": result.get("revision"),
                "parent_sha256": result.get("parent_sha256"),
                "title": result.get("title"),
                "written_to_disk": False,
                "deployed": False,
                "message": (
                    "The website preview changed in another turn before this edit could be "
                    "saved. The newer revision remains unchanged."
                ),
            }
            final_summary = website_result_summary(final_result, update=True)
            replacement = {"type": "website_preview", "data": final_result}
            final_artifacts = list(artifacts)
            for index in range(len(final_artifacts) - 1, -1, -1):
                if final_artifacts[index].get("type") == "website_preview":
                    final_artifacts[index] = replacement
                    break
            else:
                final_artifacts.append(replacement)
            message_id = self.store.add_message(
                conversation_id,
                "assistant",
                final_summary,
                worker_used=self.router.active_name,
                artifacts=final_artifacts,
            )
            if hasattr(self.store, "audit"):
                self.store.audit(
                    "website_revision_conflict",
                    {
                        "conversation_id": conversation_id,
                        "website_id": result.get("website_id"),
                        "expected_parent_sha256": result.get("parent_sha256"),
                    },
                )
        return message_id, final_summary, final_artifacts, final_result

    @staticmethod
    def _rewrite_website_events(events: list[dict], result: dict) -> list[dict]:
        rewritten: list[dict] = []
        for event in events:
            current = dict(event)
            if current.get("type") == "tool_result":
                current["result"] = tool_result_for_model(
                    "website_preview_generate", result
                )
            elif (
                current.get("type") == "artifact"
                and (current.get("artifact") or {}).get("type") == "website_preview"
            ):
                current["artifact"] = {"type": "website_preview", "data": result}
            rewritten.append(current)
        return rewritten

    async def run_turn(
        self,
        conversation_id: int,
        user_message: str,
        approved_tool: Optional[dict] = None,
        approval_context: Optional[dict] = None,
    ) -> AsyncIterator[dict]:
        """Stream one assistant turn.

        `approved_tool` contains an already executed, receipt-backed result.
        Completed media-generation actions are terminal; other protected-tool
        results may be fed to the model, but the handler is never re-invoked.
        """
        try:
            async for event in self._run(
                conversation_id, user_message, approved_tool, approval_context
            ):
                yield event
        except Exception as exc:  # noqa: BLE001
            log.exception("turn failed")
            yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}

    async def _run(
        self,
        conversation_id: int,
        user_message: str,
        approved_tool: Optional[dict],
        approval_context: Optional[dict],
    ) -> AsyncIterator[dict]:
        history = self.store.get_messages(conversation_id)
        messages = prompt_mod.build_messages(
            self.router, history,
            self.settings.context_tokens,
            self.settings.max_response_tokens,
        )
        tools = self.registry.model_tools()
        artifacts: list[dict] = []
        full_text = ""

        # Qwen's tool choice can occasionally answer an explicit camera request
        # from stale conversational context. Pre-route only this high-confidence
        # read-only intent through the same Registry/_execute boundary used by
        # model-selected calls, so policy, audit, artifact, and chat rendering
        # semantics stay identical.
        base_routed_tool = (
            None if approved_tool else deterministic_read_tool(user_message)
        )
        ciq_request = (
            calibration_iq_read_request(user_message, history)
            if not approved_tool and base_routed_tool is None
            else None
        )
        image_request = (
            image_generation_request(user_message)
            if not approved_tool and base_routed_tool is None and ciq_request is None
            else None
        )
        web_request = (
            web_research_request(user_message)
            if (
                not approved_tool
                and base_routed_tool is None
                and ciq_request is None
                and image_request is None
            )
            else None
        )
        routed_tool = (
            base_routed_tool
            or (ciq_request[0] if ciq_request else None)
            or ("image_generate" if image_request else None)
            or ("web_research_current" if web_request else None)
        )
        advertised_names = {
            str(item.get("function", {}).get("name") or "") for item in tools
        }
        routed_is_website = routed_tool == "website_preview_generate"
        routed_is_website_update = routed_is_website and website_update_intent(
            user_message
        )
        routed_is_ciq = routed_tool in {
            "calibration_iq_summary",
            "calibration_iq_read",
        }
        routed_is_image = routed_tool == "image_generate"
        routed_is_web = routed_tool == "web_research_current"
        routed_result = None
        routed_tier = self.registry.tier(routed_tool) if routed_tool else None
        if (
            routed_tool
            and routed_tool in advertised_names
            and (
                routed_tier == "read_only"
                or (routed_is_image and routed_tier == "confirm_required")
            )
        ):
            if routed_is_ciq and ciq_request:
                routed_args = dict(ciq_request[1])
            elif routed_is_image and image_request:
                routed_args = dict(image_request)
            elif routed_is_web and web_request:
                routed_args = dict(web_request)
            else:
                routed_args = {"prompt": str(user_message or "")[:2000]}
                if routed_is_website_update:
                    routed_args["operation"] = "update_latest"
            routed_call_id = (
                f"routed_{routed_tool}_{int(conversation_id)}_{len(history)}"
            )
            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": routed_call_id,
                    "type": "function",
                    "function": {
                        "name": routed_tool,
                        "arguments": json.dumps(routed_args),
                    },
                }],
            })
            routed_events: list[dict] = []
            routed_paused = False
            async for event in self._execute(
                routed_tool,
                routed_args,
                messages,
                artifacts,
                conversation_id=conversation_id,
                approval_context=approval_context,
                call_id=routed_call_id,
            ):
                if event.get("type") == "approval":
                    routed_paused = True
                if event.get("type") == "tool_result":
                    routed_result = event.get("result")
                if event.get("type") == "artifact":
                    artifact = event.get("artifact") or {}
                    if artifact.get("type") in {
                        "website_preview",
                        "calibration_iq_summary",
                        "calibration_iq_ros",
                    }:
                        routed_result = artifact.get("data")
                if routed_is_website or routed_is_ciq:
                    routed_events.append(event)
                else:
                    yield event
            tools = [
                item
                for item in tools
                if item.get("function", {}).get("name") != routed_tool
            ]

            if routed_paused:
                message_id = self.store.add_message(
                    conversation_id,
                    "assistant",
                    "",
                    worker_used=self.router.active_name,
                    artifacts=artifacts,
                )
                yield {
                    "type": "done",
                    "message_id": message_id,
                    "worker": self.router.active_name,
                    "artifacts": artifacts,
                }
                return

            # Website revisions are already complete, chat-renderable results.
            # Persist them before any optional prose synthesis so a worker
            # disconnect cannot leave a client-only phantom card. A fixed
            # truthful summary also prevents an unreceipted "updated" claim.
            if routed_is_website:
                result = routed_result if isinstance(routed_result, dict) else {}
                summary = website_result_summary(
                    result, update=routed_is_website_update
                )
                message_id, summary, artifacts, result = self._persist_website_turn(
                    conversation_id,
                    summary,
                    artifacts,
                    result,
                    update=routed_is_website_update,
                )
                routed_events = self._rewrite_website_events(routed_events, result)
                full_text = summary
                if len(history) <= 1 and summary:
                    self.store.touch_conversation(
                        conversation_id, title=user_message[:60]
                    )
                # The revision and its fixed truth summary are durable before
                # the first success-bearing event reaches the socket. A client
                # disconnect can therefore reconcile server truth rather than
                # retain a phantom live-only card.
                for routed_event in routed_events:
                    yield routed_event
                yield {"type": "token", "text": summary}
                yield {
                    "type": "done",
                    "message_id": message_id,
                    "worker": self.router.active_name,
                    "artifacts": artifacts,
                }
                return

            if routed_is_ciq:
                result = routed_result if isinstance(routed_result, dict) else {
                    "status": "error",
                    "message": "Calibration IQ returned no usable result.",
                }
                card_type = ARTIFACT_FOR_TOOL[routed_tool]
                matching_artifacts = [
                    artifact
                    for artifact in artifacts
                    if artifact.get("type") == card_type
                ]
                if not matching_artifacts:
                    artifact = {"type": card_type, "data": result}
                    artifacts.append(artifact)
                    routed_events.append({"type": "artifact", "artifact": artifact})
                elif len(matching_artifacts) > 1:
                    # The deterministic lane owns exactly one logical result.
                    keep = matching_artifacts[-1]
                    artifacts[:] = [
                        artifact
                        for artifact in artifacts
                        if artifact.get("type") != card_type or artifact is keep
                    ]
                    routed_events = [
                        event
                        for event in routed_events
                        if event.get("type") != "artifact"
                        or (event.get("artifact") or {}) is keep
                    ]

                summary = calibration_iq_result_summary(
                    result,
                    listing=routed_tool == "calibration_iq_read",
                )
                message_id = self.store.add_message(
                    conversation_id,
                    "assistant",
                    summary,
                    worker_used=self.router.active_name,
                    artifacts=artifacts,
                )
                if len(history) <= 1 and summary:
                    self.store.touch_conversation(
                        conversation_id, title=user_message[:60]
                    )
                # Persist the one result card before emitting it, so reconnects
                # recover the same scope and "Show me those" remains durable.
                for routed_event in routed_events:
                    yield routed_event
                yield {"type": "token", "text": summary}
                yield {
                    "type": "done",
                    "message_id": message_id,
                    "worker": self.router.active_name,
                    "artifacts": artifacts,
                }
                return

        # The approval resolver already executed exactly once and persisted a
        # terminal receipt. Reconstruct the protocol pair without calling the
        # handler again, then let the model report the verified result.
        if approved_tool:
            name = approved_tool["name"]
            args = approved_tool.get("args") or {}
            result = approved_tool.get("result")
            receipt = approved_tool.get("receipt") or {}
            call_id = approved_tool.get("call_id") or "approved_call"
            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": json.dumps(
                    {"result": result, "execution_receipt": receipt}, default=str
                )[:12000],
            })
            approved_events = [{
                "type": "tool_result",
                "name": name,
                "result": result,
                "receipt": receipt,
            }]
            receipt_artifact = {"type": "execution_receipt", "data": receipt}
            artifacts.append(receipt_artifact)
            approved_events.append({"type": "artifact", "artifact": receipt_artifact})
            card_type = artifact_type_for_tool(name, result)
            verified_image = (
                name == "image_generate"
                and card_type == "generated_image"
                and isinstance(result, dict)
                and receipt.get("tool_name") == "image_generate"
                and receipt.get("status") == "succeeded"
                and receipt.get("executed") is True
                and receipt.get("success") is True
                and receipt.get("result") == result
            )
            # A result-shaped success without a matching successful receipt is
            # not a generated-image card. Keep it visible only as failed status.
            if name == "image_generate" and not verified_image:
                card_type = "image_generation_status"
            if card_type and isinstance(result, dict):
                artifact = {"type": card_type, "data": result}
                artifacts.append(artifact)
                approved_events.append({"type": "artifact", "artifact": artifact})

            # Approval-gated media completion seals the turn. In particular,
            # image completion must never return to Qwen where the verified
            # image could be treated as permission to launch image-to-video.
            if name == "image_generate":
                summary = (
                    "The verified generated image is ready in the chat card."
                    if verified_image
                    else image_failure_summary(result)
                )
                message_id = self.store.add_message(
                    conversation_id,
                    "assistant",
                    summary,
                    worker_used=self.router.active_name,
                    artifacts=artifacts,
                )
                if len(history) <= 1:
                    self.store.touch_conversation(
                        conversation_id, title=user_message[:60]
                    )
                for approved_event in approved_events:
                    yield approved_event
                yield {"type": "token", "text": summary}
                yield {
                    "type": "done",
                    "message_id": message_id,
                    "worker": self.router.active_name,
                    "artifacts": artifacts,
                }
                return

            # A verified protected video result is already the full answer.
            # Persist the receipt/card before exposing either, then emit fixed
            # prose so the model cannot invent an absolute media URL or recast
            # one video mode as the other.
            verified_video = (
                name == "video_generate"
                and card_type == "generated_video"
                and isinstance(result, dict)
                and receipt.get("tool_name") == "video_generate"
                and receipt.get("status") == "succeeded"
                and receipt.get("executed") is True
                and receipt.get("success") is True
                and receipt.get("result") == result
            )
            if verified_video:
                video_mode = result.get("mode") if isinstance(result, dict) else None
                summary = (
                    "The verified generative image-to-video clip is ready in the chat card."
                    if video_mode == "image_to_video"
                    else "The verified procedural source animation is ready in the chat card."
                )
                message_id = self.store.add_message(
                    conversation_id,
                    "assistant",
                    summary,
                    worker_used=self.router.active_name,
                    artifacts=artifacts,
                )
                if len(history) <= 1:
                    self.store.touch_conversation(
                        conversation_id, title=user_message[:60]
                    )
                for approved_event in approved_events:
                    yield approved_event
                yield {"type": "token", "text": summary}
                yield {
                    "type": "done",
                    "message_id": message_id,
                    "worker": self.router.active_name,
                    "artifacts": artifacts,
                }
                return

            # A failed protected video result is also the full answer. Persist
            # its receipt/status card before emitting anything and use fixed
            # receipt-grounded prose. A second model call can otherwise invent
            # success or produce a misleading transport-error toast after the
            # authoritative failure has already been recorded.
            failed_video = (
                name == "video_generate"
                and card_type == "video_generation_status"
                and isinstance(result, dict)
                and receipt.get("tool_name") == "video_generate"
                and receipt.get("status") == "failed"
                and receipt.get("success") is False
                and receipt.get("result") == result
            )
            if failed_video:
                summary = video_failure_summary(result)
                message_id = self.store.add_message(
                    conversation_id,
                    "assistant",
                    summary,
                    worker_used=self.router.active_name,
                    artifacts=artifacts,
                )
                if len(history) <= 1:
                    self.store.touch_conversation(
                        conversation_id, title=user_message[:60]
                    )
                for approved_event in approved_events:
                    yield approved_event
                yield {"type": "token", "text": summary}
                yield {
                    "type": "done",
                    "message_id": message_id,
                    "worker": self.router.active_name,
                    "artifacts": artifacts,
                }
                return

            for approved_event in approved_events:
                yield approved_event

        for round_index in range(MAX_TOOL_ROUNDS):
            tool_calls: list[dict] = []
            round_text = ""
            sealed_round_tokens: list[dict] = []

            async for event in self.client.stream(messages, tools=tools):
                if event["type"] == "content":
                    round_text += event["text"]
                    # A model can emit optimistic prose before a tool call in
                    # the same streamed round. Hold round text until the tool
                    # choice is known so a late website call cannot expose a
                    # success claim before its artifact is durably committed.
                    sealed_round_tokens.append(
                        {"type": "token", "text": event["text"]}
                    )
                elif event["type"] == "tool_call":
                    tool_calls.append(event)

            website_call_in_round = any(
                call.get("name") == "website_preview_generate"
                for call in tool_calls
            )
            guarded_web_response = bool(
                routed_is_web
                and not tool_calls
                and (
                    _WEB_ACCESS_DENIAL_RE.search(round_text)
                    or (
                        web_research_result_is_verified(routed_result)
                        and (
                            not round_text.strip()
                            or _WEB_RESEARCH_REFUSAL_RE.search(round_text)
                        )
                    )
                )
            )
            if not website_call_in_round and not guarded_web_response:
                for token_event in sealed_round_tokens:
                    yield token_event

            if guarded_web_response:
                guarded_text = web_research_fallback_summary(routed_result)
                yield {"type": "token", "text": guarded_text}
                full_text += guarded_text
            else:
                full_text += round_text

            if not tool_calls:
                break

            # Record the assistant's tool-call turn so the model sees its own
            # request alongside the result on the next pass.
            messages.append({
                "role": "assistant",
                "content": round_text or "",
                "tool_calls": [
                    {
                        "id": c.get("id") or f"call_{round_index}_{i}",
                        "type": "function",
                        "function": {"name": c["name"], "arguments": c["arguments"]},
                    }
                    for i, c in enumerate(tool_calls)
                ],
            })

            paused = False
            for i, call in enumerate(tool_calls):
                try:
                    args = json.loads(call["arguments"] or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}

                call_id = call.get("id") or f"call_{round_index}_{i}"
                is_website_call = call.get("name") == "website_preview_generate"
                sealed_events: list[dict] = []
                website_result = None
                async for ev in self._execute(call["name"], args, messages,
                                              artifacts, conversation_id=conversation_id,
                                              approval_context=approval_context,
                                              call_id=call_id):
                    if ev["type"] == "approval":
                        paused = True
                    if is_website_call:
                        sealed_events.append(ev)
                        if ev.get("type") == "tool_result":
                            website_result = ev.get("result")
                        if (
                            ev.get("type") == "artifact"
                            and (ev.get("artifact") or {}).get("type")
                            == "website_preview"
                        ):
                            website_result = (ev.get("artifact") or {}).get("data")
                    else:
                        yield ev

                if is_website_call:
                    result = website_result if isinstance(website_result, dict) else {}
                    is_update = (
                        args.get("operation") == "update_latest"
                        or result.get("status") in {
                            "updated_preview", "unchanged_preview", "update_failed"
                        }
                    )
                    summary = website_result_summary(result, update=is_update)
                    message_id, summary, artifacts, result = self._persist_website_turn(
                        conversation_id,
                        summary,
                        artifacts,
                        result,
                        update=is_update,
                    )
                    sealed_events = self._rewrite_website_events(sealed_events, result)
                    full_text = summary
                    if len(history) <= 1 and summary:
                        self.store.touch_conversation(
                            conversation_id, title=user_message[:60]
                        )
                    for sealed_event in sealed_events:
                        yield sealed_event
                    yield {"type": "token", "text": summary}
                    yield {
                        "type": "done",
                        "message_id": message_id,
                        "worker": self.router.active_name,
                        "artifacts": artifacts,
                    }
                    return

            if paused:
                # Stop here. The UI shows the approval card; approving it
                # starts a new turn carrying approved_tool.
                break
        else:
            log.warning("Tool loop hit the %d-round cap", MAX_TOOL_ROUNDS)

        message_id = self.store.add_message(
            conversation_id, "assistant", full_text,
            worker_used=self.router.active_name, artifacts=artifacts,
        )
        if len(history) <= 1 and full_text:
            self.store.touch_conversation(conversation_id, title=user_message[:60])

        yield {
            "type": "done",
            "message_id": message_id,
            "worker": self.router.active_name,
            "artifacts": artifacts,
        }

    async def _execute(
        self,
        name: str,
        args: dict,
        messages: list[dict],
        artifacts: list[dict],
        *,
        conversation_id: int,
        approval_context: Optional[dict],
        call_id: str = "call_0",
    ) -> AsyncIterator[dict]:
        yield {"type": "tool_start", "name": name, "args": args}

        def feed(payload: Any) -> None:
            projected = tool_result_for_model(name, payload)
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": json.dumps(projected, default=str)[:12000],
            })

        try:
            result = await self.registry.invoke(
                name, args,
                message_id=(approval_context or {}).get("message_id"),
                conversation_id=conversation_id,
                tool_call_id=call_id,
            )
        except NeedsApproval as pending:
            context = approval_context or {}
            if not all(context.get(key) for key in ("session_id", "user_id", "message_id")):
                payload = {
                    "status": "blocked",
                    "message": "Protected action identity is incomplete; nothing was run.",
                }
                feed(payload)
                yield {"type": "tool_result", "name": name, "result": payload}
                return
            approval_id = self.store.create_approval(
                name, pending.summary, {"name": name, "args": args},
                conversation_id=conversation_id,
                session_id=str(context["session_id"]),
                user_id=str(context["user_id"]),
                message_id=int(context["message_id"]),
                tool_call_id=call_id,
                logged_args=self.registry.log_args(name, args),
            )
            record = self.store.get_approval(approval_id) or {}
            public_record = self.registry.public_approval(record)
            if record.get("status") in {"succeeded", "failed", "denied", "expired"}:
                receipt = self.store.get_execution_receipt(approval_id)
                replay = {
                    "status": record["status"],
                    "executed": bool((receipt or {}).get("executed")),
                    "success": bool((receipt or {}).get("success")),
                    "result": (receipt or {}).get("result"),
                    "execution_receipt": receipt,
                    "replayed": True,
                }
                feed(replay)
                yield {"type": "tool_result", "name": name, "result": replay,
                       "receipt": receipt}
                if receipt:
                    artifact = {"type": "execution_receipt", "data": receipt}
                    artifacts.append(artifact)
                    yield {"type": "artifact", "artifact": artifact}
                return
            if record.get("status") == "executing":
                payload = {
                    "status": "executing", "executed": False,
                    "message": "This exact protected action is already executing.",
                }
                feed(payload)
                yield {"type": "tool_result", "name": name, "result": payload}
                return
            feed({
                "status": "awaiting_approval",
                "message": (
                    "This action needs Otis's approval before it can run: "
                    f"{public_record.get('summary', 'Protected action')}"
                ),
            })
            approval = {
                "id": approval_id,
                "tool": public_record.get("tool", name),
                "summary": public_record.get("summary", f"Run {name}"),
                "args": public_record.get("args", {}),
                "status": record.get("status", "pending"),
                "action_digest": record.get("action_digest"),
                "idempotency_key": record.get("idempotency_key"),
            }
            artifact = {"type": "approval_request", "data": approval}
            artifacts.append(artifact)
            yield {
                "type": "approval",
                "approval": approval,
            }
            return
        except ToolBlocked as exc:
            feed({"status": "blocked", "message": str(exc)})
            yield {"type": "tool_result", "name": name,
                   "result": {"status": "blocked", "message": str(exc)}}
            return
        except (ToolError, ValueError) as exc:
            feed({"status": "error", "message": str(exc)})
            yield {"type": "tool_result", "name": name,
                   "result": {"status": "error", "message": str(exc)}}
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed", name)
            payload = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
            feed(payload)
            yield {"type": "tool_result", "name": name, "result": payload}
            return

        feed(result)
        event_result = (
            tool_result_for_model(name, result)
            if name == "website_preview_generate"
            else result
        )
        yield {"type": "tool_result", "name": name, "result": event_result}

        card_type = artifact_type_for_tool(name, result)
        if card_type and isinstance(result, dict):
            artifact = {"type": card_type, "data": result}
            artifacts.append(artifact)
            yield {"type": "artifact", "artifact": artifact}
