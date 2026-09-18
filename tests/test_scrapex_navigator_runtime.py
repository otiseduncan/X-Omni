from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services import scrapex_navigator_runtime


# ALLDATA is sunset (core.services.alldata_sunset): the runtime these tests
# exercise now refuses to run, which tests/test_alldata_sunset.py proves. They are
# kept, skipped, as the record of how the retired Navigator behaved.
_ALLDATA_SUNSET = pytest.mark.skip(
    reason="ALLDATA is sunset: this exercises the retired ALLDATA runtime, which now refuses to run."
)


def _managed_settings():
    return SimpleNamespace(scrapex_project_path=r"X:\ScrapeX")


def _valid_create():
    return {
        "action": "create_task",
        "provider": "alldata",
        "target": {"year": 2023, "make": "Honda", "model": "Accord"},
        "topic": "millimeter wave radar aiming",
        "action_budget": 40,
    }


@_ALLDATA_SUNSET
@pytest.mark.asyncio
async def test_create_task_starts_scrapex_before_posting_task():
    calls: list[str] = []

    async def start_native(settings):  # noqa: ARG001
        calls.append("start")
        return {"success": True, "verified": True}

    async def navigator(settings, args):  # noqa: ARG001
        calls.append(f"navigator:{args['action']}")
        return {"success": True, "verified": True, "data": {"id": "task-1"}}

    module = SimpleNamespace(
        start_native=start_native,
        navigator=navigator,
        NAVIGATOR_PROVIDERS=frozenset({"alldata"}),
        MAX_TOPIC_CHARS=400,
    )
    scrapex_navigator_runtime.install(module)

    result = await module.navigator(_managed_settings(), _valid_create())

    assert result["success"] is True
    assert calls == ["start", "navigator:create_task"]


@_ALLDATA_SUNSET
@pytest.mark.asyncio
async def test_create_task_returns_startup_failure_without_mutation():
    calls: list[str] = []

    async def start_native(settings):  # noqa: ARG001
        calls.append("start")
        return {"success": False, "status": "offline", "detail": "failed to bind"}

    async def navigator(settings, args):  # noqa: ARG001
        calls.append("navigator")
        return {"success": True}

    module = SimpleNamespace(
        start_native=start_native,
        navigator=navigator,
        NAVIGATOR_PROVIDERS=frozenset({"alldata"}),
        MAX_TOPIC_CHARS=400,
    )
    scrapex_navigator_runtime.install(module)

    result = await module.navigator(_managed_settings(), _valid_create())

    assert result["success"] is False
    assert result["status"] == "runtime_unavailable"
    assert result["executed"] is False
    assert calls == ["start"]


@_ALLDATA_SUNSET
@pytest.mark.asyncio
async def test_non_create_navigator_action_does_not_restart_scrapex():
    calls: list[str] = []

    async def start_native(settings):  # noqa: ARG001
        calls.append("start")
        return {"success": True}

    async def navigator(settings, args):  # noqa: ARG001
        calls.append(f"navigator:{args['action']}")
        return {"success": True}

    module = SimpleNamespace(start_native=start_native, navigator=navigator)
    scrapex_navigator_runtime.install(module)

    result = await module.navigator(
        _managed_settings(), {"action": "observe", "task_id": "task-1"}
    )

    assert result["success"] is True
    assert calls == ["navigator:observe"]


@_ALLDATA_SUNSET
@pytest.mark.asyncio
async def test_unmanaged_adapter_create_keeps_original_transport_behavior():
    calls: list[str] = []

    async def start_native(settings):  # noqa: ARG001
        calls.append("start")
        return {"success": True}

    async def navigator(settings, args):  # noqa: ARG001
        calls.append("navigator")
        return {"success": True, "status": "created"}

    module = SimpleNamespace(
        start_native=start_native,
        navigator=navigator,
        NAVIGATOR_PROVIDERS=frozenset({"alldata"}),
    )
    scrapex_navigator_runtime.install(module)

    result = await module.navigator(object(), _valid_create())

    assert result["success"] is True
    assert calls == ["navigator"]


@_ALLDATA_SUNSET
@pytest.mark.asyncio
async def test_invalid_provider_never_starts_managed_runtime():
    calls: list[str] = []

    async def start_native(settings):  # noqa: ARG001
        calls.append("start")
        return {"success": True}

    async def navigator(settings, args):  # noqa: ARG001
        calls.append("navigator")
        return {"success": False, "status": "invalid_request"}

    module = SimpleNamespace(
        start_native=start_native,
        navigator=navigator,
        NAVIGATOR_PROVIDERS=frozenset({"alldata"}),
    )
    scrapex_navigator_runtime.install(module)

    invalid = _valid_create()
    invalid["provider"] = "carfax"
    result = await module.navigator(_managed_settings(), invalid)

    assert result["status"] == "invalid_request"
    assert calls == ["navigator"]
