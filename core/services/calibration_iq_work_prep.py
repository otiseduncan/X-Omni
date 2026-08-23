"""Calibration IQ work-prep bridge.

Calibration IQ owns the work queue. ADAS Map, when present on a repair order,
is the governing calibration-requirement source. ADAS SI owns procedure
availability. This module joins those existing contracts without creating a
second queue or teaching the model to guess at job scope.

High-confidence conversational workflows are handled directly:
* "check what cars are in phase five" -> Calibration IQ phase list
* "which phase five cars need ADAS SI" -> full phase-scoped readiness audit
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
import json
import re
import threading
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from . import adas_artifact_catalog
from . import calibration_iq
from . import calibration_iq_weekly_queue as weekly_queue
from . import research_alldata_navigation as nav
from . import research_alldata_quick_reference as quick
from . import research_operator

TOOL_NAME = "calibration_iq_work_prep"
_CONTEXT_KEY = "__xomni_work_prep_context"
_INSTALL_LOCK = threading.Lock()
_INSTALLED = False

_ACTIVE_DETERMINATIONS = {"REQUIRED", "LIKELY_REQUIRED", "NEEDS_RESEARCH"}
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
# Broader than _QUICK_REFERENCE_RE: matches a bare "ADAS" mention (patched by
# calibration_iq_work_prep_guards for observed field mishearings) without
# requiring the literal "quick reference" phrase. Combined with an
# acquisition verb and an explicit reference to the vehicle in front of the
# tech, "collect the ADAS information for this car" is unambiguously the same
# request as "collect the ADAS Quick Reference" -- the tech should not have to
# know that exact phrase. Requiring the vehicle reference keeps this from
# capturing a generic knowledge question like "get the ADAS calibration
# steps", which is an ADAS SI search, not an ALLDATA acquisition.
_ADAS_MARKER_RE = re.compile(r"\b(?:adas|ados|a\s*d\s*a\s*s)\b", re.IGNORECASE)
_VEHICLE_REFERENCE_RE = re.compile(r"\b(?:this|that)\s+(?:car|vehicle|one)\b", re.IGNORECASE)
_WEEK_READY_RE = re.compile(
    r"\b(?:prepared|prepare|prep|ready|readiness)\b.{0,90}"
    r"\b(?:weekly\s+queue|queue|repair\s+orders?|ros?|si|adas|calibration\s+iq|ciq)\b|"
    r"\b(?:what|which)\b.{0,60}\b(?:adas\s+si|si)\b.{0,60}\b(?:missing|need|needed)\b",
    re.IGNORECASE | re.DOTALL,
)
# Whole-utterance aliases for the ordinary weekly command.  Week/work alone is
# not sufficient because this is an operator-authorized lane: phrases such as
# "prepare a presentation for work" and "ready for a week-long trip" must stay
# in normal conversation.
_WEEK_COMMAND_RE = re.compile(
    r"^\s*(?:please\s+)?(?:let(?:'s|\s+us)\s+)?(?:"
    r"(?:prepare|prep)(?:\s+(?:me|us|everything|all\s+cars|the\s+cars))?"
    r"(?:\s+for)?\s+(?:(?:this|the)\s+)?week|"
    r"get(?:\s+(?:me|us|all\s+cars|the\s+cars))?\s+ready\s+for\s+"
    r"(?:(?:this|the)\s+)?week|"
    r"make\s+sure\s+(?:we(?:'re|\s+are)|(?:all|the)\s+(?:cars|vehicles)\s+are)"
    r"\s+(?:prepared|ready)\s+for\s+(?:(?:this|the)\s+)?week"
    r")\s*[.!?]?\s*$",
    re.IGNORECASE,
)
# Speech-to-text can turn the final word in the terse field command
# "prepare week" into "weak".  Keep this alias anchored to the complete
# utterance so ordinary phrases such as "prepare a weak argument" never enter
# the Calibration IQ workflow.
_WEEK_WEAK_ALIAS_RE = re.compile(
    r"^\s*(?:please\s+)?(?:let(?:'s|\s+us)\s+)?(?:prepare|prep)(?:\s+(?:me|us))?"
    r"(?:\s+for)?\s+(?:the\s+)?weak\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_PHASE_COVERAGE_RE = re.compile(
    r"\b(?:coverage|covered|si[-\s]?ready)\b|"
    r"\badas\s+map\s+(?:reports?|evidence|results?)\b.{0,60}\badas\s+si\b|"
    r"\badas\s+si\b.{0,60}\badas\s+map\s+(?:reports?|evidence|results?)\b|"
    r"\b(?:missing|need|needs|needed|requiring)\b.{0,50}"
    r"\b(?:adas\s+si|si|coverage)\b|"
    r"\b(?:adas\s+si|si|coverage)\b.{0,50}"
    r"\b(?:missing|needed|required|available|ready|covered)\b",
    re.IGNORECASE | re.DOTALL,
)
_COUNT_INTENT_RE = re.compile(r"\b(?:how\s+many|count)\b", re.IGNORECASE)
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
# "download the SI for the [vehicle] that's open in ALLDATA" describes the
# already-open browser state; it is not a request to open/log in to it. Only
# the login-card verbs immediately followed by "in" + ALLDATA are excluded --
# "open ALLDATA", "open the ALLDATA browser", and "log in to ALLDATA" all
# still match the access request above.
_ALLDATA_DESCRIPTIVE_STATE_RE = re.compile(
    r"\b(?:open|resumed|started|running|loaded|showing|pulled\s+up)\s+(?:up\s+)?in\s+all\s*data\b",
    re.IGNORECASE,
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

# Walking a weekly-readiness missing-SI queue one vehicle at a time: pick a
# vehicle in ALLDATA, say "next" instead of repeating the RO or vehicle every
# turn. Deliberately anchored to the whole message so "next" embedded in an
# unrelated longer sentence never matches -- this is a bare go-ahead cue, not
# a topic word.
_QUEUE_NEXT_RE = re.compile(
    r"^\s*(?:ok(?:ay)?[,.]?\s*)?(?:go\s+to\s+)?(?:the\s+)?next(?:\s+(?:one|car|vehicle|up))?\s*[.!]?\s*$|"
    r"^\s*who'?s\s+next\s*[?.!]?\s*$",
    re.IGNORECASE,
)

# Conversational continuity for the ALLDATA acquisition flow: once "log in to
# ALLDATA" has run, a follow-up like "retrieve SI information please" carries
# no ALLDATA/quick-reference/RO wording of its own and would otherwise fall
# through to the model's own (unrouted) tool choice -- which is exactly what
# produced repeated adas_si_inventory calls in the field. The active stage is
# recorded as an invisible artifact (no UI card is registered for its type) on
# the assistant turn that ran alldata_access/quick_reference, then read back
# here so a short, low-content follow-up resolves against it instead of
# needing its own exact phrasing.
_WORK_PREP_STATE_ARTIFACT = "work_prep_state"
_WORK_PREP_MODE = "ciq_si_preparation"
_CONTINUATION_LOOKBACK_MESSAGES = 8
_CONTINUATION_PHRASE_RE = re.compile(
    r"\b(?:go\s+ahead|do\s+it|do\s+this\s+one|pull\s+it|ready|proceed|okay|"
    r"go\s+for\s+it|sounds\s+good)\b",
    re.IGNORECASE,
)


def _looks_like_work_prep_continuation(text: str) -> bool:
    # Deliberately narrow: only an explicit acquisition verb (collect, pull,
    # retrieve, ...) or one of a short list of unmistakable go-ahead phrases
    # counts. A bare "yes"/"ok" is excluded -- it is too generic and could be
    # answering something unrelated (a calendar prompt, an approval card)
    # that happens to fall within the same lookback window.
    value = " ".join(str(text or "").split()).strip()
    if not value or len(value) > 160:
        return False
    return bool(_QUICK_ACTION_RE.search(value) or _CONTINUATION_PHRASE_RE.search(value))


def _active_work_prep_stage(history: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for index, message in enumerate(reversed(history or [])):
        if index >= _CONTINUATION_LOOKBACK_MESSAGES:
            break
        artifacts = message.get("artifacts") or []
        if not isinstance(artifacts, list):
            continue
        for artifact in reversed(artifacts):
            if isinstance(artifact, dict) and artifact.get("type") == _WORK_PREP_STATE_ARTIFACT:
                data = artifact.get("data")
                if isinstance(data, dict):
                    return data
    return None


def classify_request(
    text: object, history: Optional[list[dict[str, Any]]] = None
) -> Optional[str]:
    value = str(text or "").strip()
    if not value:
        return None
    if (
        _ALLDATA_ACCESS_RE.search(value)
        and not _QUICK_REFERENCE_RE.search(value)
        and not _ALLDATA_DESCRIPTIVE_STATE_RE.search(value)
    ):
        return "alldata_access"
    if _QUICK_REFERENCE_RE.search(value) and _QUICK_ACTION_RE.search(value):
        return "quick_reference"
    if (
        _ADAS_MARKER_RE.search(value)
        and _QUICK_ACTION_RE.search(value)
        and _VEHICLE_REFERENCE_RE.search(value)
    ):
        return "quick_reference"
    if _PHASE_RE.search(value) and _PHASE_COVERAGE_RE.search(value):
        return "phase_coverage"
    if (
        _WEEK_READY_RE.search(value)
        or _WEEK_COMMAND_RE.fullmatch(value)
        or _WEEK_WEAK_ALIAS_RE.fullmatch(value)
    ):
        return "week_readiness"
    if (
        _PHASE_RE.search(value)
        and _PHASE_LIST_RE.search(value)
        and not _COUNT_INTENT_RE.search(value)
    ):
        return "phase_list"
    if (
        _RO_RE.search(value) or _THIS_RO_RE.search(value)
    ) and _RO_REQUIREMENT_RE.search(value):
        return "ro_requirements"
    if _QUEUE_NEXT_RE.search(value):
        # Whether an active weekly-readiness queue actually exists is
        # checked downstream (resolve_queue_next); a bare "next" with none
        # active fails closed with a clear message instead of guessing.
        return "queue_next"
    if history:
        stage = _active_work_prep_stage(history)
        if (
            stage
            and stage.get("mode") == _WORK_PREP_MODE
            and stage.get("stage")
            in {"awaiting_vehicle_selection", "awaiting_ro_disambiguation"}
            and _looks_like_work_prep_continuation(value)
        ):
            return "quick_reference"
    return None


def _phase(text: object) -> Optional[str]:
    match = _PHASE_RE.search(str(text or ""))
    if not match:
        return None
    token = match.group("phase").casefold()
    return _PHASE_WORDS.get(token, str(int(token)))


def _phase_coverage_focus(text: object) -> str:
    value = str(text or "")
    if _ADAS_MAP_MARKER_RE.search(value) and re.search(
        r"\b(?:reports?|evidence|results?)\b", value, re.IGNORECASE
    ):
        return "adas_map"
    return "si_readiness"


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
    # These are deliberately bounded domain aliases, not fuzzy semantics.  They
    # let ADAS Map and CIQ name the same physical operation differently without
    # promoting a merely-related component to an authoritative requirement.
    families = (
        (
            r"\b(?:occupant\s+(?:classification|detection)|ocs|passenger\s+seat\s+weight|"
            r"seat[-\s]*weight(?:\s+sensor)?(?:\s+zero\s+point)?)\b",
            "occupantclassification",
        ),
        (r"\bseat\s*belt(?:\s+inspection)?\b", "seatbelt"),
        (
            r"\b(?:ipma|forward\s+recognition|mono(?:cular)?\s+camera|"
            r"windshield\s+(?:mono[-\s]*)?camera|front\s+camera|"
            r"forward(?:[-\s]+facing)?\s+camera|lane\s+camera)\b",
            "frontcamera",
        ),
        (
            r"\b(?:millimeter[-\s]*wave|front|forward|adaptive\s+cruise)\s+radar\b|\bccm\b",
            "frontradar",
        ),
        (r"\b(?:blind\s*spot(?:\s+monitor(?:ing)?)?|bsm|bsd|sodcm)\b", "blindspot"),
        (r"\bsteering\s*angle(?:\s+sensor)?\b|\bsas\b", "steeringangle"),
        (r"\b(?:rear(?:\s+view)?|reverse)\s+camera\b", "rearcamera"),
        (r"\b(?:surround|around)\s+view\b|\b360(?:\s+camera)?\b", "surroundcamera"),
        (r"\b(?:parking|park)\s+assist\b|\bultrasonic\b|\bsonar\b", "parkingassist"),
    )
    for pattern, key in families:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return key
    replacements = (
        (r"\bblind\s*spot(?:\s+monitor(?:ing)?)?\b|\bbsm\b|\bbsd\b", " bsm "),
        (r"\bsteering\s*angle(?:\s+sensor)?\b", " steeringangle "),
        (
            r"\b(?:forward(?:\s*facing)?|front|windshield|lane)\s+camera\b",
            " frontcamera ",
        ),
        (
            r"\b(?:millimeter[-\s]*wave|forward|front|adaptive\s+cruise)\s+radar\b",
            " frontradar ",
        ),
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


def build_reconciliation_actions(
    snapshot: dict[str, Any], map_info: dict[str, Any], ro_id: str
) -> list[dict[str, Any]]:
    existing = _ciq_calibrations(snapshot)
    by_key: dict[str, list[dict[str, Any]]] = {}
    for item in existing:
        key = _calibration_key(item.get("calibration_type"))
        if key:
            by_key.setdefault(key, []).append(item)

    actions: list[dict[str, Any]] = []
    for required in map_info.get("requirements") or []:
        if not isinstance(required, dict):
            continue
        label = str(required.get("label") or "").strip()[:160]
        key = _calibration_key(label)
        if not key:
            continue
        matches = by_key.get(key) or []
        map_method = _method(required.get("method"))
        active_required = [
            item
            for item in matches
            if str(item.get("determination") or "").upper() == "REQUIRED"
        ]
        if any(
            map_method == "UNKNOWN" or _method(item.get("method")) == map_method
            for item in active_required
        ):
            continue
        if not matches:
            actions.append(
                {
                    "operation": "add_calibration",
                    "repair_order_id": ro_id,
                    "arguments": {
                        "calibration_type": label,
                        "determination": "REQUIRED",
                        "method": _method(required.get("method")),
                        "notes": "Added by X from governing ADAS Map during SI readiness reconciliation.",
                        "research_status": "ADAS Map governing source",
                    },
                }
            )
            continue
        # Prefer correcting an already-active record, then the newest
        # versioned inactive record.  Never add a second record merely because
        # an existing alias lacks the optimistic-lock fields needed for a safe
        # update; the post-read parity check will expose that as an exception.
        candidates = sorted(
            matches,
            key=lambda item: (
                str(item.get("determination") or "").upper() in _ACTIVE_DETERMINATIONS,
                int(item.get("version") or 0)
                if isinstance(item.get("version"), int)
                and not isinstance(item.get("version"), bool)
                else 0,
            ),
            reverse=True,
        )
        found = candidates[0]
        item_id = str(found.get("id") or "").strip()
        version = found.get("version")
        if (
            not item_id
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
        ):
            continue
        changes: dict[str, Any] = {
            "determination": "REQUIRED",
            "research_status": "ADAS Map governing source",
        }
        if map_method != "UNKNOWN":
            changes["method"] = map_method
        actions.append(
            {
                "operation": "update_calibration",
                "target_id": item_id,
                "expected_version": version,
                "arguments": changes,
            }
        )
    return actions


def _reconciliation_issues(
    snapshot: dict[str, Any], map_info: dict[str, Any]
) -> list[dict[str, Any]]:
    """Prove that the authoritative ADAS Map set exists exactly in CIQ."""
    existing = _ciq_calibrations(snapshot)
    issues: list[dict[str, Any]] = []
    if map_info.get("explicit_no_calibration") is True:
        active = [
            item
            for item in existing
            if str(item.get("determination") or "").upper() in _ACTIVE_DETERMINATIONS
        ]
        if active:
            issues.append({"code": "explicit_none_conflicts_with_active_ciq"})
        return issues

    governing_keys: dict[str, str] = {}
    for required in map_info.get("requirements") or []:
        if not isinstance(required, dict):
            continue
        label = str(required.get("label") or "").strip()
        key = _calibration_key(label)
        if not key:
            issues.append({"code": "requirement_unparsed", "calibration": label})
            continue
        governing_keys.setdefault(key, label)
        matches = [
            item
            for item in existing
            if _calibration_key(item.get("calibration_type")) == key
            and str(item.get("determination") or "").upper() in _ACTIVE_DETERMINATIONS
        ]
        if not matches:
            issues.append({"code": "required_item_missing", "calibration": label})
            continue
        if len(matches) > 1:
            issues.append({"code": "duplicate_active_items", "calibration": label})
            continue
        if str(matches[0].get("determination") or "").upper() != "REQUIRED":
            issues.append({"code": "required_item_not_final", "calibration": label})
            continue
        map_method = _method(required.get("method"))
        if map_method != "UNKNOWN" and _method(matches[0].get("method")) != map_method:
            issues.append({"code": "method_mismatch", "calibration": label})

    for item in existing:
        if str(item.get("determination") or "").upper() not in _ACTIVE_DETERMINATIONS:
            continue
        label = _requirement_label(item)
        key = _calibration_key(label)
        if not key:
            issues.append(
                {
                    "code": "extra_active_item",
                    "calibration": label,
                    "reason": "active CIQ label could not be normalized",
                }
            )
        elif key not in governing_keys:
            issues.append({"code": "extra_active_item", "calibration": label})
    return issues


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


def _catalog_for(adas: Any) -> Optional[adas_artifact_catalog.AdasArtifactCatalog]:
    source_root = getattr(adas, "source_root", None)
    cache_path = getattr(adas, "cache_path", None)
    if source_root is None or cache_path is None:
        return None
    return adas_artifact_catalog.AdasArtifactCatalog(
        Path(source_root), Path(cache_path)
    )


def _artifact_identity(
    snapshot: dict[str, Any], *, include_vehicle: bool = False
) -> dict[str, Any]:
    ro = snapshot.get("repair_order")
    ro = ro if isinstance(ro, dict) else {}
    vehicle = snapshot.get("vehicle")
    vehicle = vehicle if isinstance(vehicle, dict) else {}
    ro_number = str(ro.get("ro_number") or "").strip()
    vin = str(vehicle.get("vin") or ro.get("vin") or "").strip().upper()
    query: dict[str, Any] = {
        "ro_number": ro_number or None,
        "vin": vin or None,
    }
    if not ro_number and not vin:
        query["ciq_ro_id"] = str(ro.get("id") or "").strip() or None
    if include_vehicle or not any(query.values()):
        year = vehicle.get("year")
        make = vehicle.get("make")
        model = vehicle.get("model")
        if (
            year not in (None, "")
            and make not in (None, "")
            and model
            not in (
                None,
                "",
            )
        ):
            query.update(
                {
                    "year": year,
                    "make": make,
                    "model": model,
                    "trim": vehicle.get("trim"),
                    "configuration": _identity_text(
                        vehicle.get("configuration") or ro.get("vehicle_configuration")
                    )
                    or None,
                }
            )
    if not any(query.values()):
        query.update(
            {
                "year": vehicle.get("year"),
                "make": vehicle.get("make"),
                "model": vehicle.get("model"),
            }
        )
    return query


def _identity_text(value: object) -> str:
    if isinstance(value, dict):
        for key in (
            "adas_map_model_configuration",
            "model_configuration",
            "configuration",
            "label",
            "name",
        ):
            candidate = " ".join(str(value.get(key) or "").split()).casefold()
            if candidate:
                return candidate
        return ""
    return " ".join(str(value or "").split()).casefold()


def _artifact_identity_conflicts(
    snapshot: dict[str, Any], record: dict[str, Any]
) -> list[str]:
    ro = snapshot.get("repair_order")
    ro = ro if isinstance(ro, dict) else {}
    expected_vehicle = snapshot.get("vehicle")
    expected_vehicle = expected_vehicle if isinstance(expected_vehicle, dict) else {}
    observed_vehicle = record.get("vehicle")
    observed_vehicle = observed_vehicle if isinstance(observed_vehicle, dict) else {}
    conflicts: list[str] = []

    pairs = {
        "repair_order_id": (
            calibration_iq._authoritative_repair_order_id(snapshot),  # noqa: SLF001
            record.get("ciq_ro_id"),
        ),
        "ro_number": (_ro_number(snapshot), record.get("ro_number")),
        "vin": (
            expected_vehicle.get("vin") or ro.get("vin"),
            record.get("vin"),
        ),
        "year": (expected_vehicle.get("year"), observed_vehicle.get("year")),
        "make": (expected_vehicle.get("make"), observed_vehicle.get("make")),
        "model": (expected_vehicle.get("model"), observed_vehicle.get("model")),
        "trim": (expected_vehicle.get("trim"), observed_vehicle.get("trim")),
        "configuration": (
            expected_vehicle.get("configuration") or ro.get("vehicle_configuration"),
            observed_vehicle.get("configuration"),
        ),
    }
    for field, (expected, observed) in pairs.items():
        if expected in (None, "") or observed in (None, ""):
            continue
        if field == "year":
            try:
                matches = int(expected) == int(observed)
            except (TypeError, ValueError):
                matches = False
        elif field == "vin":
            matches = str(expected).strip().upper() == str(observed).strip().upper()
        else:
            matches = _identity_text(expected) == _identity_text(observed)
        if not matches:
            conflicts.append(field)
    return conflicts


async def _discover_adas_map(
    catalog: Optional[adas_artifact_catalog.AdasArtifactCatalog],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    if catalog is None:
        return {
            "status": "unverified",
            "governing_source": "ADAS Map",
            "requirements": [],
            "sources": [],
            "requirement_count": 0,
            "reason": "The ADAS artifact catalog is unavailable.",
        }
    try:
        discovery = await asyncio.to_thread(
            catalog.discover, **_artifact_identity(snapshot)
        )
    except Exception as exc:  # noqa: BLE001 - one artifact must not abort the queue
        return {
            "status": "unverified",
            "governing_source": "ADAS Map",
            "requirements": [],
            "sources": [],
            "requirement_count": 0,
            "reason": f"Artifact discovery failed ({type(exc).__name__}).",
        }

    record = (
        discovery.get("record") if isinstance(discovery.get("record"), dict) else {}
    )
    requirements = [
        dict(item)
        for item in (record.get("requirements") or [])
        if isinstance(item, dict) and str(item.get("label") or "").strip()
    ]
    explicit_none = record.get("explicit_no_calibration") is True
    discovery_status = str(discovery.get("status") or "unverified")
    status = discovery_status
    if discovery_status == adas_artifact_catalog.DISCOVERY_VERIFIED:
        status = "verified" if requirements or explicit_none else "present_unparsed"

    identity_conflicts = _artifact_identity_conflicts(snapshot, record)
    if identity_conflicts:
        status = "ambiguous"
        discovery_status = "ambiguous"

    return {
        "status": status,
        "discovery_status": discovery_status,
        "governing_source": "ADAS Map",
        "requirements": requirements,
        "sources": list(record.get("sources") or []),
        "requirement_count": len(requirements),
        "explicit_no_calibration": explicit_none,
        "inspection_id": record.get("inspection_id"),
        "vin": record.get("vin"),
        "vehicle": record.get("vehicle"),
        "artifact_index": discovery.get("index"),
        "reason": (
            "ADAS Map provenance contradicts the authoritative CIQ identity: "
            + ", ".join(identity_conflicts)
            if identity_conflicts
            else discovery.get("reason")
        ),
        "identity_conflicts": identity_conflicts,
    }


async def _catalog_coverage(
    catalog: Optional[adas_artifact_catalog.AdasArtifactCatalog],
    snapshot: dict[str, Any],
    map_info: dict[str, Any],
) -> list[dict[str, Any]]:
    labels = [
        str(item.get("label") or "").strip()
        for item in (map_info.get("requirements") or [])
        if isinstance(item, dict) and str(item.get("label") or "").strip()
    ]
    if not labels or map_info.get("status") != "verified":
        return []
    if catalog is None:
        return [
            {
                "calibration": label,
                "state": adas_artifact_catalog.UNVERIFIED,
                "available": False,
                "documents": [],
                "reason": "The ADAS artifact catalog is unavailable.",
            }
            for label in labels
        ]
    try:
        payload = await asyncio.to_thread(
            catalog.requirement_coverage,
            labels,
            **_artifact_identity(snapshot, include_vehicle=True),
        )
    except Exception as exc:  # noqa: BLE001 - classify uncertainty; keep processing ROs
        return [
            {
                "calibration": label,
                "state": adas_artifact_catalog.UNVERIFIED,
                "available": False,
                "documents": [],
                "reason": f"Artifact coverage failed ({type(exc).__name__}).",
            }
            for label in labels
        ]

    outcomes = {
        _calibration_key(item.get("requirement")): item
        for item in (payload.get("requirements") or [])
        if isinstance(item, dict)
    }
    coverage: list[dict[str, Any]] = []
    for label in labels:
        outcome = outcomes.get(_calibration_key(label)) or {}
        state = str(outcome.get("state") or adas_artifact_catalog.UNVERIFIED).upper()
        if state not in {
            adas_artifact_catalog.COVERED,
            adas_artifact_catalog.MISSING,
            adas_artifact_catalog.UNVERIFIED,
        }:
            state = adas_artifact_catalog.UNVERIFIED
        sources = [
            item for item in (outcome.get("sources") or []) if isinstance(item, dict)
        ]
        coverage.append(
            {
                "calibration": label,
                "state": state,
                # Compatibility only; decision code below keys on the explicit
                # three-state value and never maps False directly to MISSING.
                "available": state == adas_artifact_catalog.COVERED,
                "documents": sorted(
                    {
                        str(item.get("relative_path") or "")
                        for item in sources
                        if str(item.get("relative_path") or "").strip()
                    }
                ),
                "sources": sources,
                "reason": outcome.get("reason") or payload.get("reason"),
            }
        )
    return coverage


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


def _research_state_and_version(snapshot: dict[str, Any]) -> tuple[str, Optional[int]]:
    state = re.sub(
        r"[\s-]+", "_",
        str(calibration_iq._dig(  # noqa: SLF001
            snapshot,
            "research.state",
            "research_case.state",
            "research_state",
            "repair_order.research_state",
            default="",
        ) or "").strip().casefold(),
    )
    version = calibration_iq._dig(  # noqa: SLF001
        snapshot, "research.version", "research_case.version", default=None
    )
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        version = None
    return state, version


def _operator_count(result: dict[str, Any], key: str) -> int:
    value = result.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _operator_result_snapshot(result: Any, repair_order_id: str) -> Optional[dict[str, Any]]:
    if not isinstance(result, dict):
        return None
    final_snapshots = result.get("final_snapshots")
    if not isinstance(final_snapshots, dict):
        return None
    envelope = final_snapshots.get(repair_order_id)
    if (
        not isinstance(envelope, dict)
        or envelope.get("status") != "verified"
        or not isinstance(envelope.get("snapshot"), dict)
    ):
        return None
    return dict(envelope["snapshot"])


def _operator_may_have_executed(result: Any) -> bool:
    return bool(
        isinstance(result, dict)
        and (
            result.get("executed") is True
            or result.get("may_have_executed") is True
            or any(isinstance(item, dict) for item in (result.get("receipts") or []))
        )
    )


def _merge_operator_results(
    first: dict[str, Any],
    second: dict[str, Any],
    *,
    second_requested_count: int,
) -> dict[str, Any]:
    receipts = [
        receipt
        for result in (first, second)
        for receipt in (result.get("receipts") or [])
        if isinstance(receipt, dict)
    ]
    requested_count = max(_operator_count(first, "requested_count"), 1) + max(
        _operator_count(second, "requested_count"), second_requested_count
    )
    processed_count = sum(
        max(
            _operator_count(result, "processed_count"),
            len([receipt for receipt in (result.get("receipts") or []) if isinstance(receipt, dict)]),
        )
        for result in (first, second)
    )
    verified_count = sum(
        1
        for receipt in receipts
        if receipt.get("status") == "completed"
        and receipt.get("success") is True
        and isinstance(receipt.get("verification"), dict)
        and receipt["verification"].get("verified") is True
    )
    success = bool(
        first.get("success") is True
        and first.get("verified") is True
        and second.get("success") is True
        and second.get("verified") is True
    )
    combined = {
        **second,
        "status": str(second.get("status") or "success")
        if success else "partial_success" if verified_count else "failed",
        "executed": first.get("executed") is True or second.get("executed") is True,
        "success": success,
        "verified": success,
        "partial": not success and verified_count > 0,
        "requested_count": requested_count,
        "processed_count": processed_count,
        "verified_count": verified_count,
        "receipts": receipts,
        "research_reopen_attempted": True,
        "research_reopened": True,
    }
    if not isinstance(combined.get("error"), dict):
        receipt_error = next(
            (receipt.get("error") for receipt in receipts if isinstance(receipt.get("error"), dict)),
            None,
        )
        if receipt_error is not None:
            combined["error"] = receipt_error
    return combined


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

    def nested_context(suffix: str) -> dict[str, Any]:
        return {
            **context,
            "tool_call_id": f"{context['tool_call_id']}-adas-map-{suffix}-{ro_id[:12]}",
        }

    research_reopen_result: Optional[dict[str, Any]] = None
    research_state, research_version = _research_state_and_version(snapshot)
    if research_state in {"complete", "completed", "research_complete"}:
        if research_version is None:
            return snapshot, actions, {
                "status": "research_version_missing",
                "executed": False,
                "success": False,
                "verified": False,
                "message": "Calibration IQ research is complete, but its authoritative version is unavailable; X did not reopen or change the RO.",
            }
        research_reopen_result = await calibration_iq.operator_execute(
            settings,
            adas,
            {
                "actions": [{
                    "operation": "update_research",
                    "repair_order_id": ro_id,
                    "expected_version": research_version,
                    "arguments": {
                        "state": "research_in_progress",
                        "reason": "Reopened by X to reconcile governing ADAS Map calibration requirements.",
                    },
                }],
                "continue_on_error": False,
                calibration_iq._INVOCATION_CONTEXT_KEY: nested_context("reopen"),  # noqa: SLF001
            },
        )
        if not isinstance(research_reopen_result, dict):
            reread = await calibration_iq.operator_snapshot(settings, ro_id)
            post_snapshot = (
                dict(reread["snapshot"])
                if reread.get("status") == "verified" and isinstance(reread.get("snapshot"), dict)
                else None
            )
            return post_snapshot or snapshot, actions, {
                "status": "invalid_response",
                "executed": True,
                "success": False,
                "verified": False,
                "partial": False,
                "requested_count": 1,
                "processed_count": 0,
                "receipts": [],
                "may_have_executed": True,
                "indeterminate": True,
                "research_reopen_attempted": True,
                "research_reopened": False,
                "authoritative_reread": reread,
            }

        reopened_snapshot = _operator_result_snapshot(research_reopen_result, ro_id)
        reopen_reread = (
            (research_reopen_result.get("final_snapshots") or {}).get(ro_id)
            if isinstance(research_reopen_result.get("final_snapshots"), dict)
            else None
        )
        if reopened_snapshot is None and _operator_may_have_executed(research_reopen_result):
            reopen_reread = await calibration_iq.operator_snapshot(settings, ro_id)
            if reopen_reread.get("status") == "verified" and isinstance(reopen_reread.get("snapshot"), dict):
                reopened_snapshot = dict(reopen_reread["snapshot"])

        reopened_state, reopened_version = (
            _research_state_and_version(reopened_snapshot)
            if isinstance(reopened_snapshot, dict) else ("", None)
        )
        reopen_receipts = [
            receipt for receipt in (research_reopen_result.get("receipts") or [])
            if isinstance(receipt, dict)
        ]
        reopen_receipt = reopen_receipts[0] if len(reopen_receipts) == 1 else {}
        receipt_state, receipt_version = _research_state_and_version(
            {"research": reopen_receipt.get("after")}
            if isinstance(reopen_receipt.get("after"), dict) else {}
        )
        reopen_verified = bool(
            len(reopen_receipts) == 1
            and _operator_count(research_reopen_result, "requested_count") == 1
            and _operator_count(research_reopen_result, "processed_count") == 1
            and calibration_iq._receipt_verified(reopen_receipt)  # noqa: SLF001
            and reopen_receipt.get("operation") == "update_research"
            and str(reopen_receipt.get("repair_order_id") or "").strip() == ro_id
            and receipt_state == "research_in_progress"
            and receipt_version is not None
            and receipt_version > research_version
            and reopened_snapshot is not None
            and reopened_state == "research_in_progress"
            and reopened_version is not None
            and reopened_version > research_version
        )
        if not reopen_verified:
            return reopened_snapshot or snapshot, actions, {
                **research_reopen_result,
                "status": "verification_failed"
                if research_reopen_result.get("success") is True
                else str(research_reopen_result.get("status") or "failed"),
                "success": False,
                "verified": False,
                "research_reopen_attempted": True,
                "research_reopened": False,
                "message": "Calibration IQ did not prove the required research reopen; no calibration reconciliation was attempted.",
                "authoritative_reread": reopen_reread
                if isinstance(reopen_reread, dict)
                else {"status": "unavailable", "message": "Calibration IQ did not return a verified final snapshot."},
            }

        research_reopen_result = {
            **research_reopen_result,
            "status": "success",
            "success": True,
            "verified": True,
            "partial": False,
            "requested_count": 1,
            "processed_count": 1,
            "final_snapshots": {
                **(research_reopen_result.get("final_snapshots")
                   if isinstance(research_reopen_result.get("final_snapshots"), dict) else {}),
                ro_id: {"status": "verified", "snapshot": reopened_snapshot},
            },
            "verification_recovered_by_reread": bool(
                reopen_reread is not None
                and (research_reopen_result.get("success") is not True
                     or research_reopen_result.get("verified") is not True)
            ),
        }
        snapshot = dict(reopened_snapshot)
        actions = build_reconciliation_actions(snapshot, map_info, ro_id)
        if not actions:
            return snapshot, [], {
                **research_reopen_result,
                "research_reopen_attempted": True,
                "research_reopened": True,
                "research_version_before": research_version,
                "research_version_after": reopened_version,
            }

    result = await calibration_iq.operator_execute(
        settings,
        adas,
        {
            "actions": actions,
            "continue_on_error": False,
            calibration_iq._INVOCATION_CONTEXT_KEY: nested_context("requirements"),  # noqa: SLF001
        },
    )
    if not isinstance(result, dict):
        reread = await calibration_iq.operator_snapshot(settings, ro_id)
        final_snapshot = (
            dict(reread["snapshot"])
            if reread.get("status") == "verified" and isinstance(reread.get("snapshot"), dict)
            else None
        )
        invalid_result = {
            "status": "invalid_response",
            "executed": True,
            "success": False,
            "verified": False,
            "partial": False,
            "requested_count": len(actions),
            "processed_count": 0,
            "receipts": [],
            "may_have_executed": True,
            "indeterminate": True,
            "authoritative_reread": reread,
        }
        if research_reopen_result is not None:
            invalid_result = {
                **_merge_operator_results(
                    research_reopen_result, invalid_result,
                    second_requested_count=len(actions),
                ),
                "research_version_before": research_version,
                "research_version_after": _research_state_and_version(final_snapshot or snapshot)[1],
            }
        return final_snapshot or snapshot, actions, invalid_result

    final_snapshot = _operator_result_snapshot(result, ro_id)
    if final_snapshot is None and _operator_may_have_executed(result):
        reread = await calibration_iq.operator_snapshot(settings, ro_id)
        if reread.get("status") == "verified" and isinstance(reread.get("snapshot"), dict):
            final_snapshot = dict(reread["snapshot"])
    if research_reopen_result is not None:
        result = {
            **_merge_operator_results(
                research_reopen_result, result,
                second_requested_count=len(actions),
            ),
            "research_version_before": research_version,
            "research_version_after": _research_state_and_version(final_snapshot or snapshot)[1],
        }
    if final_snapshot is not None:
        return final_snapshot, actions, result
    if result.get("success") is not True or result.get("verified") is not True:
        return snapshot, actions, result
    return snapshot, actions, {
        **result,
        "status": "verification_failed",
        "executed": result.get("executed") is True,
        "success": False,
        "verified": False,
        "message": "Calibration IQ accepted the ADAS Map reconciliation but the authoritative reread failed.",
        "authoritative_reread": {
            "status": "unavailable",
            "message": "Calibration IQ did not return a verified final snapshot.",
        },
    }


async def _load_ro_snapshot(settings: Any, identifier: str) -> dict[str, Any]:
    result = await calibration_iq.operator_resolve_snapshot(settings, identifier)
    if result.get("status") != "verified":
        return {
            "status": "error",
            "message": result.get("message")
            or "Calibration IQ did not return a verified operator snapshot.",
            "raw": result,
        }
    snapshot = (
        result.get("snapshot") if isinstance(result.get("snapshot"), dict) else {}
    )
    return {"status": "verified", "snapshot": snapshot, "result": result}


async def _ro_requirements(
    settings: Any, adas: Any, args: dict[str, Any]
) -> dict[str, Any]:
    identifier = str(args.get("repair_order_id") or "").strip()
    loaded = await _load_ro_snapshot(settings, identifier)
    if loaded.get("status") != "verified":
        return {
            "mode": "ro_requirements",
            "success": False,
            "verified": False,
            **loaded,
        }
    snapshot = dict(loaded["snapshot"])
    map_info = await _discover_adas_map(_catalog_for(adas), snapshot)
    context = _valid_context(args.get(_CONTEXT_KEY))
    snapshot, planned, reconciliation = await _reconcile_one(
        settings, adas, snapshot, map_info, context
    )
    requirements = _active_ciq_requirements(snapshot)
    reconciliation_issues = (
        _reconciliation_issues(snapshot, map_info)
        if map_info.get("status") == "verified"
        else []
    )
    reconcile_ok = bool(
        map_info.get("status") != "verified"
        or (
            not reconciliation_issues
            and (
                not planned
                or (
                    reconciliation is not None
                    and reconciliation.get("verified") is True
                )
            )
        )
    )
    reconciliation_executed = bool(
        reconciliation is not None and reconciliation.get("executed") is True
    )
    return {
        "status": "success" if reconcile_ok else "partial_success",
        "mode": "ro_requirements",
        "executed": reconciliation_executed,
        "success": reconcile_ok,
        "verified": reconcile_ok,
        "snapshot_verified": True,
        "repair_order_id": str(
            calibration_iq._authoritative_repair_order_id(snapshot) or identifier
        ),  # noqa: SLF001
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
        "reconciliation_issues": reconciliation_issues,
    }


async def _phase_list(settings: Any, args: dict[str, Any]) -> dict[str, Any]:
    filters = {"phase": str(args.get("phase") or "").strip(), "limit": 100}
    if args.get("shop"):
        filters["shop"] = str(args["shop"])
    result = await calibration_iq.read_repair_orders(settings, filters)
    return {"mode": "phase_list", **result}


_WEEK_READY_DEFAULT_PHASES = frozenset({"5", "6", "7", "8"})


def _row_phase_token(row: dict[str, Any]) -> Optional[str]:
    phase = calibration_iq._phase_of(row)  # noqa: SLF001
    if phase is None:
        return None
    token = str(phase).strip()
    try:
        token = str(int(float(token)))
    except (TypeError, ValueError):
        pass
    return token or None


def _row_is_source_active(row: dict[str, Any]) -> bool:
    """Honor an explicit CIQ source-presence tombstone, if one is present."""
    active = calibration_iq._dig(  # noqa: SLF001
        row,
        "source_presence.active_on_source",
        "active_on_source",
        default=None,
    )
    return active is not False


_COMPACT_TEXT_LIMIT = 320
_COMPACT_LIST_LIMIT = 12
_COMPACT_RECEIPT_LIMIT = 12
_READINESS_ROWS_BYTE_BUDGET = 180_000


def _compact_text(value: Any, *, limit: int = _COMPACT_TEXT_LIMIT) -> str:
    return " ".join(str(value or "").split())[:limit]


def _compact_error(value: Any) -> Any:
    if not isinstance(value, dict):
        return _compact_text(value)
    compact = {
        key: value[key]
        for key in ("code", "category", "retryable", "status_code")
        if key in value
    }
    if value.get("message") is not None:
        compact["message"] = _compact_text(value.get("message"))
    details = value.get("details")
    if isinstance(details, dict) and "status_code" in details:
        compact["status_code"] = details["status_code"]
    return compact


def _compact_verification(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    compact = {
        key: value[key]
        for key in (
            "verified",
            "source",
            "resource_type",
            "resource_id",
            "observed_version",
            "reason",
        )
        if key in value
    }
    if value.get("message") is not None:
        compact["message"] = _compact_text(value.get("message"))
    if value.get("error") is not None:
        compact["error"] = _compact_error(value.get("error"))
    for key, item in list(compact.items()):
        if isinstance(item, str):
            compact[key] = _compact_text(item)
    return compact


def _compact_receipt(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    compact = {
        key: value[key]
        for key in (
            "mutation_id",
            "idempotency_key",
            "correlation_id",
            "operation",
            "risk",
            "status",
            "success",
            "verified",
            "replayed",
            "may_have_executed",
            "indeterminate",
            "repair_order_id",
            "target_id",
            "resource_type",
            "resource_id",
        )
        if key in value
    }
    for key, item in list(compact.items()):
        if isinstance(item, str):
            compact[key] = _compact_text(item)
    if value.get("verification") is not None:
        compact["verification"] = _compact_verification(value.get("verification"))
    if value.get("error") is not None:
        compact["error"] = _compact_error(value.get("error"))
    return compact


def _receipt_is_critical(value: dict[str, Any]) -> bool:
    verification = value.get("verification")
    return bool(
        value.get("indeterminate") is True
        or value.get("may_have_executed") is True
        or value.get("success") is not True
        or value.get("status") not in {"completed", "succeeded"}
        or not isinstance(verification, dict)
        or verification.get("verified") is not True
    )


def _compact_reconciliation_result(
    value: Any, *, receipt_limit: int = _COMPACT_RECEIPT_LIMIT
) -> Optional[dict[str, Any]]:
    """Keep receipt truth without repeating authoritative snapshots per RO.

    Operator results can contain a full Calibration IQ snapshot for every
    reconciled repair order.  A weekly audit already rereads and evaluates
    those snapshots before reaching this boundary, so returning all of them
    again can exceed the tool gateway's result limit and turn a verified audit
    into a synthetic ``truncated`` result.  Receipts and outcome fields remain
    intact; only the duplicated reread payload is omitted.
    """
    if not isinstance(value, dict):
        return None
    compact: dict[str, Any] = {
        key: value[key]
        for key in (
            "status",
            "executed",
            "success",
            "verified",
            "partial",
            "requested_count",
            "processed_count",
            "verified_count",
            "stopped_on_error",
            "research_reopen_attempted",
            "research_reopened",
            "research_version_before",
            "research_version_after",
            "may_have_executed",
            "indeterminate",
            "verification_recovered_by_reread",
        )
        if key in value
    }
    raw_receipts = [
        item for item in (value.get("receipts") or []) if isinstance(item, dict)
    ]
    raw_receipts = [
        item
        for _index, item in sorted(
            enumerate(raw_receipts),
            key=lambda pair: (
                not _receipt_is_critical(pair[1]),
                pair[0],
            ),
        )
    ]
    receipts = [_compact_receipt(item) for item in raw_receipts]
    compact["receipt_count"] = len(receipts)
    compact["critical_receipt_count"] = sum(
        1
        for item in raw_receipts
        if _receipt_is_critical(item)
    )
    compact["receipts"] = receipts[:receipt_limit]
    compact["receipts_shown"] = len(compact["receipts"])
    compact["receipts_truncated"] = len(receipts) > receipt_limit
    compact["receipt_sample_order"] = "critical_first"
    if value.get("message") is not None:
        compact["message"] = _compact_text(value.get("message"))
    if value.get("error") is not None:
        compact["error"] = _compact_error(value.get("error"))
    reread = value.get("authoritative_reread")
    if isinstance(reread, dict):
        compact["authoritative_reread"] = {
            key: reread[key]
            for key in ("status", "success", "verified")
            if key in reread
        }
        if reread.get("message") is not None:
            compact["authoritative_reread"]["message"] = _compact_text(
                reread.get("message")
            )
        if reread.get("error") is not None:
            compact["authoritative_reread"]["error"] = _compact_error(
                reread.get("error")
            )
    return compact


def _compact_evidence_item(
    value: Any, *, include_documents: bool = True
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    compact = {
        key: value[key] for key in ("state", "available") if key in value
    }
    if value.get("calibration") is not None:
        compact["calibration"] = _compact_text(value.get("calibration"))
    documents = [
        _compact_text(item, limit=240)
        for item in (value.get("documents") or [])
        if _compact_text(item, limit=240)
    ]
    compact["document_count"] = len(documents)
    if include_documents:
        compact["documents"] = documents[:3]
        compact["documents_truncated"] = len(documents) > 3
    if value.get("reason") is not None:
        compact["reason"] = _compact_text(value.get("reason"))
    return compact


def _compact_map_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "unverified"}
    sources: list[dict[str, Any]] = []
    for source in value.get("sources") or []:
        if not isinstance(source, dict):
            continue
        compact_source = {
            key: source[key]
            for key in (
                    "kind",
                    "item_id",
                    "inspection_id",
                    "source_url",
                    "artifact_kind",
                    "relative_path",
                    "sha256",
                    "readable",
                    "identity_verified",
            )
            if key in source
        }
        for key, item in list(compact_source.items()):
            if isinstance(item, str):
                compact_source[key] = _compact_text(item, limit=320)
        sources.append(compact_source)
    artifact_index = value.get("artifact_index")
    compact_index = None
    if isinstance(artifact_index, dict):
        errors = [
            _compact_error(item)
            for item in (artifact_index.get("errors") or [])
        ]
        compact_index = {
            key: artifact_index[key]
            for key in (
                "status",
                "scan_complete",
                "physical_pdf_count",
                "unreadable",
            )
            if key in artifact_index
        }
        compact_index["error_count"] = len(errors)
        compact_index["errors"] = errors[:3]
        compact_index["errors_truncated"] = len(errors) > 3
    compact = {
        key: value[key]
        for key in (
            "status",
            "discovery_status",
            "governing_source",
            "requirement_count",
            "explicit_no_calibration",
            "inspection_id",
            "vin",
            "vehicle",
            "reason",
            "identity_conflicts",
        )
        if key in value
    }
    if compact.get("reason") is not None:
        compact["reason"] = _compact_text(compact["reason"])
    if isinstance(compact.get("vehicle"), dict):
        compact["vehicle"] = {
            key: _compact_text(item) if isinstance(item, str) else item
            for key, item in compact["vehicle"].items()
            if key in {"year", "make", "model", "trim", "configuration"}
        }
    conflicts = [
        _compact_error(item) for item in (value.get("identity_conflicts") or [])
    ]
    compact["identity_conflict_count"] = len(conflicts)
    compact["identity_conflicts"] = conflicts[:3]
    compact["identity_conflicts_truncated"] = len(conflicts) > 3
    compact["source_count"] = len(sources)
    compact["sources"] = sources[:4]
    compact["sources_truncated"] = len(sources) > 4
    if compact_index is not None:
        compact["artifact_index"] = compact_index
    return compact


def _compact_action(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    compact = {
        key: value[key]
        for key in (
            "operation",
            "target_id",
            "repair_order_id",
            "expected_version",
            "idempotency_key",
        )
        if key in value
    }
    arguments = value.get("arguments")
    if isinstance(arguments, dict):
        compact["arguments"] = {
            key: _compact_text(item) if isinstance(item, str) else item
            for key, item in arguments.items()
            if key
            in {
                "calibration_type",
                "determination",
                "method",
                "research_status",
            }
        }
    return compact


def _compact_issue(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"message": _compact_text(value)}
    return {
        key: _compact_text(item) if isinstance(item, str) else item
        for key, item in value.items()
        if key in {"code", "calibration", "message"}
    }


def _compact_label_list(value: Any) -> tuple[list[str], int]:
    labels = [
        _compact_text(item, limit=200)
        for item in (value or [])
        if _compact_text(item, limit=200)
    ]
    return labels[:_COMPACT_LIST_LIMIT], len(labels)


def _compact_readiness_row(
    row: dict[str, Any], *, minimal: bool = False
) -> dict[str, Any]:
    """Return the bounded, inspectable form persisted by the tool gateway."""
    compact = {
        key: row[key]
        for key in (
            "repair_order_id",
            "ro_number",
            "vehicle",
            "status",
            "ready",
            "coverage_status",
        )
        if key in row
    }
    for key in ("repair_order_id", "ro_number", "vehicle", "status"):
        if isinstance(compact.get(key), str):
            compact[key] = _compact_text(compact[key])
    if row.get("message") is not None:
        compact["message"] = _compact_text(row.get("message"))
    requirements, requirement_count = _compact_label_list(
        row.get("calibration_requirements")
    )
    ciq_requirements, ciq_requirement_count = _compact_label_list(
        row.get("ciq_calibration_requirements")
    )
    compact["calibration_requirement_count"] = requirement_count
    compact["calibration_requirements"] = requirements
    compact["ciq_calibration_requirement_count"] = ciq_requirement_count
    compact["ciq_calibration_requirements"] = ciq_requirements
    actions = [
        _compact_action(item)
        for item in (row.get("reconciliation_actions") or [])
        if isinstance(item, dict)
    ]
    action_limit = 3 if minimal else _COMPACT_LIST_LIMIT
    compact["reconciliation_action_count"] = len(actions)
    compact["reconciliation_actions"] = actions[:action_limit]
    compact["reconciliation_actions_truncated"] = len(actions) > action_limit
    issues = [
        _compact_issue(item)
        for item in (row.get("reconciliation_issues") or [])
    ]
    issue_limit = 4 if minimal else _COMPACT_LIST_LIMIT
    compact["reconciliation_issue_count"] = len(issues)
    compact["reconciliation_issues"] = issues[:issue_limit]
    compact["reconciliation_issues_truncated"] = len(issues) > issue_limit
    compact["adas_map"] = _compact_map_result(row.get("adas_map"))
    if minimal:
        compact["adas_map"] = {
            key: compact["adas_map"][key]
            for key in ("status", "inspection_id", "reason")
            if key in compact["adas_map"]
        }
    compact["reconciliation"] = _compact_reconciliation_result(
        row.get("reconciliation"), receipt_limit=3 if minimal else _COMPACT_RECEIPT_LIMIT
    )
    coverage = [
        _compact_evidence_item(item, include_documents=not minimal)
        for item in (row.get("coverage") or [])
    ]
    coverage_limit = 4 if minimal else _COMPACT_LIST_LIMIT
    compact["coverage_count"] = len(coverage)
    compact["coverage"] = coverage[:coverage_limit]
    compact["coverage_truncated"] = len(coverage) > coverage_limit
    missing_si = [
        _compact_evidence_item(item, include_documents=False)
        for item in (row.get("missing_si") or [])
    ]
    compact["missing_si_count"] = len(missing_si)
    compact["missing_si"] = missing_si[:_COMPACT_LIST_LIMIT]
    compact["missing_si_truncated"] = len(missing_si) > _COMPACT_LIST_LIMIT
    unverified_si = [
        _compact_evidence_item(item, include_documents=False)
        for item in (row.get("unverified_si") or [])
    ]
    compact["unverified_si_count"] = len(unverified_si)
    compact["unverified_si"] = unverified_si[:_COMPACT_LIST_LIMIT]
    compact["unverified_si_truncated"] = len(unverified_si) > _COMPACT_LIST_LIMIT
    return compact


def _readiness_row_skeleton(row: dict[str, Any]) -> dict[str, Any]:
    compact = _compact_readiness_row(row, minimal=True)
    reconciliation = compact.get("reconciliation")
    if isinstance(reconciliation, dict):
        reconciliation = _compact_reconciliation_result(
            row.get("reconciliation"), receipt_limit=1
        )
    skeleton = {
        key: compact[key]
        for key in (
            "repair_order_id",
            "ro_number",
            "vehicle",
            "status",
            "ready",
            "coverage_status",
            "adas_map",
            "missing_si_count",
            "missing_si",
            "missing_si_truncated",
            "unverified_si_count",
            "unverified_si",
            "unverified_si_truncated",
            "reconciliation_issue_count",
        )
        if key in compact
    } | {"reconciliation": reconciliation}
    for key in ("missing_si", "unverified_si"):
        values = list(skeleton.get(key) or [])
        skeleton[key] = values[:3]
        skeleton[f"{key}_truncated"] = int(skeleton.get(f"{key}_count") or 0) > 3
    return skeleton


def _bounded_readiness_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    compact = [_compact_readiness_row(row) for row in rows]
    if len(json.dumps(compact, ensure_ascii=False, default=str).encode("utf-8")) <= _READINESS_ROWS_BYTE_BUDGET:
        return compact, {
            "repair_orders_total": len(rows),
            "repair_orders_shown": len(compact),
            "repair_orders_truncated": False,
        }

    minimal = [_compact_readiness_row(row, minimal=True) for row in rows]
    if len(json.dumps(minimal, ensure_ascii=False, default=str).encode("utf-8")) <= _READINESS_ROWS_BYTE_BUDGET:
        return minimal, {
            "repair_orders_total": len(rows),
            "repair_orders_shown": len(minimal),
            "repair_orders_truncated": False,
            "repair_order_detail_compacted": True,
        }

    skeletons = [_readiness_row_skeleton(row) for row in rows]
    if len(json.dumps(skeletons, ensure_ascii=False, default=str).encode("utf-8")) <= _READINESS_ROWS_BYTE_BUDGET:
        return skeletons, {
            "repair_orders_total": len(rows),
            "repair_orders_shown": len(skeletons),
            "repair_orders_truncated": False,
            "repair_order_detail_compacted": True,
            "repair_order_skeletons": True,
        }

    # Preserve executed/exception rows first, then include as many remaining
    # rows as the explicit result budget permits. Counts above remain complete,
    # and omission is always declared rather than gateway-truncated silently.
    prioritized = sorted(
        enumerate(skeletons),
        key=lambda pair: (
            (pair[1].get("reconciliation") or {}).get("executed") is not True,
            pair[1].get("ready") is True,
            pair[0],
        ),
    )
    selected: list[dict[str, Any]] = []
    used = 2
    for _index, row in prioritized:
        encoded = len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8"))
        if selected and used + encoded + 1 > _READINESS_ROWS_BYTE_BUDGET:
            continue
        if not selected and encoded + 2 > _READINESS_ROWS_BYTE_BUDGET:
            continue
        selected.append(row)
        used += encoded + (1 if len(selected) > 1 else 0)
    selected_identities = {
        (str(row.get("repair_order_id") or ""), str(row.get("ro_number") or ""))
        for row in selected
    }
    omitted_rows = [
        row
        for row in skeletons
        if (str(row.get("repair_order_id") or ""), str(row.get("ro_number") or ""))
        not in selected_identities
    ]
    omitted_executed = [
        row
        for row in omitted_rows
        if (row.get("reconciliation") or {}).get("executed") is True
    ]
    omitted_executed_ids = [
        str(row.get("ro_number") or row.get("repair_order_id") or "")
        for row in omitted_executed
    ]
    return selected, {
        "repair_orders_total": len(rows),
        "repair_orders_shown": len(selected),
        "repair_orders_truncated": len(selected) < len(rows),
        "repair_order_detail_compacted": True,
        "repair_orders_omitted": len(rows) - len(selected),
        "executed_repair_orders_omitted": len(omitted_executed),
        "executed_repair_order_identities": omitted_executed_ids[:50],
        "executed_repair_order_identities_truncated": len(omitted_executed_ids) > 50,
    }


async def _week_readiness(
    settings: Any, adas: Any, args: dict[str, Any]
) -> dict[str, Any]:
    # "Prepared for the week" means phases 5-8: earlier phases (teardown,
    # estimate, parts) aren't reaching calibration this week, so checking
    # their SI coverage now would be noise. An explicit phase in the request
    # overrides this default instead of being narrowed further by it.
    explicit_phase = bool(args.get("phase"))
    # The upstream collection's default scope is Calibration IQ Active Work,
    # which deliberately includes terminal-status ROs still marked
    # active_on_source.  Keep those rows instead of locally dropping them by
    # status after Calibration IQ has already declared them active.
    filters: dict[str, Any] = {"include_completed": True}
    if explicit_phase:
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
            "message": queue.get("message")
            or "Calibration IQ did not return a complete active queue.",
            "calibration_iq": queue,
        }
    rows = [
        item
        for item in (queue.get("items") or [])
        if isinstance(item, dict) and _row_is_source_active(item)
    ]
    if not explicit_phase:
        rows = [
            row for row in rows if _row_phase_token(row) in _WEEK_READY_DEFAULT_PHASES
        ]
    context = _valid_context(args.get(_CONTEXT_KEY))
    catalog = _catalog_for(adas)

    semaphore = asyncio.Semaphore(6)

    async def load(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        ident = str(
            calibration_iq._dig(
                item,
                "id",
                "repair_order_id",
                "uuid",
                "ro_number",
                "roNumber",
                "number",
                "ro",
            )
            or ""
        ).strip()  # noqa: SLF001
        async with semaphore:
            return item, await _load_ro_snapshot(settings, ident)

    loaded = await asyncio.gather(*(load(item) for item in rows))
    results: list[dict[str, Any]] = []
    added_total = 0
    executed_any = False
    processed_total = 0
    receipt_total = 0
    verified_receipt_total = 0
    for row, envelope in loaded:
        if envelope.get("status") != "verified":
            results.append(
                {
                    "ro_number": str(
                        calibration_iq._dig(
                            row, "ro_number", "roNumber", "number", "ro"
                        )
                        or ""
                    ),  # noqa: SLF001
                    "vehicle": calibration_iq._vehicle_label(row),  # noqa: SLF001
                    "ready": False,
                    "status": "ro_unavailable",
                    "adas_map": {"status": "unverified"},
                    "coverage_status": adas_artifact_catalog.UNVERIFIED,
                    "missing_si": [],
                    "unverified_si": [],
                    "message": envelope.get("message"),
                }
            )
            continue
        snapshot = dict(envelope["snapshot"])
        map_info = await _discover_adas_map(catalog, snapshot)
        snapshot, planned, reconciliation = await _reconcile_one(
            settings, adas, snapshot, map_info, context
        )
        if reconciliation is not None:
            receipts = [
                item
                for item in (reconciliation.get("receipts") or [])
                if isinstance(item, dict)
            ]
            receipt_total += len(receipts)
            verified_receipt_total += sum(
                1
                for item in receipts
                if item.get("status") == "completed"
                and item.get("success") is True
                and isinstance(item.get("verification"), dict)
                and item["verification"].get("verified") is True
            )
            processed = reconciliation.get("processed_count")
            if (
                not isinstance(processed, int)
                or isinstance(processed, bool)
                or processed < 0
            ):
                processed = len(receipts)
            processed_total += processed
            if reconciliation.get("executed") is True:
                executed_any = True
            if reconciliation.get("verified") is True:
                added_total += len(planned)
        requirements = [
            dict(item)
            for item in (map_info.get("requirements") or [])
            if isinstance(item, dict)
        ]
        vehicle = _vehicle_label(snapshot, calibration_iq._vehicle_label(row))  # noqa: SLF001
        coverage = await _catalog_coverage(catalog, snapshot, map_info)
        missing_si = [
            item
            for item in coverage
            if item.get("state") == adas_artifact_catalog.MISSING
        ]
        unverified_si = [
            item
            for item in coverage
            if item.get("state") == adas_artifact_catalog.UNVERIFIED
        ]
        map_ok = map_info.get("status") == "verified"
        reconciliation_issues = (
            _reconciliation_issues(snapshot, map_info) if map_ok else []
        )
        reconcile_ok = bool(
            map_ok
            and not reconciliation_issues
            and (
                not planned
                or (
                    reconciliation is not None
                    and reconciliation.get("verified") is True
                )
            )
        )
        if not map_ok:
            coverage_status = adas_artifact_catalog.UNVERIFIED
        elif map_info.get("explicit_no_calibration") is True:
            coverage_status = adas_artifact_catalog.COVERED
        elif missing_si:
            coverage_status = adas_artifact_catalog.MISSING
        elif unverified_si or not requirements:
            coverage_status = adas_artifact_catalog.UNVERIFIED
        else:
            coverage_status = adas_artifact_catalog.COVERED
        ready = bool(
            map_ok and reconcile_ok and coverage_status == adas_artifact_catalog.COVERED
        )
        if ready:
            status = "ready"
        elif not map_ok:
            status = "adas_map_unverified"
        elif not reconcile_ok:
            status = "reconciliation_failed"
        elif coverage_status == adas_artifact_catalog.MISSING:
            status = "si_missing"
        else:
            status = "si_unverified"
        results.append(
            {
                "repair_order_id": str(
                    calibration_iq._authoritative_repair_order_id(snapshot) or ""
                ),  # noqa: SLF001
                "ro_number": _ro_number(
                    snapshot,
                    str(
                        calibration_iq._dig(
                            row, "ro_number", "roNumber", "number", "ro"
                        )
                        or ""
                    ),
                ),  # noqa: SLF001
                "vehicle": vehicle,
                "status": status,
                "ready": ready,
                "adas_map": map_info,
                "calibration_requirements": [
                    _requirement_label(item) for item in requirements
                ],
                "ciq_calibration_requirements": [
                    _requirement_label(item)
                    for item in _active_ciq_requirements(snapshot)
                ],
                "reconciliation_actions": planned,
                "reconciliation": reconciliation,
                "reconciliation_issues": reconciliation_issues,
                "coverage": coverage,
                "coverage_status": coverage_status,
                "missing_si": missing_si,
                "unverified_si": unverified_si,
            }
        )

    if context is not None:
        _save_weekly_queue(settings, context["conversation_id"], results)

    exception_count = sum(1 for item in results if item.get("ready") is not True)
    reconciliation_failed_count = sum(
        1 for item in results if item.get("status") == "reconciliation_failed"
    )
    ro_unavailable_count = sum(
        1 for item in results if item.get("status") == "ro_unavailable"
    )
    ready_count = sum(1 for item in results if item.get("ready") is True)
    si_covered_count = sum(
        1
        for item in results
        if item.get("coverage_status") == adas_artifact_catalog.COVERED
    )
    si_missing_count = sum(
        1
        for item in results
        if item.get("coverage_status") == adas_artifact_catalog.MISSING
    )
    si_unverified_count = sum(
        1
        for item in results
        if item.get("coverage_status") == adas_artifact_catalog.UNVERIFIED
    )
    adas_map_verified_count = sum(
        1
        for item in results
        if (item.get("adas_map") or {}).get("status") == "verified"
    )
    adas_map_missing_count = sum(
        1
        for item in results
        if (item.get("adas_map") or {}).get("status") == "not_found"
    )
    adas_map_unverified_count = (
        len(results) - adas_map_verified_count - adas_map_missing_count
    )
    queue_candidates = sum(1 for item in results if item.get("missing_si"))
    public_results, public_result_meta = _bounded_readiness_rows(results)
    return {
        "status": "success" if exception_count == 0 else "partial_success",
        "mode": "week_readiness",
        "executed": executed_any,
        "success": True,
        "verified": True,
        # `success`/`verified` describe the audit execution.  Readiness is a
        # separate fact: an accurately verified audit can still prove that one
        # or more ROs need attention.
        "readiness_complete": exception_count == 0,
        "exception_count": exception_count,
        "queue_count": len(results),
        "ready_count": ready_count,
        "si_covered_count": si_covered_count,
        "needs_si_count": si_missing_count,
        "si_missing_count": si_missing_count,
        "si_unverified_count": si_unverified_count,
        "adas_map_verified_count": adas_map_verified_count,
        "adas_map_missing_count": adas_map_missing_count,
        "adas_map_unverified_count": adas_map_unverified_count,
        "adas_map_unavailable_count": (
            adas_map_missing_count + adas_map_unverified_count
        ),
        "alldata_queued_count": queue_candidates if context is not None else 0,
        "acquisition_status": (
            "queued"
            if context is not None and queue_candidates
            else "not_needed"
            if not queue_candidates
            else "not_queued_context_missing"
        ),
        "reconciliation_failed_count": reconciliation_failed_count,
        "ro_unavailable_count": ro_unavailable_count,
        "ciq_mutations_processed_count": processed_total,
        "ciq_receipt_count": receipt_total,
        "ciq_verified_receipt_count": verified_receipt_total,
        "ciq_requirements_added_or_reactivated": added_total,
        "filters": filters,
        "phase_scope": [str(args["phase"])]
        if explicit_phase
        else sorted(_WEEK_READY_DEFAULT_PHASES, key=int),
        **public_result_meta,
        "repair_orders": public_results,
    }


async def _phase_coverage(
    settings: Any, adas: Any, args: dict[str, Any]
) -> dict[str, Any]:
    """Audit one explicit CIQ phase through the full ADAS Map/SI path."""
    phase = str(args.get("phase") or "").strip()
    if not phase:
        return {
            "status": "invalid_request",
            "mode": "phase_coverage",
            "success": False,
            "verified": False,
            "message": "Tell me which Calibration IQ phase to check for ADAS SI coverage.",
        }
    coverage_focus = str(args.get("coverage_focus") or "si_readiness").casefold()
    if coverage_focus not in {"adas_map", "si_readiness"}:
        coverage_focus = "si_readiness"
    result = await _week_readiness(settings, adas, {**args, "phase": phase})
    return {
        **result,
        "mode": "phase_coverage",
        "coverage_focus": coverage_focus,
    }


def _save_weekly_queue(settings: Any, conversation_id: int, results: list[dict[str, Any]]) -> None:
    """Remember which ROs still need ADAS SI so "next" can walk the list
    without the operator repeating the RO or vehicle every turn."""
    items: list[weekly_queue.WeeklyQueueItem] = []
    for row in results:
        repair_order_id = str(row.get("repair_order_id") or "").strip()
        missing = [
            str(entry.get("calibration") or "").strip()
            for entry in (row.get("missing_si") or [])
            if isinstance(entry, dict) and entry.get("calibration")
        ]
        if not repair_order_id or not missing:
            continue
        vehicle_label = str(row.get("vehicle") or "").strip()
        parsed_vehicle = nav.vehicle_from_query(vehicle_label)
        items.append(weekly_queue.WeeklyQueueItem(
            repair_order_id=repair_order_id,
            ro_number=str(row.get("ro_number") or ""),
            vehicle_label=vehicle_label,
            vehicle_year=parsed_vehicle.get("year"),
            vehicle_make=parsed_vehicle.get("make"),
            vehicle_model_trim=parsed_vehicle.get("model_trim"),
            missing_calibrations=missing,
        ))
    store = weekly_queue.get_store(Path(settings.root))
    if not items:
        store.clear(str(conversation_id))
        return
    store.save(weekly_queue.WeeklyQueue(conversation_id=str(conversation_id), items=items))


async def handle(settings: Any, adas: Any, args: dict[str, Any]) -> dict[str, Any]:
    mode = str(args.get("mode") or "").strip().casefold()
    if mode == "phase_list":
        return await _phase_list(settings, args)
    if mode == "ro_requirements":
        return await _ro_requirements(settings, adas, args)
    if mode == "week_readiness":
        return await _week_readiness(settings, adas, args)
    if mode == "phase_coverage":
        return await _phase_coverage(settings, adas, args)
    if mode == "queue_next":
        return await _queue_next_mode(settings, adas, args)
    raise ValueError("Unsupported Calibration IQ work-prep mode.")


async def _queue_next_mode(settings: Any, adas: Any, args: dict[str, Any]) -> dict[str, Any]:
    context = _valid_context(args.get(_CONTEXT_KEY))
    if context is None:
        return {
            "status": "context_missing",
            "mode": "queue_next",
            "success": False,
            "verified": False,
            "message": "X could not identify the active conversation for the weekly readiness queue.",
        }
    result = await resolve_queue_next(settings, adas, context["conversation_id"])
    return {"mode": "queue_next", **result}


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


def _queue_item_matches_signals(item: "weekly_queue.WeeklyQueueItem", signals: list[str]) -> bool:
    vehicle = {"year": item.vehicle_year, "make": item.vehicle_make, "model_trim": item.vehicle_model_trim}
    if not (vehicle["year"] and vehicle["make"] and vehicle["model_trim"]):
        return False
    return any(quick._identity_matches_text(signal, vehicle) for signal in signals)  # noqa: SLF001


async def resolve_queue_next(settings: Any, adas: Any, conversation_id: int) -> dict[str, Any]:
    """Walk the weekly-readiness missing-SI queue: resolve whatever vehicle
    is currently selected in ALLDATA against the remaining pending items,
    collect for it if it matches, mark it complete, and report which
    vehicle to pull up next -- so "next" replaces repeating the RO or
    vehicle every turn.
    """
    store = weekly_queue.get_store(Path(settings.root))
    queue = store.get(str(conversation_id))
    if queue is None or not queue.items:
        return {
            "status": "no_active_queue",
            "success": False,
            "verified": False,
            "message": (
                "There's no active weekly readiness queue for this conversation. "
                "Run \"make sure we're prepared for the week\" first."
            ),
        }
    if queue.is_stale():
        return {
            "status": "queue_stale",
            "success": False,
            "verified": False,
            "message": (
                "The weekly readiness queue from earlier is stale. "
                "Run \"make sure we're prepared for the week\" again."
            ),
        }
    pending = queue.pending()
    if not pending:
        return {
            "status": "queue_complete",
            "success": True,
            "verified": True,
            "message": "The weekly readiness queue is complete -- every RO that needed ADAS SI has been collected.",
        }

    browser = research_operator.get_browser(Path(settings.root), adas=adas)
    state = await browser.start(auto_login=False)
    if not state.get("authenticated") or browser._page is None:  # noqa: SLF001
        return {
            "status": "human_action_required",
            "success": False,
            "verified": False,
            "message": "Open/resume the ALLDATA browser and sign in first.",
        }
    signals = await _bounded_selected_vehicle_signals(browser._page)  # noqa: SLF001
    if not signals:
        remaining = "; ".join(f"RO {item.ro_number} — {item.vehicle_label}" for item in pending[:3])
        return {
            "status": "vehicle_selection_required",
            "success": False,
            "verified": False,
            "message": (
                f"X could not read a bounded selected-vehicle signal from ALLDATA. "
                f"Select the next vehicle first. Remaining: {remaining}."
            ),
        }

    matching = [item for item in pending if _queue_item_matches_signals(item, signals)]
    if not matching:
        remaining = "; ".join(f"RO {item.ro_number} — {item.vehicle_label}" for item in pending[:5])
        return {
            "status": "not_in_queue",
            "success": False,
            "verified": False,
            "signals": signals,
            "message": (
                "The selected ALLDATA vehicle is not in the remaining weekly readiness queue. "
                f"Remaining: {remaining}."
            ),
        }
    if len(matching) > 1:
        # Two identical vehicle models both queued this week -- never guess
        # which RO the collected evidence belongs to.
        candidates = "; ".join(f"RO {item.ro_number} — {item.vehicle_label}" for item in matching[:5])
        return {
            "status": "ambiguous_match",
            "success": False,
            "verified": False,
            "signals": signals,
            "message": (
                "More than one queued RO matches this vehicle; name the RO number before collecting. "
                f"Candidates: {candidates}."
            ),
        }
    matched = matching[0]

    result = await quick.collect_for_calibration_iq_ro(settings, adas, {"repair_order_id": matched.repair_order_id})
    verified = isinstance(result, dict) and result.get("verified") is True and result.get("success") is True
    matched.status = "complete" if verified else "failed"
    store.save(queue)

    remaining_after = queue.pending()
    done_count = sum(1 for item in queue.items if item.status == "complete")
    total = len(queue.items)
    if remaining_after:
        next_item = remaining_after[0]
        next_line = (
            f"Next: RO {next_item.ro_number} — {next_item.vehicle_label} "
            f"(needs {', '.join(next_item.missing_calibrations) or 'SI'})."
        )
    else:
        next_line = "That was the last one -- the weekly readiness queue is complete."
    lead = (
        f"RO {matched.ro_number} — {matched.vehicle_label}: collected and verified."
        if verified
        else f"RO {matched.ro_number} — {matched.vehicle_label}: collection was not verified."
    )
    return {
        "status": "success" if verified else "collection_failed",
        "success": verified,
        "verified": verified,
        "repair_order_id": matched.repair_order_id,
        "vehicle": matched.vehicle_label,
        "done_count": done_count,
        "total_count": total,
        "collector_result": result,
        "message": f"{lead} {done_count} of {total} done. {next_line}",
    }


_SUMMARY_EXCEPTION_LIMIT = 3
_SUMMARY_CALIBRATION_LIMIT = 3


def _summary_value(value: object, *, limit: int = 140) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _readiness_exception_line(
    item: dict[str, Any], *, map_focus: bool = False
) -> str:
    ro_number = _summary_value(item.get("ro_number"), limit=80) or "unknown"
    vehicle = _summary_value(item.get("vehicle"), limit=160) or "vehicle unavailable"
    lead = f"RO {ro_number} — {vehicle}:"
    if item.get("status") == "ro_unavailable":
        return f"{lead} Calibration IQ detail could not be verified."
    map_status = str((item.get("adas_map") or {}).get("status") or "").casefold()
    if map_focus:
        if map_status == "not_found":
            return f"{lead} ADAS Map report is genuinely missing after a complete exact scan."
        if map_status == "ambiguous":
            return f"{lead} ADAS Map evidence is ambiguous."
        if map_status != "verified":
            return f"{lead} ADAS Map evidence is unverified."
    missing = [
        _summary_value(entry.get("calibration"), limit=120)
        for entry in (item.get("missing_si") or [])
        if isinstance(entry, dict)
        and _summary_value(entry.get("calibration"), limit=120)
    ]
    if missing:
        shown = missing[:_SUMMARY_CALIBRATION_LIMIT]
        suffix = (
            f" (+{len(missing) - len(shown)} more)" if len(missing) > len(shown) else ""
        )
        return f"{lead} needs ADAS SI for {', '.join(shown)}{suffix}."
    unverified = [
        _summary_value(entry.get("calibration"), limit=120)
        for entry in (item.get("unverified_si") or [])
        if isinstance(entry, dict)
        and _summary_value(entry.get("calibration"), limit=120)
    ]
    if unverified:
        shown = unverified[:_SUMMARY_CALIBRATION_LIMIT]
        suffix = (
            f" (+{len(unverified) - len(shown)} more)"
            if len(unverified) > len(shown)
            else ""
        )
        return (
            f"{lead} ADAS SI association is unverified for {', '.join(shown)}{suffix}."
        )
    if map_status != "verified":
        return f"{lead} ADAS Map requirement data could not be verified."
    if item.get("status") == "reconciliation_failed":
        return f"{lead} CIQ requirement reconciliation was not verified."
    return f"{lead} readiness was not verified."


def _readiness_summary(mode: str, data: dict[str, Any]) -> str:
    phase_scope = [
        _summary_value(value, limit=20)
        for value in (data.get("phase_scope") or [])
        if _summary_value(value, limit=20)
    ]
    if mode == "phase_coverage":
        phase = (
            phase_scope[0]
            if phase_scope
            else _summary_value((data.get("filters") or {}).get("phase"), limit=20)
        )
        scope = f"in Phase {phase}" if phase else "in the requested phase"
    elif len(phase_scope) > 1:
        scope = f"in weekly phases {phase_scope[0]}–{phase_scope[-1]}"
    elif phase_scope:
        scope = f"in Phase {phase_scope[0]}"
    else:
        scope = "in the weekly queue"

    rows = [
        item for item in (data.get("repair_orders") or []) if isinstance(item, dict)
    ]
    queue_count = int(data.get("queue_count") or 0)
    ready_count = int(data.get("ready_count") or 0)
    map_unavailable_count = int(data.get("adas_map_unavailable_count") or 0)
    map_missing_count = int(data.get("adas_map_missing_count") or 0)
    raw_map_unverified = data.get("adas_map_unverified_count")
    map_unverified_count = (
        int(raw_map_unverified)
        if isinstance(raw_map_unverified, int)
        and not isinstance(raw_map_unverified, bool)
        else max(0, map_unavailable_count - map_missing_count)
    )
    raw_map_verified = data.get("adas_map_verified_count")
    map_verified_count = (
        int(raw_map_verified)
        if isinstance(raw_map_verified, int) and not isinstance(raw_map_verified, bool)
        else max(0, queue_count - map_unavailable_count)
    )
    map_focus = mode == "phase_coverage" and data.get("coverage_focus") == "adas_map"
    if map_focus:
        exceptions = [
            item
            for item in rows
            if isinstance(item.get("adas_map"), dict)
            and item["adas_map"].get("status") != "verified"
        ]
        exception_count = max(
            map_unavailable_count,
            len(exceptions),
            max(0, queue_count - map_verified_count),
        )
        coverage_complete = exception_count == 0 and map_verified_count == queue_count
        if queue_count == 0 and coverage_complete:
            lead = f"Yes — there are no active Calibration IQ ROs {scope} to check."
        elif coverage_complete:
            lead = (
                f"Yes — all {queue_count} active Calibration IQ ROs {scope} "
                "have verified ADAS Map reports."
            )
        else:
            lead = (
                f"No — {exception_count} of {queue_count} active Calibration IQ ROs "
                f"{scope} do not have verified ADAS Map reports."
            )
    else:
        exceptions = [item for item in rows if item.get("ready") is not True]
        exception_count = max(
            int(data.get("exception_count") or 0),
            len(exceptions),
            max(0, queue_count - ready_count),
        )
        readiness_complete = data.get("readiness_complete")
        if not isinstance(readiness_complete, bool):
            readiness_complete = exception_count == 0 and ready_count == queue_count
        if queue_count == 0 and readiness_complete:
            lead = f"Yes — there are no active Calibration IQ ROs {scope} to prepare."
        elif readiness_complete:
            lead = (
                f"Yes — all {queue_count} active Calibration IQ ROs {scope} are SI-ready."
            )
        else:
            lead = (
                f"No — {exception_count} of {queue_count} active Calibration IQ ROs "
                f"{scope} are not yet SI-ready."
            )

    lines = [
        lead,
        (
            "ADAS Map: "
            f"{map_verified_count} verified; "
            f"{map_missing_count} genuinely missing; "
            f"{map_unverified_count} unverified."
        ),
        (
            "ADAS SI: "
            f"{int(data.get('si_covered_count') or ready_count)} fully covered; "
            f"{int(data.get('si_missing_count') or data.get('needs_si_count') or 0)} genuinely missing; "
            f"{int(data.get('si_unverified_count') or 0)} unverified."
        ),
        (
            "CIQ reconciliation: "
            f"{int(data.get('ciq_requirements_added_or_reactivated') or 0)} "
            "requirement(s) added/reactivated; "
            f"{int(data.get('reconciliation_failed_count') or 0)} unverified."
        ),
        f"ALLDATA: {int(data.get('alldata_queued_count') or 0)} vehicle(s) queued.",
    ]
    for item in exceptions[:_SUMMARY_EXCEPTION_LIMIT]:
        lines.append(_readiness_exception_line(item, map_focus=map_focus))
    omitted = max(0, exception_count - min(len(exceptions), _SUMMARY_EXCEPTION_LIMIT))
    if omitted:
        if data.get("repair_orders_truncated") is True:
            lines.append(
                f"{omitted} additional RO exception(s) remain in the verified aggregate counts; "
                "the structured row sample is explicitly marked truncated."
            )
        else:
            lines.append(
                f"{omitted} additional RO exception(s) are preserved in the structured readiness result."
            )
    return "\n".join(lines)


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
        detail = "; ".join(f"RO {row.get('RO')} — {row.get('Vehicle')}" for row in rows)
        return f"{lead} {detail}" if detail else lead
    if mode == "ro_requirements":
        if data.get("verified") is not True:
            return message or "Calibration IQ did not return a verified repair order."
        labels = [
            str(item.get("label") or "")
            for item in (data.get("calibration_requirements") or [])
            if isinstance(item, dict) and item.get("label")
        ]
        base = f"RO {data.get('ro_number') or data.get('repair_order_id')} — {data.get('vehicle') or 'vehicle'}"
        requirements = (
            ", ".join(labels)
            if labels
            else "no active calibration requirements recorded"
        )
        additions = len(data.get("reconciliation_actions") or [])
        suffix = (
            f" I reconciled and added/reactivated {additions} requirement(s) from governing ADAS Map."
            if additions and (data.get("reconciliation") or {}).get("verified") is True
            else ""
        )
        map_status = (data.get("adas_map") or {}).get("status")
        if map_status != "verified":
            suffix += " ADAS Map requirements were not machine-readable on this RO, so I did not invent any missing requirements."
        return f"{base}: {requirements}.{suffix}"
    if mode in {"week_readiness", "phase_coverage"}:
        if data.get("verified") is not True:
            fallback = (
                "The requested Phase coverage audit was not verified."
                if mode == "phase_coverage"
                else "The weekly Calibration IQ readiness audit was not verified."
            )
            return message or fallback
        return _readiness_summary(mode, data)
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
                    "Calibration IQ work-prep bridge. Use CIQ as the work queue, ADAS Map on each RO as the governing calibration-requirement source, and ADAS SI as procedure coverage. It can list a phase, run a full ADAS Map/SI coverage audit for one phase, report one RO's saved requirements, or audit/reconcile the active queue for weekly SI readiness. Missing CIQ calibrations proved by ADAS Map are added/reactivated through CIQ's verified routine operator contract."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "mode": {"type": "string", "enum": ["phase_list", "phase_coverage", "ro_requirements", "week_readiness", "queue_next"]},
                        "coverage_focus": {"type": "string", "enum": ["adas_map", "si_readiness"]},
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
                if resolved.get("status") == "ciq_vehicle_not_found":
                    # The selected vehicle is real and proven -- it just has
                    # no active Calibration IQ repair order yet. Not every
                    # vehicle pulled up in ALLDATA has one; save it as
                    # general ADAS SI reference material instead of failing
                    # closed, so the tech can select any car and still have
                    # X collect and store what they ask for.
                    general = await quick.collect_general_reference(settings, adas, args)
                    if isinstance(general, dict):
                        general.setdefault("resolved_calibration_iq", resolved)
                    return general
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
                    history = self.store.get_messages(conversation_id)
                    mode = None if approved_tool else classify_request(user_message, history)
                    if mode is None:
                        async for event in previous_run(
                            self, conversation_id, user_message, approved_tool, approval_context
                        ):
                            yield event
                        return

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
                        if mode == "phase_coverage":
                            tool_args["coverage_focus"] = _phase_coverage_focus(user_message)
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

                    # Tag the turn with the active ALLDATA acquisition stage so
                    # a later low-content follow-up ("retrieve SI information
                    # please", "go ahead") can resolve against it without
                    # needing its own exact wording. The artifact type has no
                    # registered UI card, so this is invisible in chat.
                    if mode == "alldata_access":
                        artifacts.append({
                            "type": _WORK_PREP_STATE_ARTIFACT,
                            "data": {"mode": _WORK_PREP_MODE, "stage": "awaiting_vehicle_selection"},
                        })
                    elif mode == "quick_reference":
                        payload = result if isinstance(result, dict) else {}
                        if payload.get("verified") is True and payload.get("success") is True:
                            stage = "complete"
                        elif payload.get("status") == "ciq_vehicle_ambiguous":
                            stage = "awaiting_ro_disambiguation"
                        else:
                            stage = "awaiting_vehicle_selection"
                        artifacts.append({
                            "type": _WORK_PREP_STATE_ARTIFACT,
                            "data": {"mode": _WORK_PREP_MODE, "stage": stage},
                        })

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
