"""Trusted promotion of accepted ADAS SI research into the semantic cache.

ADAS SI is the durable automotive source memory. ``knowledge.sqlite`` is only
a verified semantic cache of interpretations already established from that
library; it is never an independent source of truth.

Both research surfaces use this module:

* chat ``delegate_research`` promotes SATISFIED fact/requirement answers;
* Calibration IQ ``research_si`` promotes SATISFIED procedure matches.

The deliverables have different evidence shapes, but share the same source
boundary: only an ADAS SI document, exact application identity, known page,
source text, and a hash-verified local file can become ``verified`` cache data.
Model-directed capture still cannot self-verify anything.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from . import research_evidence_contract as contract

log = logging.getLogger("xomni.research_knowledge_promotion")

PROMOTION_ACTOR = "x-research-review"

_REQUIREMENT_TYPE_FOR_STAGE = {
    "calibration": "calibration",
    "inspection": "inspection",
    "prerequisite": "prerequisite",
    "setup": "procedure",
    "verification": "procedure",
    "whole_procedure": "procedure",
    "not_stated": "informational",
}

_REPOSITORY_LOCK = threading.Lock()
_REPOSITORIES: dict[tuple[str, tuple[str, ...]], Any] = {}


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _source_text(candidate: Any) -> str:
    if not isinstance(candidate, dict):
        return ""
    return str(candidate.get("text") or "").strip()


def _procedure_excerpt(candidate: Any, limit: int = 1_200) -> str:
    """Exact source text persisted for a reviewed procedure cache entry.

    Procedure acceptance is a whole-page semantic judgement rather than one
    answer-row quote. The cache therefore keeps a bounded exact excerpt from
    the reviewed source while the review metadata records why that page was
    accepted. Source hash verification remains the final trust gate.
    """

    text = _source_text(candidate)
    return text[:limit].strip() if text else ""


def _common_refusal(
    *,
    vehicle: Any,
    finding: Any,
    evaluation: Any,
    candidate: Any,
) -> Optional[str]:
    if not isinstance(evaluation, dict) or evaluation.get("outcome") != contract.SATISFIED:
        return "the research outcome is not SATISFIED"
    review = evaluation.get("review")
    if not isinstance(review, dict) or review.get("malformed") is not False:
        return "no well-formed independent review"
    if evaluation.get("library_conflict"):
        return "the library files this source for another vehicle"
    if not isinstance(finding, dict) or finding.get("source") != "adas_si":
        return "only the authoritative ADAS SI library is approved for semantic-cache promotion"
    if not finding.get("relative_path"):
        return "the library document is not identified"
    try:
        page = int(finding.get("page"))
    except (TypeError, ValueError):
        return "the evidence page is not known"
    if page < 1:
        return "the evidence page is not known"
    if not isinstance(vehicle, dict) or not all(vehicle.get(key) for key in ("year", "make", "model")):
        return "the vehicle application is not grounded (year, make, and model are required)"
    if contract.library_identity_conflict(vehicle, finding.get("library_vehicle")):
        return "the library files this source for another vehicle"
    if not _source_text(candidate):
        return "the reviewed ADAS SI source text is empty"
    return None


def promotion_refusal(
    *,
    vehicle: Any,
    finding: Any,
    evaluation: Any,
    candidate: Any,
) -> Optional[str]:
    """Why accepted ADAS SI evidence may not enter the verified semantic cache."""

    common = _common_refusal(
        vehicle=vehicle,
        finding=finding,
        evaluation=evaluation,
        candidate=candidate,
    )
    if common:
        return common

    review = evaluation["review"]
    deliverable = evaluation.get("deliverable")
    if deliverable == "answer":
        if review.get("answers_objective") != "FULLY" or review.get("system_match") != "SAME_SYSTEM":
            return "the review did not find a full answer for the same system"
        if review.get("vehicle_applicability") != "SOURCE_INCLUDES_THIS_VEHICLE":
            return "the source does not itself include this vehicle"
        anchor = evaluation.get("anchor_quote") or review.get("anchor_quote")
        if not contract.anchor_in_text(anchor, _source_text(candidate)):
            return "the evidence anchor is not in the retrieved source text"
        return None

    if deliverable == "procedure":
        # ``research_semantic_review.validate_review`` already downgrades an
        # internally inconsistent acceptance. Re-check the decisive fields at
        # the persistence boundary so a malformed/faked evaluation cannot be
        # promoted merely because it says SATISFIED.
        if review.get("decision") != "ACCEPT":
            return "only a complete accepted procedure is promoted"
        if review.get("classification") != "ACTUAL_PROCEDURE":
            return "the review did not classify the source as the actual procedure"
        if review.get("objective_match") != "EXACT_MATCH":
            return "the procedure does not exactly match the requested work"
        if review.get("same_unit") != "SAME":
            return "the procedure was not proved to operate on the same unit"
        evidence = review.get("evidence") if isinstance(review.get("evidence"), dict) else {}
        if evidence.get("execution_steps") != "PRESENT":
            return "the reviewed source does not contain the procedure execution steps"
        if review.get("vehicle_match") == "DIFFERENT_VEHICLE":
            return "the reviewer says the procedure is for a different vehicle"
        if not _procedure_excerpt(candidate):
            return "the reviewed procedure has no source excerpt to persist"
        return None

    return f"unsupported research deliverable {deliverable!r}"


def build_record(
    *,
    objective: str,
    vehicle: dict[str, Any],
    system: Optional[str],
    component: Optional[str],
    finding: dict[str, Any],
    evaluation: dict[str, Any],
    local_path: str,
    content_sha256: str,
    candidate: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build one source-backed semantic-cache record from a trusted evaluation."""

    review = evaluation["review"]
    deliverable = str(evaluation.get("deliverable") or "answer")
    stage = str(evaluation.get("stage") or review.get("stage") or "not_stated")
    application: dict[str, Any] = {
        "manufacturer": vehicle["make"],
        "model": vehicle["model"],
        "year": int(vehicle["year"]),
    }
    if vehicle.get("trim"):
        application["trim"] = vehicle["trim"]
    if vehicle.get("vin"):
        application["vin_pattern"] = vehicle["vin"]

    if deliverable == "procedure":
        requirement_text = _clean(objective, 4_000)
        procedure_summary = _clean(review.get("evidence_summary"), 4_000)
        applicability_notes = None
        calibration_type = _clean(review.get("procedure_type"), 200) or None
        excerpt = _procedure_excerpt(candidate)
        review_keys = (
            "classification",
            "procedure_type",
            "vehicle_match",
            "objective_match",
            "requirement_system",
            "page_system",
            "same_unit",
            "page_structure",
            "decision",
            "confidence",
        )
    else:
        requirement_text = _clean(
            evaluation.get("source_answer") or review.get("source_answer"), 4_000
        )
        procedure_summary = None
        applicability_notes = _clean(review.get("source_covers"), 4_000) or None
        calibration_type = None
        excerpt = _clean(
            evaluation.get("anchor_quote") or review.get("anchor_quote"), 1_200
        )
        review_keys = (
            "question_asks",
            "source_covers",
            "vehicle_applicability",
            "system_match",
            "answers_objective",
            "stage",
            "confidence",
        )

    requirement: dict[str, Any] = {
        "requirement_type": _REQUIREMENT_TYPE_FOR_STAGE.get(stage, "informational"),
        "text": requirement_text,
        "applicability_notes": applicability_notes,
        "procedure_summary": procedure_summary,
        "calibration_type": calibration_type,
    }
    requirement = {key: value for key, value in requirement.items() if value not in (None, "")}

    payload: dict[str, Any] = {
        "application": application,
        "system": {"name": system or component or "not stated"},
        "component": {"name": component} if component else None,
        "repair_event": {"event_type": "research_objective", "description": objective},
        "requirement": requirement,
        "lifecycle": "verified",
        "confidence": review.get("confidence"),
        "evidence": [
            {
                "source": {
                    "source_type": "adas_si_document",
                    "source_name": _clean(finding.get("title"), 500) or local_path,
                    "local_path": local_path,
                    "content_sha256": content_sha256,
                    "authoritative": True,
                    "metadata": {
                        "relative_path": finding.get("relative_path"),
                        "promotion": "research_evidence_contract",
                        "deliverable": deliverable,
                        "review": {key: review.get(key) for key in review_keys},
                    },
                },
                "page_start": int(finding["page"]),
                "excerpt": excerpt,
                "extraction_status": "extracted",
                "verification_status": "verified",
                "confidence": review.get("confidence"),
            }
        ],
    }
    if deliverable == "procedure":
        payload["procedures"] = [
            {
                "title": _clean(finding.get("title"), 500) or "OEM procedure",
                "procedure_identifier": _clean(review.get("procedure_type"), 200) or None,
                "summary": procedure_summary or None,
            }
        ]
    return payload


