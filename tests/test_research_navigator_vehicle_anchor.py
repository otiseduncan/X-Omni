from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services import research_navigator_vehicle_anchor as anchor


MANAGED = SimpleNamespace(scrapex_project_path=r"X:\ScrapeX")
UNMANAGED = object()


def _module(target_signal, run_task):
    return SimpleNamespace(
        _target_already_selected=target_signal,
        _run_task=run_task,
    )


@pytest.mark.asyncio
async def test_first_managed_primary_check_forces_exact_vin_reselection() -> None:
    calls = []

    async def target_signal(settings, provider, target):
        calls.append((settings, provider, dict(target)))
        return True

    async def run_task(*args, **kwargs):
        target = kwargs["target"]
        first = await module._target_already_selected(MANAGED, "alldata", target)
        second = await module._target_already_selected(MANAGED, "alldata", target)
        return first, second

    module = _module(target_signal, run_task)
    anchor.install(module)

    result = await module._run_task(
        target={"vin": "1HGCY1F35PA033515"},
        role="primary",
    )

    assert result == (False, True)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_each_new_managed_primary_task_gets_a_fresh_vin_anchor() -> None:
    calls = []

    async def target_signal(settings, provider, target):
        calls.append(target["vin"])
        return True

    async def run_task(*args, **kwargs):
        return await module._target_already_selected(
            MANAGED, "alldata", kwargs["target"]
        )

    module = _module(target_signal, run_task)
    anchor.install(module)

    first = await module._run_task(
        target={"vin": "1HGCY1F35PA033515"},
        role="primary",
    )
    second = await module._run_task(
        target={"vin": "3KPFT4DE4SE215941"},
        role="primary",
    )

    assert first is False
    assert second is False
    assert calls == []


@pytest.mark.asyncio
async def test_dependency_task_preserves_same_vehicle_context() -> None:
    calls = []

    async def target_signal(settings, provider, target):
        calls.append(target["vin"])
        return True

    async def run_task(*args, **kwargs):
        target = kwargs["target"]
        first = await module._target_already_selected(MANAGED, "alldata", target)
        second = await module._target_already_selected(MANAGED, "alldata", target)
        return first, second

    module = _module(target_signal, run_task)
    anchor.install(module)

    result = await module._run_task(
        target={"vin": "1HGCY1F35PA033515"},
        role="dependency",
    )

    assert result == (True, True)
    assert calls == ["1HGCY1F35PA033515", "1HGCY1F35PA033515"]


@pytest.mark.asyncio
async def test_no_vin_never_gains_vehicle_anchor_side_effects() -> None:
    calls = []

    async def target_signal(settings, provider, target):
        calls.append(dict(target))
        return True

    async def run_task(*args, **kwargs):
        target = kwargs["target"]
        first = await module._target_already_selected(MANAGED, "alldata", target)
        second = await module._target_already_selected(MANAGED, "alldata", target)
        return first, second

    module = _module(target_signal, run_task)
    anchor.install(module)

    result = await module._run_task(
        target={"year": 2014, "make": "GMC", "model": "Acadia"},
        role="primary",
    )

    assert result == (True, True)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_unmanaged_exact_vin_keeps_hermetic_behavior() -> None:
    calls = []

    async def target_signal(settings, provider, target):
        calls.append(target["vin"])
        return True

    async def run_task(*args, **kwargs):
        target = kwargs["target"]
        first = await module._target_already_selected(UNMANAGED, "alldata", target)
        second = await module._target_already_selected(UNMANAGED, "alldata", target)
        return first, second

    module = _module(target_signal, run_task)
    anchor.install(module)

    result = await module._run_task(
        target={"vin": "1HGCY1F35PA033515"},
        role="primary",
    )

    assert result == (True, True)
    assert calls == ["1HGCY1F35PA033515", "1HGCY1F35PA033515"]
