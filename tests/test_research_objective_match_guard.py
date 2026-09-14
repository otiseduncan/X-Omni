from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.services import research_objective_match_guard as guard


class _Client:
    def __init__(self, match: str):
        self.match = match

    async def stream(self, messages, tools=None, max_tokens=None, tool_choice=None):  # noqa: ARG002
        yield {
            "type": "tool_call",
            "name": "report_objective_match",
            "arguments": json.dumps(
                {
                    "match": self.match,
                    "confidence": 0.95,
                    "reason": "structured reason",
                }
            ),
        }


def _review_module(decision: str = "ACCEPT"):
    async def review_candidate(**kwargs):  # noqa: ARG001
        return {
            "classification": "ACTUAL_PROCEDURE",
            "procedure_type": "STATIC_RADAR",
            "decision": decision,
            "confidence": 0.9,
            "evidence_summary": "base review",
        }

    return SimpleNamespace(review_candidate=review_candidate)


def _navigator_module():
    return SimpleNamespace(
        review_candidate=None,
        _next_instruction_for_review=lambda review: "base instruction",
    )


@pytest.mark.asyncio
async def test_exact_match_preserves_acceptance():
    review = _review_module()
    navigator = _navigator_module()
    guard.install(review, navigator)

    verdict = await review.review_candidate(
        client=_Client("EXACT_MATCH"),
        objective={"objective": "rear side radar alignment"},
        vehicle={"year": 2022, "make": "Nissan", "model": "Sentra"},
        candidate={"title": "Side Radar Alignment", "text": "alignment steps"},
        provider="alldata",
    )

    assert verdict["decision"] == "ACCEPT"
    assert verdict["objective_match"]["match"] == "EXACT_MATCH"


@pytest.mark.asyncio
async def test_different_component_forces_branch_exit():
    review = _review_module()
    navigator = _navigator_module()
    guard.install(review, navigator)

    verdict = await review.review_candidate(
        client=_Client("DIFFERENT_COMPONENT"),
        objective={"objective": "rear side radar alignment"},
        vehicle={"year": 2022, "make": "Nissan", "model": "Sentra"},
        candidate={"title": "ICC Distance Sensor Alignment", "text": "front ICC radar steps"},
        provider="alldata",
    )

    assert verdict["decision"] == "CONTINUE_SEARCH"
    instruction = navigator._next_instruction_for_review(verdict)
    assert "Backtrack" in instruction
    assert "alternative ADAS components/systems" in instruction


@pytest.mark.asyncio
async def test_same_component_wrong_procedure_stays_in_component_branch():
    review = _review_module()
    navigator = _navigator_module()
    guard.install(review, navigator)

    verdict = await review.review_candidate(
        client=_Client("SAME_COMPONENT_WRONG_PROCEDURE"),
        objective={"objective": "front radar aiming"},
        vehicle={"year": 2023, "make": "Hyundai", "model": "Sonata"},
        candidate={"title": "Front Radar Removal", "text": "remove and install radar"},
        provider="alldata",
    )

    assert verdict["decision"] == "CONTINUE_SEARCH"
    instruction = navigator._next_instruction_for_review(verdict)
    assert "Stay within this same component/system area" in instruction
    assert "Backtrack" not in instruction


@pytest.mark.asyncio
async def test_dependency_context_does_not_regrade_against_primary_component():
    class _Explode:
        async def stream(self, *args, **kwargs):
            raise AssertionError("objective matcher must not run on a dependency task")

    review = _review_module()
    navigator = _navigator_module()
    guard.install(review, navigator)

    verdict = await review.review_candidate(
        client=_Explode(),
        objective={
            "objective": "front camera calibration",
            "dependency_context": "Required document 'Wheel Alignment'",
        },
        vehicle={"year": 2023, "make": "GMC", "model": "Acadia"},
        candidate={"title": "Wheel Alignment", "text": "alignment procedure"},
        provider="alldata",
    )

    assert verdict["decision"] == "ACCEPT"
    assert "objective_match" not in verdict
