"""Calibration IQ work-prep bridge.

Calibration IQ owns the work queue. ADAS Map, when present on a repair order,
is the governing calibration-requirement source. ADAS SI owns procedure
availability. This module joins those existing contracts without creating a
second queue or teaching the model to guess at job scope.

High-confidence conversational workflows are handled directly:
* "check what cars are in phase five" -> Calibration IQ phase list
* "what does RO ... need/have" -> the RO's saved CIQ calibration requirements
* "make sure we're prepared for the week" -> active CIQ queue + ADAS Map
  reconciliation + exact ADAS SI coverage report
* "collect/retrieve ADAS Quick Reference" -> the currently selected ALLDATA
  vehicle is resolved to exactly one active CIQ RO before the existing low-rate
  Quick Reference collector is allowed to save anything
* "log in to ALLDATA" -> the existing inline licensed-browser card, with a
  truthful fixed instruction instead of model-authored desktop-browser advice

Requirement reconciliation is routine operator work. If a machine-readable
ADAS Map on an RO contains a calibration missing from CIQ, X uses CIQ's own
add_calibration/update_calibration operator actions, then re-reads the RO before
checking ADAS SI. No calibration is invented from ADAS SI or ALLDATA.
"""

from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from . import calibration_iq
from . import research_alldata_navigation as nav
from . import research_alldata_quick_reference as quick
from . import research_operator

TOOL_NAME = "calibration_iq_work_prep"
_CONTEXT_KEY = "__xomni_work_prep_context"
_INSTALL_LOCK = threading.Lock()
_INSTALLED = False

_ACTIVE_DETERMINATIONS = {"REQUIRED", "LIKELY_REQUIRED"}
_METHODS = {"STATIC", "DYNAMIC", "BOTH", "INSPECTION_ONLY", "UNKNOWN"}

_RO_RE = re.compile(
    r"\b(?:repair\s+order|ro)\s*(?:number|no\.?|#|id)?\s*[:#]?\s*[`\"']?"
    r"(?P<identifier>"
    r"XOP-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
    r"(?=[A-Za-z0-9-]{5,64}\b)(?=[A-Za-z0-9-]*\d)"
    r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*"
    r")\b",
    re.IGNORECASE,
)
_PHASE_RE = re.compile(
    r"\bphase\s*(?:number\s*)?"
    r"(?P<phase>\d{1,2}|zero|one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.IGNORECASE,
)
_PHASE_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10",
}
_SHOPS = (
    (re.compile(r"\bwarner\s+robins\b", re.IGNORECASE), "Warner Robins"),
    (re.compile(r"\bmacon\b", re.IGNORECASE), "Macon"),
    (re.compile(r"\bperry\b", re.IGNORECASE), "Perry"),
)
_QUICK_REFERENCE_RE = re.compile(
    r"\badas\s+quick\s+reference\b|\bquick\s+reference\b.{0,60}\badas\b",
    re.IGNORECASE | re.DOTALL,
)
_QUICK_ACTION_RE = re.compile(
    r"\b(?:collect|retrieve|pull|capture|save|load|import|get|grab|download)\b",
    re.IGNORECASE,
)
_WEEK_READY_RE = re.compile(
    r"\b(?:prepared|prepare|prep|ready|readiness)\b.{0,90}"
    r"\b(?:week|queue|work|cars?|vehicles?|repair\s+orders?|si|adas)\b|"
    r"\b(?:what|which)\b.{0,60}\b(?:adas\s+si|si)\b.{0,60}\b(?:missing|need|needed)\b|"
    r"\bmake\s+sure\b.{0,80}\bprepared\b",
    re.IGNORECASE | re.DOTALL,
)
_PHASE_LIST_RE = re.compile(
    r"\b(?:check|what|which|show|list|display|see|pull\s+up)\b.{0,100}"
    r"\b(?:cars?|vehicles?|repair\s+orders?|ros?)\b|"
    r"\b(?:cars?|vehicles?|repair\s+orders?|ros?)\b.{0,100}"
    r"\b(?:in|at|on)\s+phase\b",
    re.IGNORECASE | re.DOTALL,
)
_RO_REQUIREMENT_RE = re.compile(
    r"\b(?:what|which|tell\s+me|show|check)\b.{0,100}"
    r"\b(?:calibrations?|requirements?|needs?|has|have)\b|"
    r"\b(?:what\s+does|what's|whats)\b.{0,80}\b(?:ro|repair\s+order|this)\b",
    re.IGNORECASE | re.DOTALL,
)
_THIS_RO_RE = re.compile(r"\b(?:this|that|current)\s+(?:ro|repair\s+order)\b", re.IGNORECASE)
_ALLDATA_ACCESS_RE = re.compile(
    r"\b(?:log\s*in|login|open|resume|start|set\s*up|setup)\b.{0,50}\ball\s*data\b|"
    r"\ball\s*data\b.{0,50}\b(?:log\s*in|login|open|resume|start|setup)\b",
    re.IGNORECASE | re.DOTALL,
)
_ADAS_MAP_MARKER_RE = re.compile(r"\badas\s*[-_ ]?map\b|\badasmap\b", re.IGNORECASE)
_REQUIREMENT_KEY_RE = re.compile(
    r"calibrat|requirement|required|system|service|operation|procedure|aim|align|reset|relearn|initial",
    re.IGNORECASE,
)
_CALIBRATION_LABEL_RE = re.compile(
    r"\b(?:camera|radar|blind\s*spot|\bbsm\b|\bbsd\b|steering\s*angle|occupant|ocs|"
    r"parking|park\s*assist|ultrasonic|sonar|lane|collision|cruise|sensor|around\s*view|"
    r"surround\s*view|360)\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def classify_request(text: object) -> Optional[str]:
    value = str(text or "").strip()
    if not value:
        return None
    if _ALLDATA_ACCESS_RE.search(value) and not _QUICK_REFERENCE_RE.search(value):
        return "alldata_access"
    if _QUICK_REFERENCE_RE.search(value) and _QUICK_ACTION_RE.search(value):
        return "quick_reference"
    if _WEEK_READY_RE.search(value):
        return "week_readiness"
    if _PHASE_RE.search(value) and _PHASE_LIST_RE.search(value):
        return "phase_list"
    if (_RO_RE.search(value) or _THIS_RO_RE.search(value)) and _RO_REQUIREMENT_RE.search(value):
        return "ro_requirements"
    return None


def _phase(text: object) -> Optional[str]:
    match = _PHASE_RE.search(str(text or ""))
    if not match:
        return None
    token = match.group("phase").casefold()
    return _PHASE_WORDS.get(token, str(int(token)))


def _shop(text: object) -> Optional[str]:
    value = str(text or "")
    for pattern, label in _SHOPS:
        if pattern.search(value):
            return label
    return None


def _explicit_ro(text: object) -> Optional[str]:
    match = _RO_RE.search(str(text or ""))
    return match.group("identifier") if match else None


def _latest_ro_identifier(history: list[dict[str, Any]]) -> Optional[str]:
    for message in reversed(history or []):
        artifacts = message.get("artifacts") or []
        if isinstance(artifacts, list):
            for artifact in reversed(artifacts):
                if not isinstance(artifact, dict):
                    continue
                data = artifact.get("data")
                if not isinstance(data, dict):
                    continue
                if artifact.get("type") == "calibration_iq_ro":
                    ro = data.get("repair_order")
                    if isinstance(ro, dict):
                        ident = ro.get("id") or ro.get("RO") or ro.get("ro_number")
                        if ident:
                            return str(ident)
                if artifact.get("type") == "calibration_iq_ros":
                    rows = data.get("rows")
                    if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict):
                        ident = rows[0].get("id") or rows[0].get("RO")
                        if ident:
                            return str(ident)
        text = str(message.get("content") or message.get("text") or "")
        ident = _explicit_ro(text)
        if ident:
            return ident
    return None


