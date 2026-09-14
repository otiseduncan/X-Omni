from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services import scrapex_navigator_runtime


@pytest.mark.asyncio
async def test_create_task_starts_scrapex_before_posting_task():
    calls: list[str] = []

    async def start_native(settings):  # noqa: ARG001
        calls.append("start")
        return {"success": True, "verified": True}

    async def navigator(settings, args):  # noqa: ARG001
        calls.append(f"navigator:{args['action']}")
        return {"success": True, "verified": True, "data": {"id": "task-1"}}

    module = SimpleNamespace(start_native=start_native, navigator=navigator)
    scrapex_navigator_runtime.install(module)

    result = await module.navigator(object(), {"action": "create_task"})

    assert result["success"] is True
    assert calls == ["start", "navigator:create_task"]


@pytest.mark.asyncio
async def test_create_task_returns_startup_failure_without_mutation():
    calls: list[str] = []

    async def start_native(settings):  # noqa: ARG001
        calls.append("start")
        return {"success": False, "status": "offline", "detail": "failed to bind"}

    async def navigator(settings, args):  # noqa: ARG001
        calls.append("navigator")
        return {"success": True}

    module = SimpleNamespace(start_native=start_native, navigator=navigator)
    scrapex_navigator_runtime.install(module)

    result = await module.navigator(object(), {"action": "create_task"})

    assert result["success"] is False
    assert result["status"] == "runtime_unavailable"
    assert result["executed"] is False
    assert calls == ["start"]


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

    result = await module.navigator(object(), {"action": "observe", "task_id": "task-1"})

    assert result["success"] is True
    assert calls == ["navigator:observe"]
