from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services import research_navigator_vehicle_anchor as anchor


VIN = "1HGCY1F35PA033515"
MANAGED = SimpleNamespace(scrapex_project_path=r"X:\ScrapeX")
UNMANAGED = object()


def _module():
    calls = []
    module = SimpleNamespace()

    async def target_already_selected(settings, provider, target):  # noqa: ARG001
        calls.append(dict(target))
        return True

    async def run_task(*, target, settings=MANAGED, role="primary"):
        first = await module._target_already_selected(settings, "alldata", target)
        second = await module._target_already_selected(settings, "alldata", target)
        return first, second

    module._target_already_selected = target_already_selected
    module._run_task = run_task
    module.calls = calls
    return module


@pytest.mark.asyncio
async def test_valid_vin_forces_one_primary_reselection_then_uses_real_signal():
    module = _module()
    anchor.install(module)

    first, second = await module._run_task(target={"vin": VIN}, role="primary")

    assert first is False
    assert second is True
    # The forced first answer is local; only post-selection confirmation
    # delegates to the real provider target signal.
    assert module.calls == [{"vin": VIN}]


@pytest.mark.asyncio
async def test_each_new_managed_primary_gets_a_fresh_vehicle_anchor():
    module = _module()
    anchor.install(module)

    assert await module._run_task(target={"vin": VIN}, role="primary") == (False, True)
    assert await module._run_task(target={"vin": VIN}, role="primary") == (False, True)
    assert len(module.calls) == 2


@pytest.mark.asyncio
async def test_dependency_task_preserves_current_verified_vehicle_context():
    module = _module()
    anchor.install(module)

    first, second = await module._run_task(target={"vin": VIN}, role="dependency")

    # No synthetic False: both reads are the provider's real selected-vehicle
    # signal, so the dependency can start from the procedure that named it.
    assert first is True
    assert second is True
    assert module.calls == [{"vin": VIN}, {"vin": VIN}]


@pytest.mark.asyncio
async def test_task_without_exact_vin_keeps_existing_behavior():
    module = _module()
    anchor.install(module)

    first, second = await module._run_task(
        target={"year": 2025, "make": "Kia", "model": "K4"},
        role="primary",
    )

    assert first is True
    assert second is True
    assert len(module.calls) == 2


@pytest.mark.asyncio
async def test_unmanaged_exact_vin_keeps_hermetic_behavior():
    module = _module()
    anchor.install(module)

    first, second = await module._run_task(
        target={"vin": VIN}, settings=UNMANAGED, role="primary"
    )

    assert first is True
    assert second is True
    assert module.calls == [{"vin": VIN}, {"vin": VIN}]
