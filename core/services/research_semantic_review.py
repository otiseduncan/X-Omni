"""Adversarial semantic review of one Navigator candidate, in a clean context.

The Navigator loop finds pages; it does not get to grade its own work. When
it marks a page as the procedure, this module puts the *evidence* -- the
original research objective, the exact vehicle, the page's own title,
breadcrumb, URL, extracted text, referenced documents, and a current
screenshot -- in front of the model in a fresh context that has never seen
the Navigator's reasoning, with an explicitly sceptical instruction, and asks
for ONE structured verdict.

That one verdict covers everything the review has to decide: what kind of
document the page is, whether it is for this vehicle, whether it performs the
requested system and operation, what else it requires, and the decision.
These used to be separate layers -- a keyword radar/camera veto in Python, a
second "objective match" model call, and a supporting-page boundary -- each
bolted on after a live failure and each re-judging the verdict before it. They
are one call now. The match is declared *before* the decision, because
llama.cpp's grammar emits properties in declared order, so the model has to
commit to what the page performs before it may decide anything.

The model decides what the page is and whether it satisfies the objective.
Python decides only whether the reply is well formed and internally
consistent with itself and with the workflow role Core already knows: an
acceptance that its own evidence table, vehicle match, or objective match
contradicts is not an acceptance, and a reply that cannot be parsed is never
one. Nothing here inspects titles, counts characters, or matches automotive
words.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger("xomni.research_semantic_review")

REVIEW_TOOL_NAME = "report_semantic_review"
REVIEW_TEXT_CHARS = 24_000
REVIEW_MAX_LINKS = 60
REVIEW_MAX_DEPENDENCIES = 6
REVIEW_MAX_TOKENS = 1_000

CLASSIFICATIONS: tuple[str, ...] = (
    "ACTUAL_PROCEDURE",
    "REQUIRED_SUPPORTING_PROCEDURE",
    "REMOVAL_REPLACEMENT",
    "DIAGNOSTIC_PROCEDURE",
    "GENERAL_DESCRIPTION",
    "WIRING_OR_COMPONENT_INFORMATION",
    "UNRELATED",
    "UNCERTAIN",
)
PROCEDURE_TYPES: tuple[str, ...] = (
    "STATIC_RADAR",
    "DYNAMIC_RADAR",
    "STATIC_CAMERA",
    "DYNAMIC_CAMERA",
    "SURROUND_VIEW",
    "BLIND_SPOT_RADAR",
    "INITIALIZATION_OR_RELEARN",
    "STEERING_ANGLE",
    "OCCUPANT_OR_SEAT",
    "PARKING_SENSOR",
    "OTHER",
    "NOT_A_PROCEDURE",
)
VEHICLE_MATCH: tuple[str, ...] = ("MATCHES", "DIFFERENT_VEHICLE", "NOT_STATED")
OBJECTIVE_MATCH: tuple[str, ...] = (
    "EXACT_MATCH",
    "SAME_COMPONENT_WRONG_PROCEDURE",
    "DIFFERENT_COMPONENT",
    "DIFFERENT_SENSOR_FAMILY",
    "UNCERTAIN",
)
EVIDENCE_FIELDS: tuple[str, ...] = (
    "prerequisites",
    "tools_or_equipment",
    "physical_setup",
    "geometry_or_measurements",
    "scan_tool_steps",
    "execution_steps",
    "completion_criteria",
)
EVIDENCE_STATUSES: tuple[str, ...] = (
    "PRESENT",
    "NOT_APPLICABLE",
    "REFERENCED_ELSEWHERE",
    "MISSING_OR_UNCERTAIN",
)
DECISIONS: tuple[str, ...] = (
    "ACCEPT",
    "ACCEPT_WITH_DEPENDENCIES",
    "FOLLOW_DEPENDENCY",
    "CONTINUE_SEARCH",
    "REJECT",
    "UNCERTAIN",
)
ACCEPTING_DECISIONS = frozenset({"ACCEPT", "ACCEPT_WITH_DEPENDENCIES"})
DEPENDENCY_DECISIONS = frozenset({"ACCEPT_WITH_DEPENDENCIES", "FOLLOW_DEPENDENCY"})
# What a document task may close, by the workflow role Core assigned it. A
# primary objective needs the procedure itself; a dependency task exists to
# retrieve one required supporting document, so that is what it may accept.
ACCEPTABLE_CLASSIFICATIONS_BY_ROLE: dict[str, frozenset[str]] = {
    "primary": frozenset({"ACTUAL_PROCEDURE"}),
    "dependency": frozenset({"ACTUAL_PROCEDURE", "REQUIRED_SUPPORTING_PROCEDURE"}),
}
# Objective-match categories that say "real page, wrong target": the useful
# response is to keep navigating, not to call the page ambiguous.
WRONG_TARGET_MATCHES = frozenset(
    {"SAME_COMPONENT_WRONG_PROCEDURE", "DIFFERENT_COMPONENT", "DIFFERENT_SENSOR_FAMILY"}
)

# Property order is part of the contract: llama.cpp's grammar emits object
# properties in declared order and cannot revisit one, so every judgement the
# decision must be consistent with is declared before the decision.
REVIEW_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": REVIEW_TOOL_NAME,
        "description": (
            "Report the semantic review of one candidate service-information page "
            "against the original research objective. Fill the evidence table and the "
            "objective match from the page itself before deciding."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "classification": {"type": "string", "enum": list(CLASSIFICATIONS)},
                "procedure_type": {"type": "string", "enum": list(PROCEDURE_TYPES)},
                "vehicle_match": {"type": "string", "enum": list(VEHICLE_MATCH)},
                "evidence": {
                    "type": "object",
                    "properties": {
                        field: {"type": "string", "enum": list(EVIDENCE_STATUSES)}
                        for field in EVIDENCE_FIELDS
                    },
                    "required": list(EVIDENCE_FIELDS),
                    "additionalProperties": False,
                },
                "objective_match": {
                    "type": "string",
                    "enum": list(OBJECTIVE_MATCH),
                    "description": (
                        "What the page actually performs compared with the requested "
                        "system and operation."
                    ),
                },
                "dependencies": {
                    "type": "array",
                    "maxItems": REVIEW_MAX_DEPENDENCIES,
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "minLength": 1, "maxLength": 160},
                            "reason": {"type": "string", "minLength": 1, "maxLength": 300},
                            "quote": {
                                "type": "string",
                                "minLength": 8,
                                "maxLength": 240,
                                "description": "The exact sentence on this page that requires that document.",
                            },
                        },
                        "required": ["title", "reason", "quote"],
                        "additionalProperties": False,
                    },
                },
                "decision": {"type": "string", "enum": list(DECISIONS)},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "evidence_summary": {"type": "string", "minLength": 1, "maxLength": 1200},
            },
            "required": [
                "classification",
                "procedure_type",
                "vehicle_match",
                "evidence",
                "objective_match",
                "dependencies",
                "decision",
                "confidence",
                "evidence_summary",
            ],
            "additionalProperties": False,
        },
    },
}

REVIEW_SYSTEM_PROMPT = (
    "You are an independent reviewer for a collision repair shop's ADAS service-"
    "information research. You have not seen how this page was found and you do not "
    "trust the person who found it. Do not assume this is the requested calibration "
    "procedure. Decide from the evidence alone.\n\n"
    "WHAT THE OBJECTIVE MEANS. The requirement name comes from the shop's own job "
    "system, not from the vehicle manufacturer, and the manufacturer almost never uses "
    "the same words. Work out which vehicle system the requirement refers to and what "
    "a technician must do for it after a repair or replacement, then ask whether this "
    "page performs that.\n\n"
    "JUDGE THE PAGE BY WHAT IT DOES, NOT WHAT IT IS CALLED. A page that carries the "
    "setup, tools, targets or reflectors, distances or angles, scan-tool operations, "
    "execution steps, and completion criteria for that work is an actual procedure "
    "whatever its title says. Title words such as Operation Check, Inspection, "
    "Confirmation, Verification, Beam Axis Inspection, Adjustment, Initialization, or "
    "Learn are not evidence in either direction: an 'Operation Check' that performs the "
    "beam-axis confirmation or adjustment is the procedure, and one that only tells the "
    "technician whether some other procedure is needed is supporting material. "
    "Removal/replacement, wiring, diagnostics, component descriptions, and system "
    "overviews are not the procedure merely because they mention the component.\n\n"
    "EVIDENCE TABLE. Fill it honestly from the page text: PRESENT only when the page "
    "itself contains it; REFERENCED_ELSEWHERE when the page points to another document "
    "for it; NOT_APPLICABLE when that kind of content does not belong to this procedure; "
    "otherwise MISSING_OR_UNCERTAIN.\n\n"
    "OBJECTIVE MATCH. Compare what the page actually performs with the requested system "
    "and operation, and set procedure_type to what the page itself calibrates, not to "
    "what the objective asked for. EXACT_MATCH: it performs the requested work. "
    "SAME_COMPONENT_WRONG_PROCEDURE: right component, different operation or article. "
    "DIFFERENT_COMPONENT: another component in the same broad area. "
    "DIFFERENT_SENSOR_FAMILY: a different kind of sensor -- a camera procedure never "
    "satisfies a radar requirement and a radar procedure never satisfies a camera one. "
    "UNCERTAIN: the evidence does not let you tell.\n\n"
    "WORKFLOW ROLE. When the evidence packet has no dependency_context you are reviewing "
    "the PRIMARY objective, which only the actual procedure can satisfy; a required "
    "supporting procedure is useful but cannot close it, so decide CONTINUE_SEARCH for "
    "one. When dependency_context is present, the task exists to retrieve that required "
    "supporting document, and it may be accepted as REQUIRED_SUPPORTING_PROCEDURE.\n\n"
    "DEPENDENCIES. Name one only when this page's own text unconditionally directs the "
    "technician to another named document on the normal path to completing the work "
    "(for example 'perform the wheel alignment first', 'set up the target as described "
    "in ...'), and quote that exact sentence; a dependency whose quote is not on the page "
    "is discarded. A related-information link, a parts page, a document that merely "
    "'may' apply, or a conditional repair branch -- a DTC check, diagnosis, removal, "
    "installation, or replacement that applies only if a fault is found -- is not a "
    "dependency. Leave dependencies empty when the page says nothing of the kind.\n\n"
    "DECISION. ACCEPT when this page alone satisfies the objective; "
    "ACCEPT_WITH_DEPENDENCIES when it is the procedure but needs the named documents "
    "too; FOLLOW_DEPENDENCY when this page is not the procedure but names the document "
    "that is; CONTINUE_SEARCH when it is related but not the procedure, or the wrong "
    "system or operation; REJECT when it is the wrong kind of document or the wrong "
    "vehicle; UNCERTAIN when the evidence does not let you tell. Report through the "
    "review tool only."
)


class SemanticReviewError(ValueError):
    """The reviewer's reply could not be read as a structured verdict."""


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def review_role(objective: Any) -> str:
    """Which workflow role a review answers for, from Core's own task record."""
    if isinstance(objective, dict) and _clean(objective.get("dependency_context"), 400):
        return "dependency"
    return "primary"


