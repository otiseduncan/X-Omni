from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services import research_navigator_vehicle_anchor as anchor


VIN = "1HGCY1F35PA033515"
MANAGED = SimpleNamespace(scrapex_project_path=r"X:\ScrapeX")
UNMANAGED = object()


class _ScrapeX:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def navigator(self, settings, args):  # noqa: ARG002
        self.calls.append(dict(args))
        if args.get("action") == "create_task":
            return {"success": True, "verified": True, "data": {"id": "task-1"}}
        return {"success": True, "verified": True, "data": {"id": "task-1"}}


def _module():
    calls: list[dict] = []
    scrapex = _ScrapeX()
    module = SimpleNamespace(scrapex_svc=scrapex)

    async def target_already_selected(settings, provider, target):  # noqa: ARG001
        calls.append(dict(target))
        return True

    async def run_task(*, target, settings=MANAGED, role="primary"):
        await module.scrapex_svc.navigator(
            settings,
            {
                "action": "create_task",
                "provider": "alldata",
                "target": target,
                "topic": "test",
            },
        )
        first = await module._target_already_selected(settings, "alldata", target)
        second = await module._target_already_selected(settings, "alldata", target)
        return first, second

    module._target_already_selected = target_already_selected
    module._run_task = run_task
    module._system_prompt = lambda target, *a, **k: "base prompt"
    module._vehicle_selection_note = lambda selected, target: "base note"
    module.calls = calls
    module.scrapex_calls = scrapex.calls
    return module


@pytest.mark.asyncio
async def test_valid_vin_forces_one_reselection_without_extra_picker_open():
    module = _module()
    anchor.install(module)

    first, second = await module._run_task(
        target={"year": 2023, "make": "Honda", "model": "Accord", "vin": VIN}
    )

    assert first is False
    assert second is True
    assert [call["action"] for call in module.scrapex_calls] == ["create_task"]


@pytest.mark.asyncio
async def test_ymm_primary_is_mechanically_reset_to_vehicle_picker_before_model_work():
    module = _module()
    anchor.install(module)

    target = {"year": 2025, "make": "Kia", "model": "K4"}
    first, second = await module._run_task(target=target)

    assert first is False
    assert second is True
    assert [call["action"] for call in module.scrapex_calls] == ["create_task", "open"]
    assert module.scrapex_calls[1]["url"].endswith("#/select-vehicle")
    prompt = module._system_prompt(target, "topic")
    assert "no valid VIN" in prompt
    assert "year, make, and model" in prompt


@pytest.mark.asyncio
async def test_each_new_ymm_primary_gets_a_fresh_picker_anchor():
    module = _module()
    anchor.install(module)
    target = {"year": 2025, "make": "Kia", "model": "K4"}

    assert await module._run_task(target=target) == (False, True)
    assert await module._run_task(target=target) == (False, True)
    assert [call["action"] for call in module.scrapex_calls].count("open") == 2


@pytest.mark.asyncio
async def test_dependency_task_preserves_current_vehicle_and_procedure_context():
    module = _module()
    anchor.install(module)
    target = {"year": 2025, "make": "Kia", "model": "K4"}

    first, second = await module._run_task(target=target, role="dependency")

    assert first is True
    assert second is True
    assert [call["action"] for call in module.scrapex_calls] == ["create_task"]


@pytest.mark.asyncio
async def test_unmanaged_ymm_keeps_hermetic_behavior():
    module = _module()
    anchor.install(module)
    target = {"year": 2025, "make": "Kia", "model": "K4"}

    first, second = await module._run_task(target=target, settings=UNMANAGED)

    assert first is True
    assert second is True
    assert [call["action"] for call in module.scrapex_calls] == ["create_task"]
