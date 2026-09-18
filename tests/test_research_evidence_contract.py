"""The one research outcome, and the shared evaluator behind it.

SATISFIED / PARTIAL / UNSATISFIED is the only operational research outcome;
retrieval is never one. Every judgement is the model's, returned through a
forced review tool; Core checks only shape, the anchor quote, and structured
Year/Make/Model disagreement.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.services import research_evidence_contract as contract
from core.services import research_semantic_review


class ScriptedReviewer:
    """A model client that answers every review with one scripted tool call."""

    def __init__(self, arguments: Any, *, name: str | None = None, prose: str | None = None):
        self.arguments = arguments
        self.name = name
        self.prose = prose
        self.calls: list[dict[str, Any]] = []

    async def stream(self, messages, tools=None, max_tokens=None, tool_choice=None, temperature=None):
        self.calls.append(
            {"messages": messages, "tools": tools, "tool_choice": tool_choice, "temperature": temperature}
        )
        if self.prose is not None:
            yield {"type": "content", "text": self.prose}
            return
        yield {
            "type": "tool_call",
            "name": self.name or tools[0]["function"]["name"],
            "arguments": self.arguments if isinstance(self.arguments, str) else json.dumps(self.arguments),
        }


KIA_CHART = (
    "Hyundai / Kia / Genesis Front Radar Bumper Requirement\n"
    "Model | Year | Bumper during calibration\n"
    "K4 | 2025 | OFF\n"
    "Forte | 2021-2023 | ON"
)
VEHICLE_2025_K4 = {"year": 2025, "make": "Kia", "model": "K4"}


def _answer(**overrides: Any) -> dict[str, Any]:
    review = {
        "question_asks": "Does the front bumper stay on during front radar calibration on a 2025 Kia K4?",
        "source_covers": "Hyundai, Kia, and Genesis models by year, front radar",
        "vehicle_applicability": "SOURCE_INCLUDES_THIS_VEHICLE",
        "system_match": "SAME_SYSTEM",
        "anchor_quote": "K4 | 2025 | OFF",
        "source_answer": "The front bumper comes off for front radar calibration on the 2025 K4.",
        "stage": "calibration",
        "answers_objective": "FULLY",
        "unresolved": [],
        "confidence": 0.9,
    }
    review.update(overrides)
    return review


def _candidate(**overrides: Any) -> dict[str, Any]:
    candidate = {"title": "Front Radar Bumper Requirement", "text": KIA_CHART, "page": 1}
    candidate.update(overrides)
    return candidate


async def _evaluate(reviewer, *, vehicle=VEHICLE_2025_K4, candidate=None, deliverable="answer"):
    return await contract.evaluate(
        client=reviewer,
        objective={"objective": "Does the bumper stay on for front radar calibration?", "system": "front radar"},
        vehicle=vehicle,
        candidate=candidate or _candidate(),
        provider="ADAS SI",
        deliverable=deliverable,
    )


def test_outcomes_are_the_only_operational_states_and_combine_to_the_best() -> None:
    assert contract.OUTCOMES == ("SATISFIED", "PARTIAL", "UNSATISFIED")
    assert contract.combine([]) == "UNSATISFIED"
    assert contract.combine(["UNSATISFIED", "PARTIAL"]) == "PARTIAL"
    assert contract.combine(["PARTIAL", "SATISFIED", "UNSATISFIED"]) == "SATISFIED"
    # Anything that is not an outcome -- a retrieval status -- counts for nothing.
    assert contract.combine(["success", "partial_success", "verified"]) == "UNSATISFIED"


def test_procedure_verdicts_map_accept_with_dependencies_to_partial() -> None:
    assert contract.outcome_from_procedure_review({"decision": "ACCEPT", "malformed": False})[0] == "SATISFIED"
    outcome, reasons = contract.outcome_from_procedure_review(
        {
            "decision": "ACCEPT_WITH_DEPENDENCIES",
            "malformed": False,
            "dependencies": [{"title": "Wheel Alignment", "reason": "first", "quote": "perform alignment"}],
        }
    )
    assert outcome == "PARTIAL" and "Wheel Alignment" in reasons[0]
    for decision in ("FOLLOW_DEPENDENCY", "CONTINUE_SEARCH", "REJECT", "UNCERTAIN"):
        assert contract.outcome_from_procedure_review({"decision": decision, "malformed": False})[0] == "UNSATISFIED"
    assert contract.outcome_from_procedure_review(research_semantic_review.malformed_review("x"))[0] == "UNSATISFIED"


@pytest.mark.asyncio
async def test_an_anchored_answer_for_this_vehicle_and_system_is_satisfied() -> None:
    reviewer = ScriptedReviewer(_answer())
    evaluation = await _evaluate(reviewer)
    assert evaluation["outcome"] == "SATISFIED"
    assert evaluation["anchor_quote"] == "K4 | 2025 | OFF"
    assert evaluation["stage"] == "calibration"
    call = reviewer.calls[0]
    # A forced, deterministic review in a fresh context holding only the evidence.
    assert call["tool_choice"] == {"type": "function", "function": {"name": contract.ANSWER_REVIEW_TOOL_NAME}}
    assert call["temperature"] == 0.0
    assert [message["role"] for message in call["messages"]] == ["system", "user"]


def test_the_review_schema_quotes_the_source_before_judging_it() -> None:
    order = list(contract.ANSWER_REVIEW_TOOL["function"]["parameters"]["properties"])
    assert order.index("source_covers") < order.index("vehicle_applicability")
    assert order.index("anchor_quote") < order.index("answers_objective")
    assert order.index("answers_objective") < order.index("confidence")


@pytest.mark.asyncio
async def test_retrieval_is_not_satisfaction_a_related_document_is_unsatisfied() -> None:
    # A bumper removal page is about the right part and answers nothing asked.
    evaluation = await _evaluate(
        ScriptedReviewer(_answer(answers_objective="RELATED_ONLY", anchor_quote="", source_answer=""))
    )
    assert evaluation["outcome"] == "UNSATISFIED"
    assert any("related" in reason for reason in evaluation["reasons"])


@pytest.mark.asyncio
async def test_evidence_for_another_model_year_cannot_establish_this_one() -> None:
    # The reviewer itself says the source covers other years.
    evaluation = await _evaluate(
        ScriptedReviewer(_answer(vehicle_applicability="SOURCE_COVERS_OTHER_VEHICLES_OR_YEARS")),
        vehicle={"year": 2021, "make": "Kia", "model": "K4"},
    )
    assert evaluation["outcome"] == "UNSATISFIED"

    # The reviewer over-reaches, but the library files the document for 2025:
    # a structured Year/Make/Model disagreement vetoes the acceptance.
    evaluation = await _evaluate(
        ScriptedReviewer(_answer()),
        vehicle={"year": 2021, "make": "Kia", "model": "K4"},
        candidate=_candidate(library_vehicle={"year": 2025, "make": "Kia", "model": "K4"}),
    )
    assert evaluation["outcome"] == "UNSATISFIED"
    assert "model year 2025" in evaluation["library_conflict"]

    # A source whose filing covers the year is not vetoed.
    evaluation = await _evaluate(
        ScriptedReviewer(_answer()),
        candidate=_candidate(library_vehicle={"year_start": 2024, "year_end": 2026, "manufacturer": "Kia", "model": "K4"}),
    )
    assert evaluation["outcome"] == "SATISFIED"


@pytest.mark.asyncio
async def test_a_different_system_never_answers_the_question() -> None:
    evaluation = await _evaluate(ScriptedReviewer(_answer(system_match="DIFFERENT_SYSTEM")))
    assert evaluation["outcome"] == "UNSATISFIED"


@pytest.mark.asyncio
async def test_an_anchor_that_is_not_in_the_source_is_an_ungrounded_answer() -> None:
    evaluation = await _evaluate(ScriptedReviewer(_answer(anchor_quote="K4 | 2025 | ON (bumper stays installed)")))
    assert evaluation["outcome"] == "UNSATISFIED"
    assert evaluation["review"]["anchor_grounded"] is False
    assert evaluation["review"]["dropped_anchor"].startswith("K4 | 2025 | ON")
    assert evaluation["anchor_quote"] is None


@pytest.mark.asyncio
async def test_a_partial_answer_names_what_is_still_open() -> None:
    evaluation = await _evaluate(
        ScriptedReviewer(_answer(answers_objective="PARTLY", unresolved=["the aiming target distance"]))
    )
    assert evaluation["outcome"] == "PARTIAL"
    assert evaluation["unresolved"] == ["the aiming target distance"]
    evaluation = await _evaluate(ScriptedReviewer(_answer(vehicle_applicability="SOURCE_DOES_NOT_SAY")))
    assert evaluation["outcome"] == "PARTIAL"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reviewer",
    [
        ScriptedReviewer(None, prose="The bumper stays on."),
        ScriptedReviewer("{not json"),
        ScriptedReviewer({"answers_objective": "FULLY"}),
        ScriptedReviewer(_answer(), name="some_other_tool"),
    ],
)
async def test_every_malformed_review_is_unsatisfied(reviewer) -> None:
    evaluation = await _evaluate(reviewer)
    assert evaluation["outcome"] == "UNSATISFIED"
    assert evaluation["review"]["malformed"] is True


@pytest.mark.asyncio
async def test_no_model_means_nothing_is_accepted() -> None:
    evaluation = await contract.evaluate(
        client=None,
        objective={"objective": "x"},
        vehicle=VEHICLE_2025_K4,
        candidate=_candidate(),
        provider="ADAS SI",
    )
    assert evaluation["outcome"] == "UNSATISFIED" and evaluation["reviewed"] is False


@pytest.mark.asyncio
async def test_a_procedure_deliverable_uses_the_same_reviewer_ciq_research_uses(monkeypatch) -> None:
    seen: list[dict[str, Any]] = []

    async def review_candidate(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return {
            "decision": "ACCEPT_WITH_DEPENDENCIES",
            "classification": "ACTUAL_PROCEDURE",
            "dependencies": [{"title": "Wheel Alignment", "reason": "first", "quote": "align first"}],
            "evidence_summary": "BSM aiming steps.",
            "malformed": False,
        }

    monkeypatch.setattr(research_semantic_review, "review_candidate", review_candidate)
    evaluation = await _evaluate(object(), deliverable="procedure")
    assert len(seen) == 1 and seen[0]["provider"] == "ADAS SI"
    assert evaluation["outcome"] == "PARTIAL"
    assert evaluation["unresolved"] == ["Wheel Alignment"]


def test_the_evaluator_client_is_bound_per_tool_call() -> None:
    assert contract.current_model_client() is None
    sentinel = object()
    token = contract.bind_model_client(sentinel)
    try:
        assert contract.current_model_client() is sentinel
    finally:
        contract.reset_model_client(token)
    assert contract.current_model_client() is None