def build_review_messages(
    *,
    objective: dict[str, Any],
    vehicle: dict[str, Any],
    candidate: dict[str, Any],
    provider: str,
    screenshot: Optional[tuple[bytes, str]] = None,
) -> list[dict[str, Any]]:
    """The reviewer's entire context: system stance plus the evidence packet.

    Deliberately built from scratch. Nothing from the Navigator loop's
    transcript -- its reasoning, its receipts, its earlier observations --
    is ever placed here.
    """
    import base64

    vehicle_label = " ".join(
        str(vehicle.get(key)).strip()
        for key in ("year", "make", "model", "trim")
        if vehicle.get(key) not in (None, "")
    )
    text = str(candidate.get("text") or "")
    truncated = len(text) > REVIEW_TEXT_CHARS or bool(candidate.get("text_truncated"))
    links = [
        _clean(item, 120)
        for item in (candidate.get("referenced_links") or [])
        if _clean(item, 120)
    ][:REVIEW_MAX_LINKS]
    packet: dict[str, Any] = {
        "research_objective": _clean(objective.get("objective"), 600),
        "requirement_label": _clean(objective.get("requirement_label"), 200) or None,
        "system": _clean(objective.get("system"), 200) or None,
        "component": _clean(objective.get("component"), 200) or None,
        "dependency_context": _clean(objective.get("dependency_context"), 400) or None,
        "exact_vehicle": vehicle_label or None,
        "vin": _clean(vehicle.get("vin"), 32) or None,
        "provider": provider,
        "candidate_title": _clean(candidate.get("title"), 300),
        "candidate_breadcrumb": [
            _clean(item, 120) for item in (candidate.get("breadcrumb") or []) if _clean(item, 120)
        ][:12],
        "source_url": _clean(candidate.get("url"), 1000),
        "page_text_truncated_for_review": truncated,
        "page_text_chars": len(text),
        "documents_referenced_on_page": links,
    }
    packet = {key: value for key, value in packet.items() if value not in (None, "", [])}
    body = (
        "Review this candidate page against the research objective.\n\n"
        f"Evidence packet: {json.dumps(packet, default=str)}\n\n"
        "Extracted page text:\n"
        f"{text[:REVIEW_TEXT_CHARS]}"
    )
    if truncated:
        body += "\n\n[page text cut for review; judge what is shown and mark unseen items MISSING_OR_UNCERTAIN]"
    content: Any = body
    if screenshot is not None:
        raw, mime = screenshot
        encoded = base64.b64encode(raw).decode("ascii")
        content = [
            {
                "type": "text",
                "text": body + "\n\nThe attached image is the candidate page as rendered.",
            },
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
        ]
    return [
        {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def validate_review(
    payload: Any,
    *,
    page_text: Optional[str] = None,
    role: str = "primary",
) -> dict[str, Any]:
    """Return a normalized verdict, or raise SemanticReviewError.

    Structural only. Enumerations, ranges, required fields, and the
    consistency between the decision and the reviewer's own judgements are
    checked; the meaning of the page is not re-judged here. A dependency must
    cite the sentence on the page that requires it; when ``page_text`` is
    given, a citation that is not on the page discards that dependency -- a
    check that the reviewer's claim is grounded, not a judgement of it.

    ``role`` is the workflow role Core assigned the task ("primary" or
    "dependency"). It decides only which classifications may close the task.
    """
    if not isinstance(payload, dict):
        raise SemanticReviewError("review is not an object")
    classification = str(payload.get("classification") or "").strip()
    if classification not in CLASSIFICATIONS:
        raise SemanticReviewError("classification is missing or unknown")
    procedure_type = str(payload.get("procedure_type") or "").strip()
    if procedure_type not in PROCEDURE_TYPES:
        raise SemanticReviewError("procedure_type is missing or unknown")
    vehicle_match = str(payload.get("vehicle_match") or "NOT_STATED").strip()
    if vehicle_match not in VEHICLE_MATCH:
        raise SemanticReviewError("vehicle_match is unknown")
    raw_evidence = payload.get("evidence")
    if not isinstance(raw_evidence, dict):
        raise SemanticReviewError("evidence table is missing")
    evidence: dict[str, str] = {}
    for field in EVIDENCE_FIELDS:
        status = str(raw_evidence.get(field) or "").strip()
        if status not in EVIDENCE_STATUSES:
            raise SemanticReviewError(f"evidence.{field} is missing or unknown")
        evidence[field] = status
    raw_match = payload.get("objective_match")
    objective_match = str(raw_match).strip() if raw_match not in (None, "") else ""
    if objective_match and objective_match not in OBJECTIVE_MATCH:
        raise SemanticReviewError("objective_match is unknown")
    decision = str(payload.get("decision") or "").strip()
    if decision not in DECISIONS:
        raise SemanticReviewError("decision is missing or unknown")
    confidence = payload.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise SemanticReviewError("confidence is missing")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise SemanticReviewError("confidence is out of range")
    raw_dependencies = payload.get("dependencies")
    if raw_dependencies is None:
        raw_dependencies = []
    if not isinstance(raw_dependencies, list):
        raise SemanticReviewError("dependencies must be a list")
    dependencies: list[dict[str, str]] = []
    unsupported: list[dict[str, str]] = []
    folded_page = _normalized_text(page_text) if page_text else None
    for item in raw_dependencies[:REVIEW_MAX_DEPENDENCIES]:
        if not isinstance(item, dict):
            raise SemanticReviewError("a dependency is not an object")
        title = _clean(item.get("title"), 160)
        reason = _clean(item.get("reason"), 300)
        quote = _clean(item.get("quote"), 240)
        if not title or not reason:
            raise SemanticReviewError("a dependency lacks a title or reason")
        entry = {"title": title, "reason": reason, "quote": quote}
        if folded_page is not None:
            cited = _normalized_text(quote)
            if len(cited) < 8 or cited not in folded_page:
                unsupported.append({**entry, "dropped": "the quoted sentence is not on the page"})
                continue
        dependencies.append(entry)
    summary = _clean(payload.get("evidence_summary"), 1200)
    if not summary:
        raise SemanticReviewError("evidence_summary is missing")

    role = role if role in ACCEPTABLE_CLASSIFICATIONS_BY_ROLE else "primary"
    verdict: dict[str, Any] = {
        "classification": classification,
        "procedure_type": procedure_type,
        "vehicle_match": vehicle_match,
        "evidence": evidence,
        "objective_match": objective_match or None,
        "dependencies": dependencies,
        "decision": decision,
        "confidence": round(confidence, 3),
        "evidence_summary": summary,
        "review_role": role,
        "malformed": False,
    }

    if unsupported:
        verdict["unsupported_dependencies"] = unsupported
        if decision == "ACCEPT_WITH_DEPENDENCIES" and not dependencies:
            # The page was accepted on its own evidence; only the ungrounded
            # dependency claims fall away.
            verdict["original_decision"] = decision
            verdict["decision"] = "ACCEPT"
            decision = "ACCEPT"

    # Consistency between the decision and the reviewer's own judgements. A
    # decision they contradict is downgraded, never promoted, and the
    # reviewer's words are kept so the downgrade stays inspectable.
    #
    # Two kinds of contradiction lead to different next steps. "Real page,
    # wrong target" -- a supporting page on a primary task, or a page the
    # reviewer itself says performs a different system or operation -- means
    # keep navigating (CONTINUE_SEARCH). A gap in the evidence itself -- no
    # execution steps, a different vehicle, an unacceptable document kind, an
    # objective match that is uncertain or missing -- means the page cannot
    # be called either way (UNCERTAIN).
    wrong_target: list[str] = []
    ungrounded: list[str] = []
    if decision in ACCEPTING_DECISIONS:
        allowed = ACCEPTABLE_CLASSIFICATIONS_BY_ROLE[role]
        if classification not in allowed:
            if classification == "REQUIRED_SUPPORTING_PROCEDURE" and role == "primary":
                wrong_target.append(
                    "a REQUIRED_SUPPORTING_PROCEDURE cannot satisfy the primary objective"
                )
            else:
                ungrounded.append(f"decision {decision} with classification {classification}")
        if vehicle_match == "DIFFERENT_VEHICLE":
            ungrounded.append("decision accepts a page the review says is for a different vehicle")
        if evidence["execution_steps"] != "PRESENT":
            ungrounded.append(
                f"decision accepts a page whose execution_steps are {evidence['execution_steps']}"
            )
        # The objective match answers "does this perform the requested work".
        # A dependency task was created to fetch a different, named document,
        # so comparing it with the primary objective would be the wrong test.
        if role == "primary":
            if objective_match in WRONG_TARGET_MATCHES:
                wrong_target.append(f"objective_match={objective_match}")
            elif objective_match != "EXACT_MATCH":
                ungrounded.append(
                    f"decision accepts a page whose objective_match is {objective_match or 'not stated'}"
                )
    if decision in DEPENDENCY_DECISIONS and not dependencies:
        ungrounded.append(f"decision {decision} names no dependency")

    if wrong_target:
        verdict["original_decision"] = verdict.get("original_decision", decision)
        verdict["decision"] = "CONTINUE_SEARCH"
        verdict["inconsistent"] = wrong_target + ungrounded
    elif ungrounded:
        verdict["original_decision"] = verdict.get("original_decision", decision)
        verdict["decision"] = "UNCERTAIN"
        verdict["inconsistent"] = ungrounded
    return verdict


def malformed_review(error: str) -> dict[str, Any]:
    """The verdict a reply that could not be read becomes: never an acceptance."""
    return {
        "classification": "UNCERTAIN",
        "procedure_type": "NOT_A_PROCEDURE",
        "vehicle_match": "NOT_STATED",
        "evidence": {field: "MISSING_OR_UNCERTAIN" for field in EVIDENCE_FIELDS},
        "objective_match": None,
        "dependencies": [],
        "decision": "UNCERTAIN",
        "confidence": 0.0,
        "evidence_summary": "The reviewer did not return a readable verdict.",
        "malformed": True,
        "error": _clean(error, 400),
    }


def accepted(review: Any) -> bool:
    return (
        isinstance(review, dict)
        and review.get("malformed") is not True
        and review.get("decision") in ACCEPTING_DECISIONS
    )


async def review_candidate(
    *,
    client: Any,
    objective: dict[str, Any],
    vehicle: dict[str, Any],
    candidate: dict[str, Any],
    provider: str,
    screenshot: Optional[tuple[bytes, str]] = None,
) -> dict[str, Any]:
    """Run one isolated review and return its validated verdict.

    Every failure mode -- a model error, prose instead of a tool call, an
    unreadable or inconsistent verdict -- ends as UNCERTAIN with the reason
    recorded. It never ends as an acceptance.
    """
    role = review_role(objective)
    messages = build_review_messages(
        objective=objective,
        vehicle=vehicle,
        candidate=candidate,
        provider=provider,
        screenshot=screenshot,
    )
    try:
        events = [
            event
            async for event in client.stream(
                messages,
                tools=[REVIEW_TOOL_SCHEMA],
                max_tokens=REVIEW_MAX_TOKENS,
                tool_choice="required",
            )
        ]
    except Exception as exc:  # noqa: BLE001 - a failed review is an UNCERTAIN verdict
        log.warning("semantic review model call failed", exc_info=True)
        return malformed_review(f"model call failed: {type(exc).__name__}: {exc}")
    calls = [event for event in events if event.get("type") == "tool_call"]
    usage = next((event for event in events if event.get("type") == "usage"), None)
    if not calls:
        prose = "".join(str(event.get("text") or "") for event in events if event.get("type") == "content")
        verdict = malformed_review(
            "the reviewer answered in prose instead of the review tool: " + prose[:200]
        )
    else:
        call = calls[0]
        try:
            payload = json.loads(call.get("arguments") or "{}")
        except (TypeError, ValueError) as exc:
            payload = None
            parse_error = f"unparseable review arguments: {exc}"
        else:
            parse_error = ""
        if call.get("name") not in (None, "", REVIEW_TOOL_NAME):
            verdict = malformed_review(f"the reviewer called {call.get('name')!r} instead of the review tool")
        elif payload is None:
            verdict = malformed_review(parse_error)
        else:
            try:
                verdict = validate_review(
                    payload, page_text=str(candidate.get("text") or ""), role=role
                )
            except SemanticReviewError as exc:
                verdict = malformed_review(str(exc))
    if isinstance(usage, dict):
        usage_block = usage.get("usage") if isinstance(usage.get("usage"), dict) else {}
        verdict["review_prompt_tokens"] = usage_block.get("prompt_tokens")
    verdict["reviewed_title"] = _clean(candidate.get("title"), 300)
    verdict["reviewed_url"] = _clean(candidate.get("url"), 1000)
    return verdict
