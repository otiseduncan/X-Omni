from __future__ import annotations

from core.services import research_primary_procedure_guard as guard


def _verdict(classification: str):
    return {
        "classification": classification,
        "procedure_type": "INITIALIZATION_OR_RELEARN",
        "vehicle_match": "MATCHES",
        "evidence": {"execution_steps": "PRESENT"},
        "dependencies": [],
        "decision": "ACCEPT",
        "confidence": 0.94,
        "evidence_summary": "The page contains executable steps.",
        "malformed": False,
    }


def test_supporting_page_cannot_close_primary_objective():
    verdict = guard.apply_primary_procedure_boundary(
        _verdict("REQUIRED_SUPPORTING_PROCEDURE"),
        objective={"objective": "Seat Belt calibration procedure", "system": "Seat Belt"},
    )
    assert verdict["decision"] == "CONTINUE_SEARCH"
    assert verdict["original_decision"] == "ACCEPT"
    assert verdict["classification"] == "REQUIRED_SUPPORTING_PROCEDURE"
    assert verdict["primary_procedure_check"]["dependency_task"] is False


def test_supporting_page_may_close_a_real_dependency_task():
    original = _verdict("REQUIRED_SUPPORTING_PROCEDURE")
    verdict = guard.apply_primary_procedure_boundary(
        original,
        objective={
            "objective": "Front radar adjustment",
            "dependency_context": "Required document 'Wheel Alignment Pre-check'",
        },
    )
    assert verdict is original
    assert verdict["decision"] == "ACCEPT"


def test_actual_procedure_still_closes_primary_objective():
    original = _verdict("ACTUAL_PROCEDURE")
    verdict = guard.apply_primary_procedure_boundary(
        original,
        objective={"objective": "Front radar adjustment", "system": "Front Radar"},
    )
    assert verdict is original
    assert verdict["decision"] == "ACCEPT"


def test_prompt_judges_operation_check_by_steps_not_title():
    prompt = guard._PROMPT_SUFFIX  # noqa: SLF001
    assert "Operation Check" in prompt
    assert "Beam Axis Inspection" in prompt
    assert "ACTUAL_PROCEDURE" in prompt
    assert "functional role from its steps, not its title" in prompt
    assert "only tells the technician whether another procedure is needed" in prompt