def _plain(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _calibration_key(value: object) -> str:
    text = str(value or "").casefold()
    replacements = (
        (r"\bblind\s*spot(?:\s+monitor(?:ing)?)?\b|\bbsm\b|\bbsd\b", " bsm "),
        (r"\bsteering\s*angle(?:\s+sensor)?\b", " steeringangle "),
        (r"\b(?:forward(?:\s*facing)?|front|windshield|lane)\s+camera\b", " frontcamera "),
        (r"\b(?:millimeter[-\s]*wave|forward|front|adaptive\s+cruise)\s+radar\b", " frontradar "),
        (r"\b(?:rear\s*view|rear)\s+camera\b", " rearcamera "),
        (r"\b(?:surround|around)\s+view\b|\b360\s+camera\b", " surroundcamera "),
        (r"\boccupant\s+classification(?:\s+system)?\b|\bocs\b", " ocs "),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(?:calibration|calibrate|recalibration|recalibrate|aiming|aim|alignment|align|"
        r"adjustment|adjust|initialization|initialize|relearn|reset|setup|procedure|system|sensor)\b",
        " ",
        text,
    )
    return _plain(text)


def _method(value: object) -> str:
    text = str(value or "").strip().upper().replace(" ", "_")
    if text in _METHODS:
        return text
    folded = str(value or "").casefold()
    if "static" in folded and "dynamic" in folded:
        return "BOTH"
    if "static" in folded:
        return "STATIC"
    if "dynamic" in folded:
        return "DYNAMIC"
    if "inspection" in folded:
        return "INSPECTION_ONLY"
    return "UNKNOWN"


def _looks_like_calibration_label(value: object) -> bool:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return bool(3 <= len(text) <= 160 and _CALIBRATION_LABEL_RE.search(text))


def _requirement_from_value(value: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if isinstance(value, dict):
        required_flag = value.get("required")
        determination = str(value.get("determination") or "").upper()
        if required_flag is False or determination in {"NOT_REQUIRED", "REMOVED_AFTER_REVIEW"}:
            return []
        label = next(
            (
                str(value.get(key) or "").strip()
                for key in ("calibration_type", "calibration", "label", "name", "title", "system", "procedure")
                if str(value.get(key) or "").strip()
            ),
            "",
        )
        if _looks_like_calibration_label(label):
            out.append({"label": label, "method": _method(value.get("method"))})
        return out
    if isinstance(value, list):
        for item in value:
            out.extend(_requirement_from_value(item))
        return out
    if isinstance(value, str):
        pieces = re.split(r"[\n\r;,|]+", value)
        for piece in pieces:
            label = re.sub(r"^[\s\-*•\d.)]+", "", piece).strip()
            if _looks_like_calibration_label(label):
                out.append({"label": label[:160], "method": _method(label)})
    return out


def _node_has_adas_map_marker(value: Any, path: tuple[str, ...]) -> bool:
    if any(_ADAS_MAP_MARKER_RE.search(part.replace("_", " ")) for part in path):
        return True
    if not isinstance(value, dict):
        return False
    for key in ("provider", "source", "source_name", "title", "document_type", "name", "label"):
        raw = value.get(key)
        if isinstance(raw, str) and _ADAS_MAP_MARKER_RE.search(raw):
            return True
    return any(_ADAS_MAP_MARKER_RE.search(str(key).replace("_", " ")) for key in value)


def _source_url(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for key in ("url", "href", "source_uri", "source_url", "link"):
            raw = value.get(key)
            if isinstance(raw, str):
                match = _URL_RE.search(raw)
                if match:
                    return match.group(0)[:2000]
        for item in value.values():
            found = _source_url(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _source_url(item)
            if found:
                return found
    return None


def extract_adas_map(snapshot: Any) -> dict[str, Any]:
    """Find machine-readable ADAS Map requirements without treating CIQ as the source.

    The exact upstream payload has changed over time, so this deliberately keys
    off an explicit ADAS Map marker and then accepts common requirement shapes.
    No unmarked assessment/ADAS text is promoted to governing-source truth.
    """
    requirements: dict[str, dict[str, str]] = {}
    sources: list[dict[str, Any]] = []

    def walk(value: Any, path: tuple[str, ...] = (), inherited: bool = False) -> None:
        marked = inherited or _node_has_adas_map_marker(value, path)
        if marked:
            if isinstance(value, dict):
                url = _source_url(value)
                source = {
                    "path": ".".join(path) or "root",
                    "url": url,
                    "title": next(
                        (
                            str(value.get(key) or "").strip()
                            for key in ("title", "name", "source_name", "provider", "source")
                            if str(value.get(key) or "").strip()
                        ),
                        "ADAS Map",
                    ),
                }
                if source not in sources:
                    sources.append(source)
                for key, item in value.items():
                    if _REQUIREMENT_KEY_RE.search(str(key)):
                        for requirement in _requirement_from_value(item):
                            requirements.setdefault(_calibration_key(requirement["label"]), requirement)
            elif path and _ADAS_MAP_MARKER_RE.search(path[-1].replace("_", " ")):
                for requirement in _requirement_from_value(value):
                    requirements.setdefault(_calibration_key(requirement["label"]), requirement)
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, (*path, str(key)), marked)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, (*path, str(index)), marked)

    walk(snapshot)
    cleaned = [item for key, item in requirements.items() if key]
    return {
        "status": "verified" if cleaned else ("present_unparsed" if sources else "not_found"),
        "governing_source": "ADAS Map",
        "requirements": cleaned,
        "sources": sources[:10],
        "requirement_count": len(cleaned),
    }


def _ciq_calibrations(snapshot: Any) -> list[dict[str, Any]]:
    raw = snapshot.get("calibrations") if isinstance(snapshot, dict) else []
    return [dict(item) for item in (raw or []) if isinstance(item, dict)]


def _active_ciq_requirements(snapshot: Any) -> list[dict[str, Any]]:
    return [
        item for item in _ciq_calibrations(snapshot)
        if str(item.get("determination") or "").upper() in _ACTIVE_DETERMINATIONS
        and str(item.get("calibration_type") or "").strip()
    ]


def build_reconciliation_actions(snapshot: dict[str, Any], map_info: dict[str, Any], ro_id: str) -> list[dict[str, Any]]:
    existing = _ciq_calibrations(snapshot)
    by_key: dict[str, dict[str, Any]] = {}
    for item in existing:
        key = _calibration_key(item.get("calibration_type"))
        if key:
            by_key.setdefault(key, item)

    actions: list[dict[str, Any]] = []
    for required in map_info.get("requirements") or []:
        if not isinstance(required, dict):
            continue
        label = str(required.get("label") or "").strip()[:160]
        key = _calibration_key(label)
        if not key:
            continue
        found = by_key.get(key)
        if found is None:
            actions.append({
                "operation": "add_calibration",
                "repair_order_id": ro_id,
                "arguments": {
                    "calibration_type": label,
                    "determination": "REQUIRED",
                    "method": _method(required.get("method")),
                    "notes": "Added by X from governing ADAS Map during SI readiness reconciliation.",
                    "research_status": "ADAS Map governing source",
                },
            })
            continue
        if str(found.get("determination") or "").upper() in _ACTIVE_DETERMINATIONS:
            continue
        item_id = str(found.get("id") or "").strip()
        version = found.get("version")
        if not item_id or isinstance(version, bool) or not isinstance(version, int) or version < 1:
            continue
        changes: dict[str, Any] = {
            "determination": "REQUIRED",
            "research_status": "ADAS Map governing source",
        }
        map_method = _method(required.get("method"))
        if map_method != "UNKNOWN":
            changes["method"] = map_method
        actions.append({
            "operation": "update_calibration",
            "target_id": item_id,
            "expected_version": version,
            "arguments": changes,
        })
    return actions


def _vehicle_label(snapshot: dict[str, Any], fallback: str = "") -> str:
    vehicle = snapshot.get("vehicle") if isinstance(snapshot.get("vehicle"), dict) else {}
    parts = [vehicle.get("year"), vehicle.get("make"), vehicle.get("model"), vehicle.get("trim")]
    label = " ".join(str(item) for item in parts if item not in (None, "")).strip()
    return label or fallback


def _ro_number(snapshot: dict[str, Any], fallback: str = "") -> str:
    ro = snapshot.get("repair_order") if isinstance(snapshot.get("repair_order"), dict) else {}
    for key in ("ro_number", "roNumber", "number", "ro"):
        value = ro.get(key)
        if value not in (None, ""):
            return str(value)
    return fallback


def _requirement_label(item: dict[str, Any]) -> str:
    return str(item.get("calibration_type") or item.get("label") or "").strip()


async def _adas_coverage(adas: Any, vehicle: str, requirements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    async def one(item: dict[str, Any]) -> dict[str, Any]:
        label = _requirement_label(item)
        query = f"{vehicle} {label}".strip()
        try:
            result = await asyncio.to_thread(adas.search, {"query": query})
        except Exception as exc:  # noqa: BLE001
            return {"calibration": label, "query": query, "available": False, "reason": type(exc).__name__}
        payload = result if isinstance(result, dict) else {}
        exact = payload.get("exact_source_matched") is True
        hits = [
            hit for hit in (payload.get("results") or [])
            if isinstance(hit, dict)
            and str(hit.get("excerpt") or "").strip()
            and int(hit.get("source_match_score") or 0) >= 10
        ]
        return {
            "calibration": label,
            "query": query,
            "available": bool(exact and hits),
            "documents": sorted({str(hit.get("relative_path") or hit.get("source") or "") for hit in hits if hit}),
            "reason": None if exact and hits else str(payload.get("message") or "No exact vehicle procedure is indexed in ADAS SI."),
        }

    return await asyncio.gather(*(one(item) for item in requirements)) if requirements else []


def _valid_context(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    conversation_id = value.get("conversation_id")
    message_id = value.get("message_id")
    tool_call_id = str(value.get("tool_call_id") or "").strip()
    if (
        isinstance(conversation_id, bool) or not isinstance(conversation_id, int) or conversation_id <= 0
        or isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0
        or not tool_call_id
    ):
        return None
    return {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "tool_call_id": tool_call_id,
        "user_id": str(value.get("user_id") or "local-dev"),
        "role": str(value.get("role") or "owner"),
    }


async def _reconcile_one(
    settings: Any,
    adas: Any,
    snapshot: dict[str, Any],
    map_info: dict[str, Any],
    context: Optional[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], Optional[dict[str, Any]]]:
    ro_id = str(calibration_iq._authoritative_repair_order_id(snapshot) or "").strip()  # noqa: SLF001
    if not ro_id or map_info.get("status") != "verified":
        return snapshot, [], None
    actions = build_reconciliation_actions(snapshot, map_info, ro_id)
    if not actions:
        return snapshot, [], None
    if context is None:
        return snapshot, actions, {
            "status": "context_missing",
            "success": False,
            "verified": False,
            "message": "X could identify missing CIQ requirements from ADAS Map but the authoritative turn identity was unavailable, so nothing was changed.",
        }
    nested_context = {
        **context,
        "tool_call_id": f"{context['tool_call_id']}-adas-map-{ro_id[:12]}",
    }
    result = await calibration_iq.operator_execute(
        settings,
        adas,
        {
            "actions": actions,
            "continue_on_error": False,
            calibration_iq._INVOCATION_CONTEXT_KEY: nested_context,  # noqa: SLF001
        },
    )
    if not isinstance(result, dict) or result.get("success") is not True or result.get("verified") is not True:
        return snapshot, actions, result if isinstance(result, dict) else {"status": "invalid_response"}
    reread = await calibration_iq.operator_snapshot(settings, ro_id)
    if reread.get("status") == "verified" and isinstance(reread.get("snapshot"), dict):
        return dict(reread["snapshot"]), actions, result
    return snapshot, actions, {
        "status": "verification_failed",
        "success": False,
        "verified": False,
        "message": "Calibration IQ accepted the ADAS Map reconciliation but the authoritative reread failed.",
    }


async def _load_ro_snapshot(settings: Any, identifier: str) -> dict[str, Any]:
    result = await calibration_iq.get_repair_order(settings, {"repair_order_id": identifier})
    if result.get("status") != "verified":
        return {"status": "error", "message": result.get("message") or "Calibration IQ did not return a verified RO.", "raw": result}
    snapshot = result.get("raw") if isinstance(result.get("raw"), dict) else {}
    return {"status": "verified", "snapshot": snapshot, "result": result}


async def _ro_requirements(settings: Any, adas: Any, args: dict[str, Any]) -> dict[str, Any]:
    identifier = str(args.get("repair_order_id") or "").strip()
    loaded = await _load_ro_snapshot(settings, identifier)
    if loaded.get("status") != "verified":
        return {"mode": "ro_requirements", "success": False, "verified": False, **loaded}
    snapshot = dict(loaded["snapshot"])
    map_info = extract_adas_map(snapshot)
    context = _valid_context(args.get(_CONTEXT_KEY))
    snapshot, planned, reconciliation = await _reconcile_one(settings, adas, snapshot, map_info, context)
    requirements = _active_ciq_requirements(snapshot)
    return {
        "status": "success" if reconciliation is None or reconciliation.get("verified") is True else "partial_success",
        "mode": "ro_requirements",
        "executed": bool(planned),
        "success": reconciliation is None or reconciliation.get("verified") is True,
        "verified": True,
        "repair_order_id": str(calibration_iq._authoritative_repair_order_id(snapshot) or identifier),  # noqa: SLF001
        "ro_number": _ro_number(snapshot, identifier),
        "vehicle": _vehicle_label(snapshot),
        "calibration_requirements": [
            {
                "id": item.get("id"),
                "label": _requirement_label(item),
                "determination": item.get("determination"),
                "method": item.get("method"),
            }
            for item in requirements
        ],
        "adas_map": map_info,
        "reconciliation_actions": planned,
        "reconciliation": reconciliation,
    }


async def _phase_list(settings: Any, args: dict[str, Any]) -> dict[str, Any]:
    filters = {"phase": str(args.get("phase") or "").strip(), "limit": 100}
    if args.get("shop"):
        filters["shop"] = str(args["shop"])
    result = await calibration_iq.read_repair_orders(settings, filters)
    return {"mode": "phase_list", **result}


async def _week_readiness(settings: Any, adas: Any, args: dict[str, Any]) -> dict[str, Any]:
    filters: dict[str, Any] = {"include_completed": False}
    if args.get("phase"):
        filters["phase"] = str(args["phase"])
    if args.get("shop"):
        filters["shop"] = str(args["shop"])
    queue = await calibration_iq.query_repair_orders(settings, filters)
    if queue.get("status") != "verified":
        return {
            "status": "failed",
            "mode": "week_readiness",
            "success": False,
            "verified": False,
            "message": queue.get("message") or "Calibration IQ did not return a complete active queue.",
            "calibration_iq": queue,
        }
    rows = [item for item in (queue.get("items") or []) if isinstance(item, dict)]
    context = _valid_context(args.get(_CONTEXT_KEY))

    semaphore = asyncio.Semaphore(6)
    async def load(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        ident = str(calibration_iq._dig(item, "id", "repair_order_id", "uuid", "ro_number", "roNumber", "number", "ro") or "").strip()  # noqa: SLF001
        async with semaphore:
            return item, await _load_ro_snapshot(settings, ident)

    loaded = await asyncio.gather(*(load(item) for item in rows))
    results: list[dict[str, Any]] = []
    added_total = 0
    for row, envelope in loaded:
        if envelope.get("status") != "verified":
            results.append({
                "ro_number": str(calibration_iq._dig(row, "ro_number", "roNumber", "number", "ro") or ""),  # noqa: SLF001
                "vehicle": calibration_iq._vehicle_label(row),  # noqa: SLF001
                "ready": False,
                "status": "ro_unavailable",
                "missing_si": [],
                "message": envelope.get("message"),
            })
            continue
        snapshot = dict(envelope["snapshot"])
        map_info = extract_adas_map(snapshot)
        snapshot, planned, reconciliation = await _reconcile_one(settings, adas, snapshot, map_info, context)
        if reconciliation is not None and reconciliation.get("verified") is True:
            added_total += len(planned)
        requirements = _active_ciq_requirements(snapshot)
        vehicle = _vehicle_label(snapshot, calibration_iq._vehicle_label(row))  # noqa: SLF001
        coverage = await _adas_coverage(adas, vehicle, requirements)
        missing_si = [item for item in coverage if item.get("available") is not True]
        map_ok = map_info.get("status") == "verified"
        reconcile_ok = not planned or (reconciliation is not None and reconciliation.get("verified") is True)
        ready = bool(map_ok and reconcile_ok and requirements and not missing_si)
        results.append({
            "repair_order_id": str(calibration_iq._authoritative_repair_order_id(snapshot) or ""),  # noqa: SLF001
            "ro_number": _ro_number(snapshot, str(calibration_iq._dig(row, "ro_number", "roNumber", "number", "ro") or "")),  # noqa: SLF001
            "vehicle": vehicle,
            "status": "ready" if ready else ("adas_map_unavailable" if not map_ok else "si_missing" if missing_si else "reconciliation_failed"),
            "ready": ready,
            "adas_map": map_info,
            "calibration_requirements": [_requirement_label(item) for item in requirements],
            "reconciliation_actions": planned,
            "reconciliation": reconciliation,
            "coverage": coverage,
            "missing_si": missing_si,
        })

    return {
        "status": "success",
        "mode": "week_readiness",
        "executed": added_total > 0,
        "success": True,
        "verified": True,
        "queue_count": len(results),
        "ready_count": sum(1 for item in results if item.get("ready") is True),
        "needs_si_count": sum(1 for item in results if item.get("missing_si")),
        "adas_map_unavailable_count": sum(1 for item in results if (item.get("adas_map") or {}).get("status") != "verified"),
        "ciq_requirements_added_or_reactivated": added_total,
        "filters": filters,
        "repair_orders": results,
    }


async def handle(settings: Any, adas: Any, args: dict[str, Any]) -> dict[str, Any]:
    mode = str(args.get("mode") or "").strip().casefold()
    if mode == "phase_list":
        return await _phase_list(settings, args)
    if mode == "ro_requirements":
        return await _ro_requirements(settings, adas, args)
    if mode == "week_readiness":
        return await _week_readiness(settings, adas, args)
    raise ValueError("Unsupported Calibration IQ work-prep mode.")


async def _bounded_selected_vehicle_signals(page: Any) -> list[str]:
    signals: list[str] = []
    try:
        current = await nav._current_vehicle_label(page)  # noqa: SLF001
        if current:
            signals.append(" ".join(str(current).split())[:500])
    except Exception:
        pass
    try:
        title = await page.title()
        if title:
            signals.append(" ".join(str(title).split())[:500])
    except Exception:
        pass
    selectors = (
        "[data-testid*='vehicle' i]", "[data-test*='vehicle' i]",
        "[aria-label*='vehicle' i]", "[id*='vehicle' i]", "[class*='vehicle' i]",
    )
    for selector in selectors:
        try:
            nodes = page.locator(selector)
            count = min(await nodes.count(), 12)
        except Exception:
            continue
        for index in range(count):
            node = nodes.nth(index)
            try:
                if not await node.is_visible(timeout=150):
                    continue
                text = " ".join((await node.inner_text(timeout=500)).split())
            except Exception:
                continue
            if text and text not in signals:
                signals.append(text[:500])
    return signals[:25]


def _row_matches_signals(row: dict[str, Any], signals: list[str]) -> bool:
    vehicle_label = calibration_iq._vehicle_label(row)  # noqa: SLF001
    vehicle = nav.vehicle_from_query(vehicle_label)
    if not (vehicle.get("year") and vehicle.get("make") and vehicle.get("model_trim")):
        return False
    vin = str(calibration_iq._dig(row, "vin", "vehicle.vin", "vehicle_vin") or "").strip()  # noqa: SLF001
    if vin and any(vin.casefold() in signal.casefold() for signal in signals):
        return True
    return any(quick._identity_matches_text(signal, vehicle) for signal in signals)  # noqa: SLF001


async def resolve_selected_alldata_to_ciq(settings: Any, adas: Any) -> dict[str, Any]:
    browser = research_operator.get_browser(Path(settings.root), adas=adas)
    state = await browser.start(auto_login=False)
    if not state.get("authenticated") or browser._page is None:  # noqa: SLF001
        return {"status": "human_action_required", "verified": False, "message": "Open/resume the ALLDATA browser and sign in first."}
    signals = await _bounded_selected_vehicle_signals(browser._page)  # noqa: SLF001
    if not signals:
        return {"status": "vehicle_selection_required", "verified": False, "message": "X could not read a bounded selected-vehicle signal from ALLDATA. Select the CIQ vehicle first."}
    queue = await calibration_iq.query_repair_orders(settings, {"include_completed": False})
    if queue.get("status") != "verified":
        return {"status": "ciq_unavailable", "verified": False, "message": queue.get("message") or "Calibration IQ active queue is unavailable."}
    matches = [row for row in (queue.get("items") or []) if isinstance(row, dict) and _row_matches_signals(row, signals)]
    if len(matches) != 1:
        return {
            "status": "ciq_vehicle_not_found" if not matches else "ciq_vehicle_ambiguous",
            "verified": False,
            "signals": signals,
            "matches": [
                {
                    "id": calibration_iq._dig(row, "id", "repair_order_id", "uuid"),  # noqa: SLF001
                    "ro_number": calibration_iq._dig(row, "ro_number", "roNumber", "number", "ro"),  # noqa: SLF001
                    "vehicle": calibration_iq._vehicle_label(row),  # noqa: SLF001
                    "shop": calibration_iq._shop_label(row),  # noqa: SLF001
                }
                for row in matches[:10]
            ],
            "message": (
                "The selected ALLDATA vehicle did not match an active Calibration IQ RO."
                if not matches
                else "The selected ALLDATA vehicle matches more than one active Calibration IQ RO; name the RO number before collecting."
            ),
        }
    row = matches[0]
    return {
        "status": "verified",
        "verified": True,
        "repair_order_id": str(calibration_iq._dig(row, "id", "repair_order_id", "uuid") or calibration_iq._dig(row, "ro_number", "roNumber", "number", "ro")),  # noqa: SLF001
        "ro_number": str(calibration_iq._dig(row, "ro_number", "roNumber", "number", "ro") or ""),  # noqa: SLF001
        "vehicle": calibration_iq._vehicle_label(row),  # noqa: SLF001
        "signals": signals,
    }


def summarize(mode: str, result: Any) -> str:
    data = result if isinstance(result, dict) else {}
    message = str(data.get("message") or "").strip()
    if mode == "alldata_access":
        if data.get("browser_active") and data.get("session_id"):
            return "Use the ALLDATA research access card below and click Resume ALLDATA browser. Select the exact Calibration IQ vehicle in the inline browser, then ask me to collect its ADAS Quick Reference."
        return "Use the ALLDATA research access card below and click Open ALLDATA browser. Sign in in the inline browser if needed, then select the exact Calibration IQ vehicle."
    if mode == "quick_reference":
        if data.get("status") == "success":
            return (
                f"ALLDATA Quick Reference for {data.get('vehicle') or 'the selected CIQ vehicle'} is accounted for: "
                f"{int(data.get('captured_count') or 0)} new procedure(s) captured and "
                f"{int(data.get('exact_duplicates_skipped') or 0) + int(data.get('possible_duplicates_skipped') or 0)} duplicate(s) skipped."
            )
        return message or "The ALLDATA Quick Reference collection was not verified."
    if mode == "phase_list":
        if data.get("status") != "verified":
            return message or "Calibration IQ did not return a verified phase list."
        rows = [row for row in (data.get("rows") or []) if isinstance(row, dict)]
        phase = str((data.get("filters") or {}).get("phase") or "")
        lead = f"Calibration IQ has {int(data.get('count') or 0)} active vehicle(s) in phase {phase}."
        detail = "; ".join(
            f"RO {row.get('RO')} — {row.get('Vehicle')}" for row in rows
        )
        return f"{lead} {detail}" if detail else lead
    if mode == "ro_requirements":
        if data.get("verified") is not True:
            return message or "Calibration IQ did not return a verified repair order."
        labels = [str(item.get("label") or "") for item in (data.get("calibration_requirements") or []) if isinstance(item, dict) and item.get("label")]
        base = f"RO {data.get('ro_number') or data.get('repair_order_id')} — {data.get('vehicle') or 'vehicle'}"
        requirements = ", ".join(labels) if labels else "no active calibration requirements recorded"
        additions = len(data.get("reconciliation_actions") or [])
        suffix = f" I reconciled and added/reactivated {additions} requirement(s) from governing ADAS Map." if additions and (data.get("reconciliation") or {}).get("verified") is True else ""
        map_status = (data.get("adas_map") or {}).get("status")
        if map_status != "verified":
            suffix += " ADAS Map requirements were not machine-readable on this RO, so I did not invent any missing requirements."
        return f"{base}: {requirements}.{suffix}"
    if mode == "week_readiness":
        if data.get("verified") is not True:
            return message or "The weekly Calibration IQ readiness audit was not verified."
        lines = [
            f"I checked {int(data.get('queue_count') or 0)} active Calibration IQ RO(s). "
            f"{int(data.get('ready_count') or 0)} are SI-ready; {int(data.get('needs_si_count') or 0)} need ADAS SI; "
            f"{int(data.get('adas_map_unavailable_count') or 0)} could not be verified against ADAS Map. "
            f"I added/reactivated {int(data.get('ciq_requirements_added_or_reactivated') or 0)} missing CIQ requirement(s) from governing ADAS Map."
        ]
        for item in data.get("repair_orders") or []:
            if not isinstance(item, dict) or item.get("ready") is True:
                continue
            missing = [str(entry.get("calibration") or "") for entry in (item.get("missing_si") or []) if isinstance(entry, dict) and entry.get("calibration")]
            if missing:
                lines.append(f"RO {item.get('ro_number')} — {item.get('vehicle')}: need ADAS SI for {', '.join(missing)}.")
            elif (item.get("adas_map") or {}).get("status") != "verified":
                lines.append(f"RO {item.get('ro_number')} — {item.get('vehicle')}: ADAS Map requirement data could not be verified.")
            elif item.get("status") == "reconciliation_failed":
                lines.append(f"RO {item.get('ro_number')} — {item.get('vehicle')}: CIQ requirement reconciliation failed verification.")
        return "\n".join(lines)
    return message or "The requested Calibration IQ work-prep action completed."


def _message_id(history: list[dict[str, Any]], approval_context: Optional[dict[str, Any]]) -> Optional[int]:
    value = (approval_context or {}).get("message_id")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    for item in reversed(history or []):
        if str(item.get("role") or "") != "user":
            continue
        candidate = item.get("id") or item.get("message_id")
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
            return candidate
    return None


def install() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        from ..tools import registry as registry_mod

        registry_mod.TOOL_SCHEMAS.setdefault(
            TOOL_NAME,
            {
                "description": (
                    "Calibration IQ work-prep bridge. Use CIQ as the work queue, ADAS Map on each RO as the governing calibration-requirement source, and ADAS SI as procedure coverage. It can list a phase, report one RO's saved requirements, or audit/reconcile the active queue for weekly SI readiness. Missing CIQ calibrations proved by ADAS Map are added/reactivated through CIQ's verified routine operator contract."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "mode": {"type": "string", "enum": ["phase_list", "ro_requirements", "week_readiness"]},
                        "repair_order_id": {"type": "string"},
                        "phase": {"type": "string"},
                        "shop": {"type": "string"},
                    },
                    "required": ["mode"],
                },
            },
        )

        previous_registry_init = registry_mod.Registry.__init__
        if not getattr(previous_registry_init, "_xomni_ciq_work_prep", False):
            def registry_init(self, *args, **kwargs):
                previous_registry_init(self, *args, **kwargs)
                self.policy.setdefault(
                    TOOL_NAME,
                    {
                        "tier": "operator_authorized",
                        "description": "Verified Calibration IQ/ADAS Map/ADAS SI work preparation and routine requirement reconciliation.",
                    },
                )

                async def handler(tool_args: dict[str, Any]):
                    from ..config import Settings
                    from . import adas_si as adas_si_mod
                    settings = Settings.load()
                    adas = adas_si_mod.AdasSI(
                        settings.adas_si_root,
                        settings.root / "data" / "capabilities" / "adas_si" / "index.sqlite",
                    )
                    return await handle(settings, adas, tool_args)

                self.register(TOOL_NAME, handler)

            registry_init._xomni_ciq_work_prep = True  # type: ignore[attr-defined]
            registry_mod.Registry.__init__ = registry_init

        # Allow the existing Quick Reference collector to resolve the selected
        # ALLDATA vehicle back to one active CIQ RO when the user naturally says
        # "for the Acura" instead of supplying an RO number.
        previous_collect = quick.collect_for_calibration_iq_ro
        if not getattr(previous_collect, "_xomni_selected_ciq_resolution", False):
            async def collect_for_calibration_iq_ro(settings: Any, adas: Any, args: dict[str, Any]):
                if str(args.get("repair_order_id") or "").strip():
                    return await previous_collect(settings, adas, args)
                resolved = await resolve_selected_alldata_to_ciq(settings, adas)
                if resolved.get("verified") is not True:
                    return {
                        **resolved,
                        "action": "collect_alldata_quick_reference",
                        "executed": False,
                        "success": False,
                    }
                forwarded = dict(args)
                forwarded["repair_order_id"] = resolved["repair_order_id"]
                result = await previous_collect(settings, adas, forwarded)
                if isinstance(result, dict):
                    result.setdefault("resolved_calibration_iq", resolved)
                return result

            collect_for_calibration_iq_ro._xomni_selected_ciq_resolution = True  # type: ignore[attr-defined]
            quick.collect_for_calibration_iq_ro = collect_for_calibration_iq_ro

        # One narrow pre-route owns the field workflow so explicit work-prep
        # requests cannot fall back to generic ADAS SI searches. Everything else
        # continues through the normal agent loop.
        try:
            from ..orchestrator import loop as loop_mod
            previous_run = loop_mod.Orchestrator._run
            if not getattr(previous_run, "_xomni_ciq_work_prep", False):
                async def routed_run(
                    self,
                    conversation_id: int,
                    user_message: str,
                    approved_tool: Optional[dict],
                    approval_context: Optional[dict],
                ) -> AsyncIterator[dict]:
                    mode = None if approved_tool else classify_request(user_message)
                    if mode is None:
                        async for event in previous_run(
                            self, conversation_id, user_message, approved_tool, approval_context
                        ):
                            yield event
                        return

                    history = self.store.get_messages(conversation_id)
                    effective_context = dict(approval_context or {})
                    message_id = _message_id(history, approval_context)
                    if message_id is not None:
                        effective_context["message_id"] = message_id
                    call_id = f"routed_work_prep_{mode}_{conversation_id}_{len(history)}"
                    artifacts: list[dict[str, Any]] = []
                    messages: list[dict[str, Any]] = []

                    if mode == "alldata_access":
                        tool_name = "research_provider_setup"
                        tool_args: dict[str, Any] = {}
                    elif mode == "quick_reference":
                        tool_name = "collision_research"
                        tool_args = {
                            "action": "collect_alldata_quick_reference",
                            "max_documents": 40,
                            "delay_seconds": 1.25,
                        }
                        if (identifier := _explicit_ro(user_message)):
                            tool_args["repair_order_id"] = identifier
                    else:
                        tool_name = TOOL_NAME
                        tool_args = {"mode": mode}
                        if (phase := _phase(user_message)):
                            tool_args["phase"] = phase
                        if (shop := _shop(user_message)):
                            tool_args["shop"] = shop
                        identifier = _explicit_ro(user_message)
                        if mode == "ro_requirements" and not identifier:
                            identifier = _latest_ro_identifier(history)
                        if identifier:
                            tool_args["repair_order_id"] = identifier
                        context = {
                            "conversation_id": conversation_id,
                            "message_id": message_id,
                            "tool_call_id": call_id,
                            "user_id": str(effective_context.get("user_id") or "local-dev"),
                            "role": str(effective_context.get("role") or "owner"),
                        }
                        if _valid_context(context):
                            tool_args[_CONTEXT_KEY] = context
                        if mode == "ro_requirements" and not tool_args.get("repair_order_id"):
                            result = {
                                "status": "missing_context",
                                "mode": mode,
                                "success": False,
                                "verified": False,
                                "message": "Tell me the RO number, or pull that RO up in Calibration IQ first.",
                            }
                            summary = summarize(mode, result)
                            saved_id = self.store.add_message(
                                conversation_id,
                                "assistant",
                                summary,
                                worker_used=self.router.active_name,
                                artifacts=[],
                            )
                            yield {"type": "token", "text": summary}
                            yield {"type": "done", "message_id": saved_id, "worker": self.router.active_name, "artifacts": []}
                            return

                    result: Any = None
                    buffered_events: list[dict[str, Any]] = []
                    async for event in self._execute(
                        tool_name,
                        tool_args,
                        messages,
                        artifacts,
                        conversation_id=conversation_id,
                        approval_context=effective_context,
                        call_id=call_id,
                    ):
                        if event.get("type") == "tool_result":
                            result = event.get("result")
                        # The generic research_provider card renders an access
                        # panel for an unknown collector result. Suppress only
                        # that misleading card; the collector's verified prose
                        # below is the user-facing result.
                        if (
                            mode == "quick_reference"
                            and event.get("type") == "artifact"
                            and (event.get("artifact") or {}).get("type") == "research_provider"
                        ):
                            continue
                        buffered_events.append(event)
                    if mode == "quick_reference":
                        artifacts[:] = [
                            artifact for artifact in artifacts
                            if artifact.get("type") != "research_provider"
                        ]

                    summary = summarize(mode, result)
                    saved_id = self.store.add_message(
                        conversation_id,
                        "assistant",
                        summary,
                        worker_used=self.router.active_name,
                        artifacts=artifacts,
                    )
                    if len(history) <= 1 and summary:
                        self.store.touch_conversation(conversation_id, title=user_message[:60])
                    for event in buffered_events:
                        yield event
                    yield {"type": "token", "text": summary}
                    yield {
                        "type": "done",
                        "message_id": saved_id,
                        "worker": self.router.active_name,
                        "artifacts": artifacts,
                    }

                routed_run._xomni_ciq_work_prep = True  # type: ignore[attr-defined]
                loop_mod.Orchestrator._run = routed_run
        except Exception:  # noqa: BLE001
            pass

        _INSTALLED = True
