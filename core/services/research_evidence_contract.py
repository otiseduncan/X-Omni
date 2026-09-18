"""The one operational outcome of technical research, and the evaluator behind it.

Retrieval is not an answer. A search that returns a document proves only that
the document exists; whether it answers Otis's objective for *this* vehicle
and *this* system is a semantic judgement, and every technical research path
-- ordinary chat through ``delegate_research`` and Calibration IQ research
through ``research_si`` -- makes that judgement here, with the same
evaluator, and reports the same outcome:

    SATISFIED    accepted evidence answers the objective for this vehicle and
                 system, anchored to exact source text
    PARTIAL      accepted evidence answers part of it; something the answer
                 depends on is still open (a required supporting document, a
                 stage the source does not cover, an unstated applicability)
    UNSATISFIED  nothing retrieved answers it -- including a related document
                 that is about the right part but not the requested work

Two deliverables share the evaluator:

* ``procedure`` -- Otis wants the procedure itself. The candidate goes to the
  procedure review (``research_semantic_review``), the same isolated,
  sceptical verdict Calibration IQ research has always used.
  ``ACCEPT_WITH_DEPENDENCIES`` is PARTIAL: the page itself says another
  document is required on the normal path, and that document is unresolved.
* ``answer`` -- Otis wants a requirement or fact (does the bumper stay on,
  does BSM need calibration after this repair). The candidate goes to the
  answer review below: the reviewer quotes the exact source text first, says
  which vehicles and systems the source itself covers, and only then whether
  it answers the question.

Core checks shape and self-consistency only: enumerations, that the anchor
quote is really in the retrieved text, that the reviewer's own judgements
agree with its verdict, and that the library's filing of a document (a
structured Year/Make/Model identity, not prose) does not contradict an
applicability claim. It never matches automotive words. Every failure --
no model, a model error, prose instead of the review tool, a malformed or
inconsistent verdict -- ends UNSATISFIED, never as an acceptance.
"""

from __future__ import annotations

import contextvars
import inspect
import json
import logging
from typing import Any, Optional

log = logging.getLogger("xomni.research_evidence_contract")

SATISFIED = "SATISFIED"
PARTIAL = "PARTIAL"
UNSATISFIED = "UNSATISFIED"
OUTCOMES: tuple[str, ...] = (SATISFIED, PARTIAL, UNSATISFIED)
_RANK = {UNSATISFIED: 0, PARTIAL: 1, SATISFIED: 2}

DELIVERABLES: tuple[str, ...] = ("answer", "procedure")

ANSWER_REVIEW_TOOL_NAME = "report_evidence_answer_review"
ANSWER_TEXT_CHARS = 12_000
ANSWER_MAX_TOKENS = 900
ANSWER_MAX_UNRESOLVED = 4
ANCHOR_MIN_CHARS = 8

APPLICABILITY: tuple[str, ...] = (
    "SOURCE_INCLUDES_THIS_VEHICLE",
    "SOURCE_COVERS_OTHER_VEHICLES_OR_YEARS",
    "SOURCE_DOES_NOT_SAY",
)
SYSTEM_MATCH: tuple[str, ...] = ("SAME_SYSTEM", "DIFFERENT_SYSTEM", "UNCERTAIN")
ANSWERS: tuple[str, ...] = ("FULLY", "PARTLY", "RELATED_ONLY", "NO")
STAGES: tuple[str, ...] = (
    "prerequisite",
    "inspection",
    "setup",
    "calibration",
    "verification",
    "whole_procedure",
    "not_stated",
)

