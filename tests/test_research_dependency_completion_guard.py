from __future__ import annotations

import copy
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from core.services import research_dependency_completion_guard as guard


@dataclass
class Budget:
    max_turns: int
    turns_used: int = 0
    stall_points: int = 0
    dependency_slots: int = 3
    progress_events: list[dict] = field(default_factory=list)

    @property
    def turns_left(self):
        return max(0, self.max_turns - self.turns_used)


def _module(outcome, *, primary_turns=17):
    calls = []
    module = SimpleNamespace(DEPENDENCY_TURN_SHARE=0.5, _Budget=Budget)

    async def run_task(*args, **kwargs):  # noqa: ARG001
        budget = kwargs["budget"]
        calls.append(
            {
                "role": kwargs.get("role"),
                "max_turns": budget.max_turns,
                "dependency_slots": budget.dependency_slots,
            }
        )
        budget.turns_used = min(primary_turns, budget.max_turns)
        budget.stall_points = 2
        budget.progress_events.append({"kind": "fake_turn", "progress": True})
        return copy.deepcopy(outcome)

    module._run_task = run_task
    module.calls = calls
    return module


def _budget(*, max_turns=40, turns_used=0, dependency_slots=3):
    return Budget(
        max_turns=max_turns,
        turns_used=turns_used,
        dependency_slots=dependency_slots,
    )


@pytest.mark.asyncio
async def test_primary_reserves_dependency_capacity_inside_same_hard_ceiling():
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
                    "reason": (
                        "Required before the camera learn; center the steering angle sensor first."
                    ),
                },
                {"title": "Camera Setup", "reason": "Required target setup."},
            ],
        },
        "reviews": [],
        "candidates": [],
    }
    module = _module(outcome, primary_turns=17)
    guard.install(module)
    budget = _budget()

    result = await module._run_task(role="primary", budget=budget)

    # Existing contract: 40 total turns, 0.5 dependency share, three slots.
    # Agent's own formula gives 6 turns per dependency -> 18 reserved, leaving
    # a 22-turn primary cap. The objective ceiling itself stays exactly 40.
    assert module.calls == [
        {"role": "primary", "max_turns": 22, "dependency_slots": 0}
    ]
    assert budget.max_turns == 40
    assert budget.turns_used == 17
    assert budget.turns_left == 23
    assert budget.stall_points == 2
    event = budget.progress_events[-1]
    assert event["kind"] == "dependency_budget_reserved"
    assert event["hard_turn_limit"] == 40
    assert event["primary_turn_limit"] == 22
    assert event["reserved_dependency_turns"] == 18

    dependencies = result["review"]["dependencies"]
    assert [item["title"] for item in dependencies] == [
        "Steering Wheel Angle Sensor Centering",
        "Camera Setup",
    ]
    # Punctuation-only duplicate kept the more informative explanation.
    assert "center the steering angle sensor first" in dependencies[0]["reason"]


@pytest.mark.asyncio
async def test_reservation_does_not_increase_total_even_when_no_dependency_is_returned():
    module = _module(
        {
            "review": {
                "decision": "ACCEPT",
                "dependencies": [],
            }
        },
        primary_turns=10,
    )
    guard.install(module)
    budget = _budget()

    await module._run_task(role="primary", budget=budget)

    assert budget.max_turns == 40
    assert budget.turns_used == 10
    assert module.calls[0]["max_turns"] == 22
    assert budget.progress_events[-1]["reserved_dependency_turns"] == 18


@pytest.mark.asyncio
async def test_dependency_task_uses_its_given_private_budget_without_recursive_reserve():
    module = _module(
        {
            "review": {
                "decision": "ACCEPT_WITH_DEPENDENCIES",
                "dependencies": [
                    {"title": "Another Prerequisite", "reason": "Named by this document."}
                ],
            }
        },
        primary_turns=6,
    )
    guard.install(module)
    budget = _budget(max_turns=13, dependency_slots=0)

    await module._run_task(role="dependency", budget=budget)

    assert module.calls == [
        {"role": "dependency", "max_turns": 13, "dependency_slots": 0}
    ]
    assert budget.max_turns == 13
    assert budget.turns_used == 6
    assert all(
        item.get("kind") != "dependency_budget_reserved"
        for item in budget.progress_events
    )


def test_reserve_matches_agent_dependency_share_formula():
    module = SimpleNamespace(DEPENDENCY_TURN_SHARE=0.5)
    budget = _budget(max_turns=40, dependency_slots=3)

    primary, reserved = guard._dependency_reserve(module, budget)  # noqa: SLF001

    assert (primary, reserved) == (22, 18)
    assert primary + reserved == 40
