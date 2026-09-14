from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services import research_dependency_budget_extension as extension


class _Budget:
    def __init__(self) -> None:
        self.max_turns = 40
        self.dependency_slots = 3
        self.progress_events: list[dict] = []


@pytest.mark.asyncio
async def test_primary_keeps_full_40_and_required_dependencies_add_capacity_afterward():
    async def run_task(*args, **kwargs):  # noqa: ARG001
        return {
            "review": {
                "decision": "ACCEPT_WITH_DEPENDENCIES",
                "dependencies": [
                    {"title": "Wheel Alignment"},
                    {"title": "Camera Target Setup"},
                ],
            }
        }

    module = SimpleNamespace(_run_task=run_task)
    extension.install(module)
    budget = _Budget()

    await module._run_task(role="primary", budget=budget)

    assert budget.max_turns == 64
    event = budget.progress_events[-1]
    assert event["primary_turn_limit"] == 40
    assert event["objective_turn_limit"] == 64
    assert event["required_dependencies"] == 2
    assert event["dependency_turns_each"] == 12


@pytest.mark.asyncio
async def test_plain_accept_does_not_expand_the_budget():
    async def run_task(*args, **kwargs):  # noqa: ARG001
        return {"review": {"decision": "ACCEPT", "dependencies": []}}

    module = SimpleNamespace(_run_task=run_task)
    extension.install(module)
    budget = _Budget()

    await module._run_task(role="primary", budget=budget)

    assert budget.max_turns == 40
    assert budget.progress_events == []


@pytest.mark.asyncio
async def test_dependency_task_never_expands_its_own_budget():
    async def run_task(*args, **kwargs):  # noqa: ARG001
        return {
            "review": {
                "decision": "ACCEPT_WITH_DEPENDENCIES",
                "dependencies": [{"title": "Another Document"}],
            }
        }

    module = SimpleNamespace(_run_task=run_task)
    extension.install(module)
    budget = _Budget()

    await module._run_task(role="dependency", budget=budget)

    assert budget.max_turns == 40
    assert budget.progress_events == []
