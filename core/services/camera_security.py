"""The exterior-camera capabilities X offers, answered from Frigate.

The tool names here are unchanged on purpose -- camera_event_history,
camera_footage, camera_snapshot_analyze, exterior_camera_request are what
the model already knows how to reach for, and what routing and prompting
already assume. What changed is underneath: every answer now comes from the
Frigate NVR on its own machine instead of a recorder running on Omega.

The division of labour is the same one the rest of X follows. This module
decides nothing conversational: it retrieves evidence, enforces bounds,
runs the existing vision contract over real pixels, and reports a
structured state. Composing that into an answer stays the model's job, and
when Frigate is unreachable the honest structured state travels outward
rather than a guess about what is happening outside.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone, tzinfo
from typing import Any, Optional
from urllib.parse import urlencode

from . import camera as camera_svc
from . import frigate_surveillance as surveillance_svc
from .frigate_client import FrigateError, FrigateInvalidRequest, FrigateState

log = logging.getLogger("xomni.camera_security")

MAX_HISTORY_ITEMS = 50
DEFAULT_HISTORY_ITEMS = 20

SECURITY_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    # Media-rendering capabilities come before the broad history reader so a
    # request to *see* something meets the rendering contract first. The
    # model still owns intent and argument selection.
    "camera_footage": {
        "description": (
            "Show recorded exterior footage for a time range or a detection, or analyze "
            "what changed across it. For 'what happened' in a bounded window, provide both "
            "bounds and set analysis true even when event history is empty; this samples real "
            "continuous-recording frames rather than relying on detector captions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "Detection id from camera_event_history; omit for the most recent one.",
                },
                "analysis": {
                    "type": "boolean",
                    "description": "True only for a temporal action question.",
                },
                "since": {
                    "type": "string",
                    "description": "ISO start; an explicit offset is preferred, and an offset-less value is interpreted in the operator timezone.",
                },
                "until": {
                    "type": "string",
                    "description": "ISO end with the same offset or operator-local convention.",
                },
                "prompt": {"type": "string", "maxLength": 1000},
            },
            "additionalProperties": False,
        },
    },
    "camera_snapshot_analyze": {
        "description": (
            "Render/analyze one exterior still -- a specific detection's snapshot, or the "
            "camera's current view when event_id is omitted. Required for its image card; "
            "text URLs are insufficient."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "prompt": {"type": "string", "maxLength": 1000},
            },
            "additionalProperties": False,
        },
    },
    "camera_event_history": {
        "description": (
            "List exterior-camera detections the recorder logged, most recent first. Use to "
            "find when an object or configured audio label appeared. An empty list does not "
            "prove nothing happened; for actual scene review use camera_footage with analysis."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "ISO datetime, inclusive lower bound."},
                "until": {"type": "string", "description": "ISO datetime, inclusive upper bound."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_HISTORY_ITEMS},
                "include_recordings": {
                    "type": "boolean",
                    "description": "Include bounded continuous-recording coverage for the same range.",
                },
            },
            "additionalProperties": False,
        },
    },
}

_SECURITY_ANALYSIS_PROMPT = (
    "Reply in exactly this three-line format:\n"
    "PERSON: yes or no\n"
    "VEHICLE: yes or no\n"
    "DESCRIPTION: one plain sentence describing exactly what is visible\n"
    "VEHICLE means any moving or present road/off-road vehicle including car, "
    "truck, SUV, van, motorcycle, ATV, tractor, trailer, or similar vehicle. "
    "Base every line only on the pixels. If neither a person nor vehicle is "
    "visible, report both as no and describe the scene."
)

_FOOTAGE_ANALYSIS_PROMPT = (
    "You are analyzing one contact sheet made from chronological, time-labeled frames "
    "from a continuous exterior recording. Read left-to-right and top-to-bottom; the "
    "first and last frames are intentional before/after evidence. Answer only from changes "
    "visible across those pixels. A sparse sample can establish an observed change, but it "
    "cannot prove that nothing happened between samples.\n"
    "Reply in exactly these seven lines:\n"
    "PERSON: yes, no, or uncertain\n"
    "VEHICLE: yes, no, or uncertain\n"
    "VEHICLE_MOVEMENT: observed, not_observed, or uncertain\n"
    "PERSON_INTERACTION: observed, not_observed, or uncertain\n"
    "SUFFICIENCY: sufficient or insufficient\n"
    "DESCRIPTION: one plain sentence describing the chronological scene\n"
    "EVIDENCE: one plain sentence naming the visible before/during/after change, or why it is insufficient\n"
    "Use observed only when the sampled frames visibly support the temporal claim. Use "
    "not_observed only with SUFFICIENCY: sufficient, and word it as not observed in the "
    "sample rather than proof nothing happened."
)


# ------------------------------------------------------------------ helpers


def _parse_iso(value: object, *, local_timezone: tzinfo = timezone.utc) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_timezone)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _local(value: datetime, local_timezone: Optional[tzinfo] = None) -> str:
    return value.astimezone(local_timezone).strftime("%Y-%m-%d %I:%M:%S %p %Z")


def snapshot_url(event_id: str) -> str:
    return f"/api/camera/event-snapshot.jpg?{urlencode({'event_id': str(event_id)})}"


def latest_frame_url() -> str:
    return "/api/camera/latest.jpg"


def _parse_security_caption(text: str) -> tuple[Optional[bool], Optional[bool], str]:
    """Parse only the exact security response contract.

    Substring matching is unsafe at a security boundary (``PERSON: yes or
    no``, ``PERSON: yesterday``): a decision counts only when all three
    required lines occur exactly once.
    """
    decisions: dict[str, bool] = {}
    descriptions: list[str] = []
    invalid = False
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        decision = re.fullmatch(r"(PERSON|VEHICLE)\s*:\s*(yes|no)", line, re.IGNORECASE)
        if decision:
            name = decision.group(1).upper()
            if name in decisions:
                invalid = True
            decisions[name] = decision.group(2).casefold() == "yes"
            continue
        description = re.fullmatch(r"DESCRIPTION\s*:\s*(.+)", line, re.IGNORECASE)
        if description:
            value = description.group(1).strip()
            if descriptions or not value:
                invalid = True
            if value:
                descriptions.append(value)
            continue
        if line:
            invalid = True
    fallback = descriptions[0] if descriptions else str(text or "").strip()
    if invalid or set(decisions) != {"PERSON", "VEHICLE"} or len(descriptions) != 1:
        return None, None, fallback
    return decisions["PERSON"], decisions["VEHICLE"], descriptions[0]


def _parse_footage_analysis_caption(text: str) -> Optional[dict[str, str]]:
    """Accept only a complete temporal-analysis contract from the vision worker."""
    allowed = {
        "PERSON": {"yes", "no", "uncertain"},
        "VEHICLE": {"yes", "no", "uncertain"},
        "VEHICLE_MOVEMENT": {"observed", "not_observed", "uncertain"},
        "PERSON_INTERACTION": {"observed", "not_observed", "uncertain"},
        "SUFFICIENCY": {"sufficient", "insufficient"},
    }
    values: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.fullmatch(
            r"(PERSON|VEHICLE|VEHICLE_MOVEMENT|PERSON_INTERACTION|SUFFICIENCY|DESCRIPTION|EVIDENCE)\s*:\s*(.+)",
            line,
            re.IGNORECASE,
        )
        if match is None:
            return None
        key, value = match.group(1).upper(), match.group(2).strip()
        if key in values or not value:
            return None
        normalized = value.casefold()
        if key in allowed:
            if normalized not in allowed[key]:
                return None
            values[key] = normalized
        else:
            values[key] = value
    required = set(allowed) | {"DESCRIPTION", "EVIDENCE"}
    if set(values) != required:
        return None
    if values["SUFFICIENCY"] == "insufficient":
        # Never turn sparse samples into a negative action conclusion.
        values["VEHICLE_MOVEMENT"] = "uncertain"
        values["PERSON_INTERACTION"] = "uncertain"
    return values


def _temporal_error(
    message: str,
    *,
    status: str = "no_footage",
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    event: Optional[dict[str, Any]] = None,
    local_timezone: Optional[tzinfo] = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "source": "frigate",
        "analysis_status": status,
        "error": message,
    }
    if since is not None:
        result["analyzed_started_at"] = _iso(since)
        result["started_at_local"] = _local(since, local_timezone)
    if until is not None:
        result["analyzed_ended_at"] = _iso(until)
        result["ended_at_local"] = _local(until, local_timezone)
    if event is not None:
        result["event_id"] = event.get("id")
    return result


async def _vision_ready(router) -> bool:
    if router.supports_vision():
        return True
    try:
        await router.ensure_capability(vision=True)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


# -------------------------------------------------------------------- tools


async def camera_event_history(args: dict, *, surveillance) -> dict[str, Any]:
    """What the recorder actually detected, and whether it is reachable at all."""
    local_timezone = getattr(surveillance, "local_timezone", timezone.utc)
    since = _parse_iso(args.get("since"), local_timezone=local_timezone)
    until = _parse_iso(args.get("until"), local_timezone=local_timezone)
    invalid_bounds = [
        name for name, parsed in (("since", since), ("until", until))
        if str(args.get(name) or "").strip() and parsed is None
    ]
    if invalid_bounds:
        return surveillance_svc.error_result(
            FrigateInvalidRequest(
                f"Invalid ISO camera time: {', '.join(invalid_bounds)}."
            ),
            items=[],
            total_count=0,
            shown_count=0,
        )
    try:
        limit = min(max(int(args.get("limit") or DEFAULT_HISTORY_ITEMS), 1), MAX_HISTORY_ITEMS)
    except (TypeError, ValueError):
        limit = DEFAULT_HISTORY_ITEMS

    status = await surveillance.status()
    result: dict[str, Any] = {
        "source": "frigate",
        "frigate_status": status,
        "frigate_url": status.get("base_url"),
    }
    try:
        items = await surveillance.events(since=since, until=until, limit=limit)
    except FrigateError as exc:
        result.update(surveillance_svc.error_result(exc))
        result["items"] = []
        result["total_count"] = 0
        result["shown_count"] = 0
        return result

    for item in items:
        detection_ids = item.get("detection_ids") or []
        if detection_ids:
            item["snapshot_event_id"] = detection_ids[0]
            item["snapshot_url"] = snapshot_url(detection_ids[0])
        item.setdefault("trigger", "detection")
        item.setdefault("captured_at", item.get("started_at"))
        item.setdefault("captured_at_local", item.get("started_at_local"))

    result.update(
        {
            "ok": True,
            "state": FrigateState.AVAILABLE if items else FrigateState.NO_EVENT_DATA,
            "total_count": len(items),
            "shown_count": len(items),
            "truncated": len(items) >= limit,
            "items": items,
        }
    )
    if not items:
        # "Frigate detected nothing" and "there is no recording" are
        # different facts, and conflating them is how a camera answer
        # becomes a false reassurance.
        result["note"] = surveillance_svc.state_message(FrigateState.NO_EVENT_DATA)

    if args.get("include_recordings"):
        try:
            result["recordings"] = await surveillance.recordings(
                since=since, until=until, limit=surveillance_svc.MAX_RECORDING_SPANS
            )
        except FrigateError as exc:
            result["recordings"] = []
            result["recordings_error"] = surveillance_svc.state_message(exc.state)
    return result


async def camera_footage_analyze(router, args: dict, *, surveillance) -> dict[str, Any]:
    """Answer temporal security questions from real recorded frames, never captions."""
    event: Optional[dict[str, Any]] = None
    raw_event_id = args.get("event_id")
    local_timezone = getattr(surveillance, "local_timezone", timezone.utc)
    requested_since = _parse_iso(args.get("since"), local_timezone=local_timezone)
    requested_until = _parse_iso(args.get("until"), local_timezone=local_timezone)

    for name, parsed in (("since", requested_since), ("until", requested_until)):
        if str(args.get(name) or "").strip() and parsed is None:
            return _temporal_error(
                f"{name} must be an ISO datetime.", status="invalid_request"
            )

    if (requested_since is None) != (requested_until is None):
        return _temporal_error(
            "Both since and until are required for a temporal-analysis range.",
            status="invalid_request",
        )

    try:
        if raw_event_id is not None:
            since, until, event = await surveillance.event_window(str(raw_event_id))
        elif requested_since is not None and requested_until is not None:
            since, until = requested_since, requested_until
        else:
            recent = await surveillance.events(limit=1)
            if not recent:
                return _temporal_error(
                    surveillance_svc.state_message(FrigateState.NO_EVENT_DATA),
                    status="missing_event",
                )
            since, until, event = await surveillance.event_window(recent[0]["id"])
    except FrigateError as exc:
        return _temporal_error(
            surveillance_svc.state_message(exc.state, str(exc)), status=exc.state
        )

    if until <= since:
        return _temporal_error(
            "Analysis end time must be after start time.", status="invalid_request", event=event
        )
    duration = (until - since).total_seconds()
    if duration > surveillance_svc.MAX_FOOTAGE_ANALYSIS_DURATION_SECONDS:
        limit_minutes = surveillance_svc.MAX_FOOTAGE_ANALYSIS_DURATION_SECONDS // 60
        return _temporal_error(
            f"That range is too long to examine directly; bounded analysis covers about "
            f"{limit_minutes} minutes at a time. Ask about a specific few-minute window and "
            "the continuous recording can be checked directly, detection or not.",
            status="range_too_broad",
            since=since,
            until=until,
        )

    try:
        samples = await surveillance.analysis_samples(since, until)
    except asyncio.CancelledError:
        raise
    except FrigateError as exc:
        return _temporal_error(
            surveillance_svc.state_message(exc.state, str(exc)),
            status=exc.state,
            since=since,
            until=until,
            event=event,
        )
    except surveillance_svc.FootagePreparationError:
        return _temporal_error(
            "Frame extraction could not complete promptly; no temporal conclusion was made.",
            status="frame_extraction_failed",
            since=since,
            until=until,
            event=event,
        )
    except Exception:
        log.warning("temporal frame extraction failed", exc_info=True)
        return _temporal_error(
            "Frame extraction failed; no temporal conclusion was made.",
            status="frame_extraction_failed",
            since=since,
            until=until,
            event=event,
        )

    try:
        contact_sheet = camera_svc.validate_camera_frame(
            bytes(samples["contact_sheet"]), "image/jpeg"
        )
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("temporal contact sheet was invalid: %s", exc)
        return _temporal_error(
            "Analysis frames could not be validated; no temporal conclusion was made.",
            status="frame_validation_failed",
            since=since,
            until=until,
            event=event,
        )

    question = str(args.get("prompt") or "").strip()
    if question:
        try:
            question = camera_svc.camera_prompt(question)
        except ValueError as exc:
            return _temporal_error(
                str(exc), status="invalid_request", since=since, until=until, event=event
            )
    prompt = _FOOTAGE_ANALYSIS_PROMPT + (
        f"\nOperator temporal question: {question}" if question else ""
    )
    if not await _vision_ready(router):
        return _temporal_error(
            "A vision-capable Omni worker could not be made available for footage analysis.",
            status="vision_unavailable",
            since=since,
            until=until,
            event=event,
        )
    try:
        raw_caption = await camera_svc.caption_frame(router, contact_sheet, prompt)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("temporal vision analysis failed", exc_info=True)
        return _temporal_error(
            "The frames were extracted, but Omni could not analyze them; no temporal conclusion was made.",
            status="vision_failed",
            since=since,
            until=until,
            event=event,
        )
    parsed = _parse_footage_analysis_caption(raw_caption)
    if parsed is None:
        log.warning("temporal vision response did not match its evidence contract")
        return _temporal_error(
            "Omni did not return a complete temporal-evidence result; no temporal conclusion was made.",
            status="unstructured_vision_result",
            since=since,
            until=until,
            event=event,
        )

    clip_url: Optional[str] = None
    playback_error: Optional[str] = None
    try:
        playback = await surveillance.playback(since, until)
        clip_url = playback["clip_url"]
    except FrigateError as exc:
        playback_error = (
            "The analyzed frames are available, but playable footage is not: "
            + surveillance_svc.state_message(exc.state)
        )
    except Exception:
        log.info("temporal analysis playback was unavailable", exc_info=True)
        playback_error = "The analyzed frames are available, but playable footage is unavailable."

    def detected(value: str) -> Optional[bool]:
        return True if value == "yes" else False if value == "no" else None

    movement = parsed["VEHICLE_MOVEMENT"]
    interaction = parsed["PERSON_INTERACTION"]
    sufficient = parsed["SUFFICIENCY"] == "sufficient"
    result: dict[str, Any] = {
        "ok": True,
        "state": FrigateState.AVAILABLE,
        "analysis_status": "sufficient" if sufficient else "insufficient_evidence",
        "source": "frigate",
        "analyzed_started_at": samples["analyzed_started_at"],
        "analyzed_ended_at": samples["analyzed_ended_at"],
        "started_at_local": _local(since, local_timezone),
        "ended_at_local": _local(until, local_timezone),
        "sample_count": int(samples["sample_count"]),
        "sampled_at": list(samples["sampled_at"]),
        "source_segments": list(samples["source_segments"]),
        "person_detected": detected(parsed["PERSON"]),
        "vehicle_detected": detected(parsed["VEHICLE"]),
        "vehicle_movement_observation": movement,
        "person_interaction_observation": interaction,
        # Only an observed positive becomes a boolean conclusion. Absence in
        # sparse samples stays the explicit enum.
        "vehicle_movement_observed": True if movement == "observed" else None,
        "person_interaction_observed": True if interaction == "observed" else None,
        "description": parsed["DESCRIPTION"],
        "evidence": parsed["EVIDENCE"],
        "contact_sheet_sha256": contact_sheet.sha256,
        "clip_url": clip_url,
        "playback_error": playback_error,
    }
    if event is not None:
        result["event_id"] = event.get("id")
    return result


async def camera_motion_clip(args: dict, *, surveillance) -> dict[str, Any]:
    """Playable recorded footage for a detection or an explicit time range."""
    local_timezone = getattr(surveillance, "local_timezone", timezone.utc)
    since = _parse_iso(args.get("since"), local_timezone=local_timezone)
    until = _parse_iso(args.get("until"), local_timezone=local_timezone)
    for name, parsed in (("since", since), ("until", until)):
        if str(args.get(name) or "").strip() and parsed is None:
            return surveillance_svc.error_result(
                FrigateInvalidRequest(f"{name} must be an ISO datetime.")
            )
    raw_event_id = args.get("event_id")
    event: Optional[dict[str, Any]] = None

    try:
        if since or until:
            if since is None or until is None:
                return {
                    "ok": False,
                    "source": "frigate",
                    "state": FrigateState.INVALID_REQUEST,
                    "error": "Both since and until are required for time-range playback.",
                }
            if (until - since).total_seconds() > surveillance_svc.MAX_TOOL_PLAYBACK_DURATION_SECONDS:
                minutes = surveillance_svc.MAX_TOOL_PLAYBACK_DURATION_SECONDS // 60
                return {
                    "ok": False,
                    "source": "frigate",
                    "state": FrigateState.INVALID_REQUEST,
                    "error": (
                        f"Playback is limited to {minutes} minutes. "
                        "Pick a detection or a shorter time range."
                    ),
                }
        elif raw_event_id is not None:
            since, until, event = await surveillance.event_window(str(raw_event_id))
        else:
            recent = await surveillance.events(limit=1)
            if not recent:
                return {
                    "ok": False,
                    "source": "frigate",
                    "state": FrigateState.NO_EVENT_DATA,
                    "error": surveillance_svc.state_message(FrigateState.NO_EVENT_DATA),
                }
            since, until, event = await surveillance.event_window(recent[0]["id"])
        playback = await surveillance.playback(since, until)
    except FrigateError as exc:
        return surveillance_svc.error_result(exc)

    result: dict[str, Any] = {
        "ok": True,
        "source": "frigate",
        "state": FrigateState.AVAILABLE,
        "clip_url": playback["clip_url"],
        "started_at_local": playback["started_at_local"],
        "ended_at_local": playback["ended_at_local"],
        "requested_started_at_local": _local(since, local_timezone),
        "requested_ended_at_local": _local(until, local_timezone),
        "partial": playback["partial"],
    }
    if event is not None:
        result["event_id"] = event.get("id")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        labels = [str(label) for label in (data.get("objects") or []) if label]
        if labels:
            result["labels"] = labels
    return result


async def camera_snapshot_analyze(router, args: dict, *, surveillance) -> dict[str, Any]:
    """One still -- a detection's snapshot, or the camera's current view."""
    raw_event_id = args.get("event_id")
    captured_at = datetime.now(timezone.utc)
    try:
        if raw_event_id is not None:
            event_id = str(raw_event_id)
            since, _until, event = await surveillance.event_window(event_id)
            captured_at = since
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            detections = [str(value) for value in (data.get("detections") or []) if value]
            if not detections:
                raise FrigateInvalidRequest(
                    "That Frigate review item has no object snapshot; it may be motion or audio only."
                )
            snapshot_event_id = detections[0]
            frame_bytes = await surveillance.event_snapshot(snapshot_event_id)
            image_url = snapshot_url(snapshot_event_id)
            trigger = "detection"
        else:
            event_id = None
            event = None
            frame_bytes = await surveillance.latest_frame()
            image_url = latest_frame_url()
            trigger = "current_view"
    except FrigateError as exc:
        return surveillance_svc.error_result(exc)

    try:
        frame = camera_svc.validate_camera_frame(bytes(frame_bytes), "image/jpeg")
    except (TypeError, ValueError) as exc:
        return {
            "ok": False,
            "source": "frigate",
            "state": FrigateState.UNAVAILABLE,
            "error": f"The camera image could not be validated: {exc}",
        }

    custom_prompt = str(args.get("prompt") or "").strip()
    if custom_prompt:
        try:
            custom_prompt = camera_svc.camera_prompt(custom_prompt)
        except ValueError as exc:
            return {
                "ok": False,
                "source": "frigate",
                "state": FrigateState.INVALID_REQUEST,
                "error": str(exc),
            }
    prompt = _SECURITY_ANALYSIS_PROMPT + (
        f"\nOperator question: {custom_prompt}" if custom_prompt else ""
    )
    if not await _vision_ready(router):
        return {
            "ok": False,
            "source": "frigate",
            "state": FrigateState.UNAVAILABLE,
            "error": "A vision-capable Omni worker could not be made available.",
            "snapshot_url": image_url,
        }
    try:
        raw_caption = await camera_svc.caption_frame(router, frame, prompt)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("camera still analysis failed", exc_info=True)
        return {
            "ok": False,
            "source": "frigate",
            "state": FrigateState.UNAVAILABLE,
            "error": "The image was retrieved, but Omni could not analyze it.",
            "snapshot_url": image_url,
        }
    person, vehicle, description = _parse_security_caption(raw_caption)
    result: dict[str, Any] = {
        "ok": True,
        "source": "frigate",
        "state": FrigateState.AVAILABLE,
        "trigger": trigger,
        "captured_at": _iso(captured_at),
        "captured_at_local": _local(
            captured_at, getattr(surveillance, "local_timezone", None)
        ),
        "caption": description,
        "person_detected": person,
        "vehicle_detected": vehicle,
        "snapshot_url": image_url,
        "frame_sha256": frame.sha256,
    }
    if event_id is not None:
        result["id"] = event_id
        result["event_id"] = event_id
        result["snapshot_event_id"] = snapshot_event_id
    return result


async def exterior_camera_look(router, args: dict, *, surveillance) -> dict[str, Any]:
    """"Look outside" -- the camera's current frame, described from its pixels.

    Kept under the existing exterior_camera_request tool name. It no longer
    asks the browser to open a proxied stream: Frigate already holds the
    current frame, so X fetches it, looks at it, and answers.
    """
    result = await camera_snapshot_analyze(router, {"prompt": args.get("prompt")}, surveillance=surveillance)
    result["camera_source_id"] = "exterior"
    result["live_frame_url"] = latest_frame_url()
    status = await surveillance.status()
    result["frigate_url"] = status.get("base_url")
    result.setdefault("state", status.get("state"))
    return result


__all__ = [
    "MAX_HISTORY_ITEMS",
    "SECURITY_TOOL_SCHEMAS",
    "camera_event_history",
    "camera_footage_analyze",
    "camera_motion_clip",
    "camera_snapshot_analyze",
    "exterior_camera_look",
    "latest_frame_url",
    "snapshot_url",
]
