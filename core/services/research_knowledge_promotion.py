"""Trusted promotion of a SATISFIED research answer into durable knowledge.

The model-facing knowledge facade can never label a claim verified
(``AutomotiveKnowledgeService.store`` stores all model-directed evidence
unverified). This is the other side of that boundary: Core, not the model,
promotes an answer the shared evaluator accepted, and only when every gate
holds:

1. the research outcome is SATISFIED for an ``answer`` deliverable, from a
   well-formed independent structured review that says the source answers the
   objective FULLY, for the SAME system, and that the source itself includes
   this vehicle;
2. the source is an approved authoritative source -- a document in the local
   ADAS SI library -- whose file resolves inside the library root;
3. the exact evidence anchor is present in the text Core itself retrieved from
   that document (checked again here, independently of the evaluator);
4. the application is grounded: the request names year, make, and model, and
   the library's structured filing of the document does not contradict it;
5. the page is known, and the repository re-hashes the file and accepts the
   record as ``verified`` only when that hash matches (source integrity).

Model inference -- a claim no retrieved source text contains -- fails gate 3
and never becomes verified knowledge. Anything that fails a gate is simply
not promoted; the answer Otis gets is unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
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


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def promotion_refusal(
    *,
    vehicle: Any,
    finding: Any,
    evaluation: Any,
    candidate: Any,
) -> Optional[str]:
    """Why this evidence may not become verified knowledge, or None when every gate holds."""

    if not isinstance(evaluation, dict) or evaluation.get("outcome") != contract.SATISFIED:
        return "the research outcome is not SATISFIED"
    if evaluation.get("deliverable") != "answer":
        return "only an answer anchored to exact source text is promoted"
    review = evaluation.get("review")
    if not isinstance(review, dict) or review.get("malformed") is not False:
        return "no well-formed independent review"
    if review.get("answers_objective") != "FULLY" or review.get("system_match") != "SAME_SYSTEM":
        return "the review did not find a full answer for the same system"
    if review.get("vehicle_applicability") != "SOURCE_INCLUDES_THIS_VEHICLE":
        return "the source does not itself include this vehicle"
    if evaluation.get("library_conflict"):
        return "the library files this source for another vehicle"
    if not isinstance(finding, dict) or finding.get("source") != "adas_si":
        return "only the authoritative ADAS SI library is an approved source for promotion"
    if not finding.get("relative_path"):
        return "the library document is not identified"
    try:
        page = int(finding.get("page"))
    except (TypeError, ValueError):
        return "the evidence page is not known"
    if page < 1:
        return "the evidence page is not known"
    anchor = evaluation.get("anchor_quote") or review.get("anchor_quote")
    text = (candidate or {}).get("text") if isinstance(candidate, dict) else None
    if not contract.anchor_in_text(anchor, text):
        return "the evidence anchor is not in the retrieved source text"
    if not isinstance(vehicle, dict) or not all(vehicle.get(key) for key in ("year", "make", "model")):
        return "the vehicle application is not grounded (year, make, and model are required)"
    if contract.library_identity_conflict(vehicle, finding.get("library_vehicle")):
        return "the library files this source for another vehicle"
    return None


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
) -> dict[str, Any]:
    review = evaluation["review"]
    anchor = evaluation.get("anchor_quote") or review.get("anchor_quote")
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
    return {
        "application": application,
        "system": {"name": system or component or "not stated"},
        "component": {"name": component} if component else None,
        "repair_event": {"event_type": "research_objective", "description": objective},
        "requirement": {
            "requirement_type": _REQUIREMENT_TYPE_FOR_STAGE.get(stage, "informational"),
            "text": _clean(evaluation.get("source_answer") or review.get("source_answer"), 4_000),
            "applicability_notes": _clean(review.get("source_covers"), 4_000),
        },
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
                        "review": {
                            key: review.get(key)
                            for key in (
                                "question_asks",
                                "source_covers",
                                "vehicle_applicability",
                                "system_match",
                                "answers_objective",
                                "stage",
                                "confidence",
                            )
                        },
                    },
                },
                "page_start": int(finding["page"]),
                "excerpt": anchor,
                "extraction_status": "extracted",
                "verification_status": "verified",
                "confidence": review.get("confidence"),
            }
        ],
    }


def make_learner(repository: Any, adas: Any):
    """The ``learn`` hook ``delegate_research`` calls after a SATISFIED answer."""

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
            return {"promoted": False, "reason": "durable knowledge is not available"}
        try:
            path = adas.resolve_relative(str(finding["relative_path"]))
            digest = await asyncio.to_thread(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
        except Exception as exc:  # noqa: BLE001 - an unreadable source is simply not promoted
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
        )
        if payload.get("component") is None:
            payload.pop("component")
        try:
            outcome = await asyncio.to_thread(
                repository.create_record, payload, actor=PROMOTION_ACTOR
            )
        except Exception as exc:  # noqa: BLE001 - the repository's own gates refused it
            log.info("durable knowledge promotion refused: %s", exc)
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


__all__ = ["PROMOTION_ACTOR", "build_record", "make_learner", "promotion_refusal"]
