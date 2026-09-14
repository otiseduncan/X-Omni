from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from core.services import research_dependency_completion_guard as guard


def _module(outcome):
    module = SimpleNamespace(DEPENDENCY_TURN_SHARE=0.5)

    async def run_task(*args, **kwargs):  # noqa: ARG001
        return copy.deepcopy(outcome)

    module._run_task = run_task
    return module


def _budget(*, max_turns=40, turns_used=39, dependency_slots=3):
    return SimpleNamespace(
        max_turns=max_turns,
        turns_used=turns_used,
        dependency_slots=dependency_slots,
        progress_events=[],
    )


@pytest.mark.asyncio
async def test_required_dependencies_receive_budget_after_primary_finishes():
    outcome = {
        "review": {
            "decision": "ACCEPT_WITH_DEPENDENCIES",
            "dependencies": [
                {
                    "title": "Steering Wheel Angle Sensor Centering",
                    "reason": "Required before the camera learn.",
                },
                {
                    "title": "Steering-Wheel Angle Sensor Centering",
                    "reason": "Required before the camera learn; center the steering angle sensor first.",
                },
                {"title": "Camera Setup", "reason": "Required target setup."},
            ],
        },
        "reviews": [],
        "candidates": [],
    }
    module = _module(outcome)
    guard.install(module)
    budget = _budget()

    result = await module._run_task(role="primary", budget=budget)

    # The original 40 turns belonged to the primary. A 50% dependency share
    # therefore creates an 80-turn objective envelope only after primary exit.
    assert budget.max_turns == 80
    assert budget.turns_used == 39
    assert budget.progress_events[-1]["kind"] == "dependency_budget_reserved"
    assert budget.progress_events[-1]["primary_turn_limit"] == 40
    assert budget.progress_events[-1]["objective_turn_limit"] == 80

    dependencies = result["review"]["dependencies"]
    assert [item["title"] for item in dependencies] == [
        "Steering Wheel Angle Sensor Centering",
        "Camera Setup",
    ]
    # Punctuation-only duplicate kept the more informative explanation.
    assert "center the steering angle sensor first" in dependencies[0]["reason"]


@pytest.mark.asyncio
async def test_plain_accept_does_not_expand_budget_even_when_documents_are_noted():
    module = _module(
        {
            "review": {
                "decision": "ACCEPT",
                "dependencies": [{"title": "Related Information", "reason": "See also."}],
            }
        }
    )
    guard.install(module)
    budget = _budget(turns_used=12)

    await module._run_task(role="primary", budget=budget)

    assert budget.max_turns == 40
    assert budget.progress_events == []


@pytest.mark.asyncio
async def test_dependency_task_cannot_recursively_expand_its_private_budget():
    module = _module(
        {
            "review": {
                "decision": "ACCEPT_WITH_DEPENDENCIES",
                "dependencies": [{"title": "Another Prerequisite", "reason": "Named by this document."}],
            }
        }
    )
    guard.install(module)
    budget = _budget(max_turns=13, turns_used=10, dependency_slots=0)

    await module._run_task(role="dependency", budget=budget)

    assert budget.max_turns == 13
    assert budget.progress_events == []


@pytest.mark.asyncio
async def test_follow_dependency_also_reserves_supporting_document_capacity():
    module = _module(
        {
            "review": {
                "decision": "FOLLOW_DEPENDENCY",
                "dependencies": [{"title": "OEM Setup Procedure", "reason": "This page points to it."}],
            }
        }
    )
    guard.install(module)
    budget = _budget(turns_used=38)

    await module._run_task(role="primary", budget=budget)

    assert budget.max_turns == 80
