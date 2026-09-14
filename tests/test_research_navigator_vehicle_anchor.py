from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services import research_navigator_vehicle_anchor as anchor


VIN = "1HGCY1F35PA033515"


def _module():
    calls = []
    module = SimpleNamespace()

    async def target_already_selected(settings, provider, target):  # noqa: ARG001
        calls.append(dict(target))
        return True

    async def run_task(*, target):
        first = await module._target_already_selected(object(), "alldata", target)
        second = await module._target_already_selected(object(), "alldata", target)
        return first, second

    module._target_already_selected = target_already_selected
    module._run_task = run_task
    module.calls = calls
    return module


@pytest.mark.asyncio
async def test_valid_vin_forces_one_reselection_then_uses_real_signal():
    module = _module()
    anchor.install(module)

    first, second = await module._run_task(target={"vin": VIN})

    assert first is False
    assert second is True
    # The forced first answer is local; only the post-selection confirmation
    # delegates to the real provider target signal.
    assert module.calls == [{"vin": VIN}]


@pytest.mark.asyncio
async def test_each_new_task_gets_a_fresh_vehicle_anchor():
    module = _module()
    anchor.install(module)

    assert await module._run_task(target={"vin": VIN}) == (False, True)
    assert await module._run_task(target={"vin": VIN}) == (False, True)
    assert len(module.calls) == 2


@pytest.mark.asyncio
async def test_task_without_exact_vin_keeps_existing_behavior():
    module = _module()
    anchor.install(module)

    first, second = await module._run_task(
        target={"year": 2025, "make": "Kia", "model": "K4"}
    )

    assert first is True
    assert second is True
    assert len(module.calls) == 2
