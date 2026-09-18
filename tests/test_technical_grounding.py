"""Unsupported vehicle-specific claims never reach Otis as established fact.

Research results mark each finding accepted or not; the model-owned evidence
review reads only what accepted evidence establishes and flags a draft that
states, as fact for Otis's vehicle, what no accepted evidence establishes for
it. Flagging sends the draft to the tool-less revision. Core checks shape only.
"""

from __future__ import annotations

from core.orchestrator import evidence_review


def _check(**flags: bool) -> dict:
    payload = {
        "draft_claims": ["The 2021 K4 needs the bumper off for radar calibration."],
        "findings": [{"number": 1, "draft": "agrees"}],
        "problems": [],
        "draft_contradicts_evidence": False,
        "draft_uses_general_knowledge_over_evidence": False,
        "draft_credits_the_source_with_claims_it_does_not_make": False,
        "draft_flattens_stage_dependent_requirements": False,
        "draft_pastes_raw_extraction_or_tool_status": False,
        "draft_states_vehicle_facts_no_accepted_evidence_establishes": False,
    }
    payload.update(flags)
    return payload


READING = evidence_review.EvidenceReadingResult(
    asked_for_source=False,
    readings=(
        evidence_review.EvidenceReading(
            source_text="K4 | 2025 | OFF",
            finding="Bumper off for front radar calibration on the 2025 K4.",
            stage="calibration",
            applies_to="2025 Kia K4",
        ),
    ),
)


def test_the_check_schema_requires_the_grounding_judgement_after_the_problems() -> None:
    parameters = evidence_review.CHECK_TOOL["function"]["parameters"]
    order = list(parameters["properties"])
    flag = "draft_states_vehicle_facts_no_accepted_evidence_establishes"
    assert flag in parameters["required"]
    assert order.index("problems") < order.index(flag)


def test_a_draft_that_claims_another_years_evidence_for_this_vehicle_is_revised() -> None:
    grounded = evidence_review.parse_check(_check(), 1)
    assert grounded is not None and evidence_review.needs_revision(READING, grounded) is False
    ungrounded = evidence_review.parse_check(
        _check(draft_states_vehicle_facts_no_accepted_evidence_establishes=True), 1
    )
    assert ungrounded is not None and ungrounded.ungrounded_vehicle_fact is True
    assert evidence_review.needs_revision(READING, ungrounded) is True


def test_the_reader_and_reviser_are_told_unaccepted_evidence_establishes_nothing() -> None:
    assert "not accepted establishes nothing about Otis's vehicle" in evidence_review.READING_SYSTEM
    assert "PARTIAL or UNSATISFIED" in evidence_review.READING_SYSTEM
    assert "document that was not accepted" in evidence_review.CHECK_SYSTEM
    assert "never state as fact for his vehicle" in evidence_review.REVISION_INSTRUCTION
    # delegate_research results remain evidence the review always reads.
    assert "delegate_research" in evidence_review.EVIDENCE_TOOLS