# Declared order is generation order (llama.cpp grammar): the reviewer names
# the question, reads what the source covers, and copies the exact text before
# it may say whether that text answers anything.
ANSWER_REVIEW_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": ANSWER_REVIEW_TOOL_NAME,
        "description": (
            "Report whether one retrieved source answers the research objective for the "
            "exact vehicle and system. Quote the source before judging it."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "question_asks": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 300,
                    "description": "What the objective asks, in one sentence: which vehicle, which system, what about it.",
                },
                "source_covers": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                    "description": (
                        "Which vehicles, model years, and systems this source itself says it "
                        "covers -- from its title, header, table rows, or library filing."
                    ),
                },
                "vehicle_applicability": {"type": "string", "enum": list(APPLICABILITY)},
                "system_match": {
                    "type": "string",
                    "enum": list(SYSTEM_MATCH),
                    "description": "Is the system the source speaks about the system the objective asks about?",
                },
                "anchor_quote": {
                    "type": "string",
                    "maxLength": 400,
                    "description": (
                        "The exact source text that answers the objective, copied verbatim "
                        "(one line, row, or sentence). Empty when the source does not answer it."
                    ),
                },
                "source_answer": {
                    "type": "string",
                    "maxLength": 400,
                    "description": "What that quoted text says in answer, in plain words. Empty when none.",
                },
                "stage": {
                    "type": "string",
                    "enum": list(STAGES),
                    "description": "The procedure stage the source ties its answer to; not_stated when it names none.",
                },
                "answers_objective": {"type": "string", "enum": list(ANSWERS)},
                "unresolved": {
                    "type": "array",
                    "maxItems": ANSWER_MAX_UNRESOLVED,
                    "items": {"type": "string", "minLength": 1, "maxLength": 200},
                    "description": "What the objective still needs that this source does not give. Empty when nothing.",
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": [
                "question_asks",
                "source_covers",
                "vehicle_applicability",
                "system_match",
                "anchor_quote",
                "source_answer",
                "stage",
                "answers_objective",
                "unresolved",
                "confidence",
            ],
        },
    },
}

ANSWER_REVIEW_SYSTEM_PROMPT = (
    "You are an independent reviewer for a collision repair shop's ADAS research. You "
    "did not find this source and you do not trust whoever did. Decide from the source "
    "alone whether it answers the research objective for the exact vehicle and system.\n\n"
    "APPLICABILITY. A source establishes something for a vehicle only when the source "
    "itself covers that vehicle: its title, header, table rows, or the library filing "
    "given in the packet name that make, model, and model year (or a range that includes "
    "it). A document for a different model year, a later generation, or another model "
    "does not establish anything for this vehicle unless the source explicitly lists it: "
    "SOURCE_COVERS_OTHER_VEHICLES_OR_YEARS. If the source never says, SOURCE_DOES_NOT_SAY.\n\n"
    "SYSTEM. Front radar, forward camera, blind spot monitoring (BSM, rear side radar), "
    "surround view, rear camera, and parking sensors are different systems. A source "
    "about one does not answer a question about another: DIFFERENT_SYSTEM.\n\n"
    "ANSWER. Copy the exact source text that answers the objective into anchor_quote -- "
    "verbatim, including a table row with its header when the answer is a row -- then "
    "say what it means. FULLY when that text answers the whole objective; PARTLY when it "
    "answers part and something is still open (list it in unresolved); RELATED_ONLY when "
    "the source is about the same part or area but does not answer what was asked -- a "
    "bumper removal page does not answer a calibration question, and a list saying a "
    "calibration is needed is not the calibration procedure; NO when it does not "
    "address the objective. Tie the answer to the stage the source gives it "
    "(prerequisite, inspection, setup, calibration, verification) and never invent one. "
    "Report through the review tool only."
)


class EvidenceReviewError(ValueError):
    """A reviewer reply that cannot be read as a structured verdict."""


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _folded(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


# The model that evaluates evidence inside one tool call. The orchestrator
# binds its own client for the duration of a tool invocation; background jobs
# pass theirs explicitly.
_MODEL_CLIENT: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "xomni_research_evaluator_client", default=None
)


def bind_model_client(client: Any) -> contextvars.Token:
    return _MODEL_CLIENT.set(client)


def reset_model_client(token: contextvars.Token) -> None:
    _MODEL_CLIENT.reset(token)


def current_model_client() -> Any | None:
    return _MODEL_CLIENT.get()