def repository_from_settings(settings: Any) -> Any | None:
    """Return one process-local repository handle for background CIQ research.

    Chat already receives the repository created in ``core.main``. Background
    ``research_si`` is intentionally decoupled from chat wiring, so it resolves
    the same configured database path here and reuses one handle per process.
    Both handles point at the same WAL-backed cache; ADAS SI remains the source
    of truth either way.
    """

    root = getattr(settings, "root", None)
    adas_root = getattr(settings, "adas_si_root", None)
    if root is None or adas_root is None:
        return None
    configured = getattr(settings, "automotive_knowledge_db", None)
    path = Path(configured) if configured else Path(root) / "data" / "capabilities" / "automotive_knowledge" / "knowledge.sqlite"
    roots = (str(Path(adas_root)),)
    key = (str(path), roots)
    with _REPOSITORY_LOCK:
        cached = _REPOSITORIES.get(key)
        if cached is not None:
            return cached
        try:
            from .automotive_knowledge import AutomotiveKnowledgeRepository

            repository = AutomotiveKnowledgeRepository(path, authoritative_roots=roots)
        except Exception:  # noqa: BLE001 - semantic cache failure must never fail research
            log.warning("semantic cache is not available for background promotion", exc_info=True)
            return None
        _REPOSITORIES[key] = repository
        return repository


