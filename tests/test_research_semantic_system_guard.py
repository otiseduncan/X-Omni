from __future__ import annotations

from core.services import research_navigator_agent
from core.services import research_semantic_review
from core.services import research_semantic_system_guard as guard


def _verdict(procedure_type: str, decision: str = "ACCEPT") -> dict:
    return {
        "classification": "ACTUAL_PROCEDURE",
        "procedure_type": procedure_type,
        "vehicle_match": "MATCHES",
        "evidence": {field: "PRESENT" for field in research_semantic_review.EVIDENCE_FIELDS},
        "dependencies": [],
        "decision": decision,
        "confidence": 0.95,
        "evidence_summary": "Candidate contains a complete aiming procedure.",
        "malformed": False,
    }


def test_front_radar_objective_rejects_camera_acceptance_and_continues_search():
    objective = {
        "objective": "front millimeter wave radar aiming after radar replacement",
        "system": "Millimeter Wave Radar",
        "component": "front radar sensor",
    }
    verdict = guard.apply_system_consistency(
        _verdict("DYNAMIC_CAMERA"), objective=objective
    )

    assert verdict["decision"] == "CONTINUE_SEARCH"
    assert verdict["original_decision"] == "ACCEPT"
    assert verdict["system_family_check"] == {
        "objective_family": "radar",
        "candidate_family": "camera",
        "procedure_type": "DYNAMIC_CAMERA",
        "veto_only": True,
    }
    assert "Do not extract this page again" in verdict["evidence_summary"]
    assert "requested radar procedure" in verdict["evidence_summary"]
    assert research_semantic_review.accepted(verdict) is False

    instruction = research_navigator_agent._next_instruction_for_review(verdict)
    assert "NOT the requested procedure" in instruction
    assert "do not extract this page again" in instruction


def test_front_radar_objective_keeps_radar_acceptance():
    objective = {
        "objective": "front millimeter wave radar aiming",
        "system": "Millimeter Wave Radar",
    }
    verdict = guard.apply_system_consistency(
        _verdict("STATIC_RADAR"), objective=objective
    )
    assert verdict["decision"] == "ACCEPT"
    assert "system_family_check" not in verdict


def test_camera_objective_rejects_radar_acceptance_and_continues_search():
    objective = {
        "objective": "windshield camera calibration",
        "system": "Forward Looking Camera",
    }
    verdict = guard.apply_system_consistency(
        _verdict("STATIC_RADAR"), objective=objective
    )
    assert verdict["decision"] == "CONTINUE_SEARCH"
    assert verdict["system_family_check"]["objective_family"] == "camera"
    assert verdict["system_family_check"]["candidate_family"] == "radar"
    assert "requested camera procedure" in verdict["evidence_summary"]


def test_ambiguous_objective_is_not_semantically_decided_by_python():
    verdict = guard.apply_system_consistency(
        _verdict("BLIND_SPOT_RADAR"),
        objective={"objective": "blind spot calibration"},
    )
    assert verdict["decision"] == "ACCEPT"
    assert guard.objective_family({"objective": "blind spot calibration"}) is None


def test_non_accepting_verdict_is_never_promoted_or_changed():
    verdict = guard.apply_system_consistency(
        _verdict("DYNAMIC_CAMERA", decision="CONTINUE_SEARCH"),
        objective={"objective": "front radar calibration"},
    )
    assert verdict["decision"] == "CONTINUE_SEARCH"


def test_package_wiring_refreshes_navigator_reviewer_reference():
    assert research_navigator_agent.review_candidate is research_semantic_review.review_candidate
    assert "Radar and camera are different sensor families" in research_semantic_review.REVIEW_SYSTEM_PROMPT