def normalize_deliverable(value: Any) -> str:
    text = _clean(value, 40).casefold()
    return text if text in DELIVERABLES else "answer"


def combine(outcomes: Any) -> str:
    """The research outcome of several evaluated candidates: the best one."""

    best = UNSATISFIED
    for outcome in outcomes or ():
        if outcome in _RANK and _RANK[outcome] > _RANK[best]:
            best = outcome
    return best


def anchor_in_text(anchor: Any, text: Any) -> bool:
    """Whether the quoted anchor is really in the retrieved text (whitespace/case folded)."""

    cited = _folded(anchor)
    return len(cited) >= ANCHOR_MIN_CHARS and cited in _folded(text)


def _year(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def library_identity_conflict(vehicle: Any, library_vehicle: Any) -> Optional[str]:
    """Structured Year/Make/Model disagreement between the request and a source's filing.

    ``library_vehicle`` is the application the ADAS SI library (or durable
    knowledge) records for the source: ``year`` or ``year_start``/``year_end``,
    ``make``/``manufacturer``, ``model``. Only fields both sides state are
    compared; nothing is inferred from prose.
    """

    if not isinstance(vehicle, dict) or not isinstance(library_vehicle, dict):
        return None
    reasons = []
    want_make = _folded(vehicle.get("make"))
    have_make = _folded(library_vehicle.get("make") or library_vehicle.get("manufacturer"))
    if want_make and have_make and want_make != have_make:
        reasons.append(f"filed for make {library_vehicle.get('make') or library_vehicle.get('manufacturer')}")
    want_model = _folded(vehicle.get("model"))
    have_model = _folded(library_vehicle.get("model"))
    if want_model and have_model and want_model != have_model:
        reasons.append(f"filed for model {library_vehicle.get('model')}")
    want_year = _year(vehicle.get("year"))
    start = _year(library_vehicle.get("year_start") or library_vehicle.get("year"))
    end = _year(library_vehicle.get("year_end") or library_vehicle.get("year"))
    if want_year is not None and start is not None and end is not None and not start <= want_year <= end:
        span = str(start) if start == end else f"{start}-{end}"
        reasons.append(f"filed for model year {span}")
    return "; ".join(reasons) or None


# --- procedure deliverable -------------------------------------------------


def outcome_from_procedure_review(verdict: Any) -> tuple[str, list[str]]:
    """Map a validated procedure verdict to the research outcome."""

    if not isinstance(verdict, dict) or verdict.get("malformed") is True:
        return UNSATISFIED, ["the review did not return a readable verdict"]
    decision = verdict.get("decision")
    if decision == "ACCEPT":
        return SATISFIED, []
    if decision == "ACCEPT_WITH_DEPENDENCIES":
        titles = [
            _clean(item.get("title"), 160)
            for item in verdict.get("dependencies") or []
            if isinstance(item, dict) and _clean(item.get("title"), 160)
        ]
        return PARTIAL, [f"requires {title}, which is not resolved" for title in titles] or [
            "requires a supporting document that is not resolved"
        ]
    reasons = [f"decision {decision or 'missing'}"]
    if verdict.get("objective_match"):
        reasons.append(f"objective_match {verdict['objective_match']}")
    if verdict.get("classification"):
        reasons.append(f"classification {verdict['classification']}")
    return UNSATISFIED, reasons


# --- answer deliverable ----------------------------------------------------


def _arguments(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value or "{}")
        except (TypeError, ValueError) as exc:
            raise EvidenceReviewError(f"unparseable review arguments: {exc}") from exc
    return value


def validate_answer_review(payload: Any, *, source_text: str) -> dict[str, Any]:
    """Return a normalized answer verdict with its outcome, or raise.

    Shape and self-consistency only. The anchor must be in ``source_text``;
    an anchor that is not there is an ungrounded answer.
    """

    payload = _arguments(payload)
    if not isinstance(payload, dict):
        raise EvidenceReviewError("review is not an object")
    fields = {}
    for name, allowed in (
        ("vehicle_applicability", APPLICABILITY),
        ("system_match", SYSTEM_MATCH),
        ("answers_objective", ANSWERS),
    ):
        value = str(payload.get(name) or "").strip()
        if value not in allowed:
            raise EvidenceReviewError(f"{name} is missing or unknown")
        fields[name] = value
    stage = str(payload.get("stage") or "not_stated").strip()
    if stage not in STAGES:
        raise EvidenceReviewError("stage is unknown")
    confidence = payload.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise EvidenceReviewError("confidence is missing")
    if not 0.0 <= float(confidence) <= 1.0:
        raise EvidenceReviewError("confidence is out of range")
    raw_unresolved = payload.get("unresolved") or []
    if not isinstance(raw_unresolved, list):
        raise EvidenceReviewError("unresolved must be a list")
    unresolved = [
        _clean(item, 200) for item in raw_unresolved[:ANSWER_MAX_UNRESOLVED] if _clean(item, 200)
    ]
    anchor = _clean(payload.get("anchor_quote"), 400)
    grounded = anchor_in_text(anchor, source_text)
    verdict: dict[str, Any] = {
        "question_asks": _clean(payload.get("question_asks"), 300) or None,
        "source_covers": _clean(payload.get("source_covers"), 300) or None,
        **fields,
        "anchor_quote": anchor if grounded else None,
        "anchor_grounded": grounded,
        "source_answer": _clean(payload.get("source_answer"), 400) or None,
        "stage": stage,
        "unresolved": unresolved,
        "confidence": round(float(confidence), 3),
        "malformed": False,
    }
    if anchor and not grounded:
        verdict["dropped_anchor"] = anchor
    return verdict


def outcome_from_answer_review(verdict: Any) -> tuple[str, list[str]]:
    """Structural mapping of an answer verdict to the research outcome."""

    if not isinstance(verdict, dict) or verdict.get("malformed") is True:
        return UNSATISFIED, ["the review did not return a readable verdict"]
    answers = verdict.get("answers_objective")
    reasons: list[str] = []
    if answers in {"RELATED_ONLY", "NO"}:
        reasons.append(
            "the source is related but does not answer the objective"
            if answers == "RELATED_ONLY"
            else "the source does not address the objective"
        )
    if verdict.get("system_match") == "DIFFERENT_SYSTEM":
        reasons.append("the source is about a different system")
    if verdict.get("vehicle_applicability") == "SOURCE_COVERS_OTHER_VEHICLES_OR_YEARS":
        reasons.append("the source covers other vehicles or model years, not this one")
    if not verdict.get("anchor_grounded"):
        reasons.append("no exact source text supports an answer")
    if reasons:
        return UNSATISFIED, reasons
    gaps: list[str] = []
    if answers != "FULLY":
        gaps.append("the source answers only part of the objective")
    if verdict.get("system_match") != "SAME_SYSTEM":
        gaps.append("the source does not make clear it is the same system")
    if verdict.get("vehicle_applicability") != "SOURCE_INCLUDES_THIS_VEHICLE":
        gaps.append("the source does not state that it covers this vehicle")
    gaps.extend(f"open: {item}" for item in verdict.get("unresolved") or [])
    return (PARTIAL, gaps) if gaps else (SATISFIED, [])


def _accepts(client: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(client.stream).parameters.values()
    except (TypeError, ValueError, AttributeError):
        return False
    return any(
        parameter.name == name or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def build_answer_messages(
    *,
    objective: dict[str, Any],
    vehicle: dict[str, Any],
    candidate: dict[str, Any],
    provider: str,
    screenshot: Optional[tuple[bytes, str]] = None,
) -> list[dict[str, Any]]:
    """The reviewer's whole context: stance plus one evidence packet, built fresh."""

    import base64

    label = " ".join(
        str(vehicle.get(key)).strip()
        for key in ("year", "make", "model", "trim")
        if vehicle.get(key) not in (None, "")
    )
    text = str(candidate.get("text") or "")
    packet = {
        "research_objective": _clean(objective.get("objective"), 600),
        "system": _clean(objective.get("system"), 200) or None,
        "component": _clean(objective.get("component"), 200) or None,
        "exact_vehicle": label or None,
        "provider": provider,
        "source_title": _clean(candidate.get("title"), 300) or None,
        "library_filing": candidate.get("library_vehicle") or None,
        "page": candidate.get("page"),
        "source_text_truncated_for_review": len(text) > ANSWER_TEXT_CHARS
        or bool(candidate.get("text_truncated")),
    }
    packet = {key: value for key, value in packet.items() if value not in (None, "", [], {})}
    body = (
        "Review this retrieved source against the research objective.\n\n"
        f"Evidence packet: {json.dumps(packet, ensure_ascii=False, default=str)}\n\n"
        "Source text (' | ' separates table columns; each row lines up with the header above it):\n"
        f"{text[:ANSWER_TEXT_CHARS]}"
    )
    content: Any = body
    if screenshot is not None:
        raw, mime = screenshot
        content = [
            {"type": "text", "text": body + "\n\nThe attached image is the source page as rendered."},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"},
            },
        ]
    return [
        {"role": "system", "content": ANSWER_REVIEW_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def malformed_answer_review(error: str) -> dict[str, Any]:
    return {
        "vehicle_applicability": "SOURCE_DOES_NOT_SAY",
        "system_match": "UNCERTAIN",
        "answers_objective": "NO",
        "anchor_quote": None,
        "anchor_grounded": False,
        "source_answer": None,
        "stage": "not_stated",
        "unresolved": [],
        "confidence": 0.0,
        "malformed": True,
        "error": _clean(error, 400),
    }


async def review_answer(
    *,
    client: Any,
    objective: dict[str, Any],
    vehicle: dict[str, Any],
    candidate: dict[str, Any],
    provider: str,
    screenshot: Optional[tuple[bytes, str]] = None,
) -> dict[str, Any]:
    messages = build_answer_messages(
        objective=objective,
        vehicle=vehicle,
        candidate=candidate,
        provider=provider,
        screenshot=screenshot,
    )
    options: dict[str, Any] = {"tools": [ANSWER_REVIEW_TOOL]}
    if _accepts(client, "max_tokens"):
        options["max_tokens"] = ANSWER_MAX_TOKENS
    if _accepts(client, "tool_choice"):
        options["tool_choice"] = {"type": "function", "function": {"name": ANSWER_REVIEW_TOOL_NAME}}
    if _accepts(client, "temperature"):
        # The same source must get the same verdict on every run.
        options["temperature"] = 0.0
    try:
        events = [event async for event in client.stream(messages, **options)]
    except Exception as exc:  # noqa: BLE001 - a failed review is never an acceptance
        log.warning("answer review model call failed", exc_info=True)
        return malformed_answer_review(f"model call failed: {type(exc).__name__}: {exc}")
    call = next((event for event in events if event.get("type") == "tool_call"), None)
    if call is None:
        return malformed_answer_review("the reviewer answered in prose instead of the review tool")
    if call.get("name") not in (None, "", ANSWER_REVIEW_TOOL_NAME):
        return malformed_answer_review(f"the reviewer called {call.get('name')!r}")
    try:
        return validate_answer_review(call.get("arguments"), source_text=str(candidate.get("text") or ""))
    except EvidenceReviewError as exc:
        return malformed_answer_review(str(exc))


# --- the shared evaluator ----------------------------------------------------


async def evaluate(
    *,
    client: Any,
    objective: dict[str, Any],
    vehicle: dict[str, Any],
    candidate: dict[str, Any],
    provider: str,
    deliverable: str = "answer",
    screenshot: Optional[tuple[bytes, str]] = None,
) -> dict[str, Any]:
    """Evaluate one retrieved candidate against one objective. Never raises.

    ``candidate`` carries ``title``, ``text`` (the retrieved source text the
    anchor must come from), optionally ``url``, ``page``, ``breadcrumb``,
    ``referenced_links`` and ``library_vehicle`` (the source's structured
    filing). Returns ``{"outcome", "deliverable", "review", "reasons", ...}``.
    """

    deliverable = normalize_deliverable(deliverable)
    vehicle = vehicle if isinstance(vehicle, dict) else {}
    base = {"deliverable": deliverable, "title": _clean(candidate.get("title"), 300) or None}
    if client is None:
        return {
            **base,
            "outcome": UNSATISFIED,
            "reviewed": False,
            "review": None,
            "reasons": ["no model was available to review the evidence"],
        }
    conflict = library_identity_conflict(vehicle, candidate.get("library_vehicle"))
    if deliverable == "procedure":
        from . import research_semantic_review

        review = await research_semantic_review.review_candidate(
            client=client,
            objective=objective,
            vehicle=vehicle,
            candidate=candidate,
            provider=provider,
            screenshot=screenshot,
        )
        outcome, reasons = outcome_from_procedure_review(review)
        result: dict[str, Any] = {
            **base,
            "reviewed": not (isinstance(review, dict) and review.get("malformed")),
            "review": review,
            "stage": "whole_procedure",
            "source_answer": _clean((review or {}).get("evidence_summary"), 400) or None,
            "unresolved": [
                _clean(item.get("title"), 160)
                for item in (review or {}).get("dependencies") or []
                if isinstance(item, dict) and _clean(item.get("title"), 160)
            ],
        }
    else:
        review = await review_answer(
            client=client,
            objective=objective,
            vehicle=vehicle,
            candidate=candidate,
            provider=provider,
            screenshot=screenshot,
        )
        outcome, reasons = outcome_from_answer_review(review)
        result = {
            **base,
            "reviewed": not review.get("malformed"),
            "review": review,
            "stage": review.get("stage"),
            "anchor_quote": review.get("anchor_quote"),
            "source_answer": review.get("source_answer"),
            "source_covers": review.get("source_covers"),
            "unresolved": list(review.get("unresolved") or []),
        }
    if conflict and outcome != UNSATISFIED:
        # The library files this source for another vehicle; the reviewer's
        # acceptance contradicts that structured fact.
        reasons = [f"the source is {conflict}, not this vehicle", *reasons]
        outcome = UNSATISFIED
    result["outcome"] = outcome
    result["reasons"] = reasons
    if conflict:
        result["library_conflict"] = conflict
    return result


def compact_evaluation(evaluation: Any) -> Optional[dict[str, Any]]:
    """The part of an evaluation that rides on a finding, for the model and the card."""

    if not isinstance(evaluation, dict):
        return None
    review = evaluation.get("review") if isinstance(evaluation.get("review"), dict) else {}
    compact = {
        "outcome": evaluation.get("outcome"),
        "deliverable": evaluation.get("deliverable"),
        "stage": evaluation.get("stage"),
        "source_answer": evaluation.get("source_answer"),
        "anchor_quote": evaluation.get("anchor_quote"),
        "source_covers": evaluation.get("source_covers"),
        "unresolved": evaluation.get("unresolved") or None,
        "reasons": evaluation.get("reasons") or None,
        "decision": review.get("decision"),
        "classification": review.get("classification"),
        "vehicle_applicability": review.get("vehicle_applicability"),
        "system_match": review.get("system_match"),
    }
    return {key: value for key, value in compact.items() if value not in (None, "", [], {})}


__all__ = [
    "ANSWER_REVIEW_TOOL",
    "DELIVERABLES",
    "OUTCOMES",
    "PARTIAL",
    "SATISFIED",
    "UNSATISFIED",
    "anchor_in_text",
    "bind_model_client",
    "combine",
    "compact_evaluation",
    "current_model_client",
    "evaluate",
    "library_identity_conflict",
    "normalize_deliverable",
    "outcome_from_answer_review",
    "outcome_from_procedure_review",
    "reset_model_client",
    "review_answer",
    "validate_answer_review",
]