def make_settings_learner(settings: Any, adas: Any):
    """Build the same promotion hook for background ``research_si``."""

    return make_learner(repository_from_settings(settings), adas)


def make_learner(repository: Any, adas: Any):
    """Build the shared trusted promotion hook for chat and CIQ research."""

    async def learn(
        *,
        objective: str,
        vehicle: dict[str, Any],
        system: Optional[str],
        component: Optional[str],
        finding: dict[str, Any],
        evaluation: dict[str, Any],
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        refusal = promotion_refusal(
            vehicle=vehicle, finding=finding, evaluation=evaluation, candidate=candidate
        )
        if refusal:
            return {"promoted": False, "reason": refusal}
        if repository is None or adas is None:
            return {"promoted": False, "reason": "the semantic cache is not available"}
        try:
            path = adas.resolve_relative(str(finding["relative_path"]))
            digest = await asyncio.to_thread(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
        except Exception as exc:  # noqa: BLE001 - unreadable source simply is not cached
            return {"promoted": False, "reason": f"the source could not be hashed: {type(exc).__name__}"}
        payload = build_record(
            objective=objective,
            vehicle=vehicle,
            system=system,
            component=component,
            finding=finding,
            evaluation=evaluation,
            local_path=str(path),
            content_sha256=digest,
            candidate=candidate,
        )
        if payload.get("component") is None:
            payload.pop("component")
        procedures = payload.get("procedures")
        if isinstance(procedures, list):
            for procedure in procedures:
                if isinstance(procedure, dict) and procedure.get("procedure_identifier") is None:
                    procedure.pop("procedure_identifier", None)
                if isinstance(procedure, dict) and procedure.get("summary") is None:
                    procedure.pop("summary", None)
        try:
            outcome = await asyncio.to_thread(
                repository.create_record, payload, actor=PROMOTION_ACTOR
            )
        except Exception as exc:  # noqa: BLE001 - repository gates own final refusal
            log.info("semantic-cache promotion refused: %s", exc)
            return {"promoted": False, "reason": _clean(str(exc), 200)}
        record = outcome.get("record") or {}
        promoted = record.get("lifecycle") == "verified"
        return {
            "promoted": promoted,
            "record_id": record.get("id"),
            "created": outcome.get("created"),
            "reason": None if promoted else "the repository did not accept the record as verified",
        }

    return learn


__all__ = [
    "PROMOTION_ACTOR",
    "build_record",
    "make_learner",
    "make_settings_learner",
    "promotion_refusal",
    "repository_from_settings",
]
