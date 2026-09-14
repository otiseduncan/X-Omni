from __future__ import annotations

import json
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from core.services import research_ymm_mechanical_anchor as ymm_anchor


class _ScrapeX:
    OPERATOR_TIMEOUT = 180.0

    def __init__(self, *, selected=True, signal=True, ambiguous=False):
        self.selected = selected
        self.signal = signal
        self.ambiguous = ambiguous
        self.navigator_calls: list[dict] = []
        self.request_calls: list[dict] = []

    async def navigator(self, settings, args):  # noqa: ARG002
        self.navigator_calls.append(dict(args))
        return {
            "success": True,
            "verified": True,
            "data": {"id": "task-ymm-1"},
        }

    async def _request(self, settings, method, path, *, body, timeout, may_mutate):  # noqa: ARG002
        self.request_calls.append(
            {
                "method": method,
                "path": path,
                "body": dict(body),
                "timeout": timeout,
                "may_mutate": may_mutate,
            }
        )
        target = {
            "kind": "vehicle",
            "selected": self.selected,
            "needs_operator": self.ambiguous,
        }
        if self.ambiguous:
            target["candidates"] = ["2025 Ford Explorer 2.3L", "2025 Ford Explorer 3.0L"]
        return {
            "action_target": target,
            "action_detail": "multiple configurations" if self.ambiguous else None,
        }

    async def navigator_current_target_signal(self, settings, provider, target):  # noqa: ARG002
        assert provider == "alldata"
        return {
            "success": True,
            "verified": True,
            "data": {"provider": "alldata", "selected": self.signal},
        }


class _Anchor:
    def __init__(self):
        self._ANCHOR_STATE = ContextVar("test_ymm_anchor", default=None)


def _settings(managed=True):
    return SimpleNamespace(scrapex_project_path="X:/ScrapeX" if managed else None)


def _args(*, vin=""):
    target = {"year": 2025, "make": "Ford", "model": "Explorer"}
    if vin:
        target["vin"] = vin
    return {
        "action": "create_task",
        "provider": "alldata",
        "target": target,
        "topic": "360 degree camera alignment",
    }


@pytest.mark.asyncio
async def test_managed_ymm_task_is_mechanically_selected_before_model_navigation():
    scrapex = _ScrapeX(selected=True, signal=True)
    anchor = _Anchor()
    agent = SimpleNamespace()
    ymm_anchor.install(agent, anchor, scrapex)
    state = {
        "role": "primary",
        "mode": "year_make_model",
        "forced": False,
        "picker_anchor_verified": True,
    }
    token = anchor._ANCHOR_STATE.set(state)
    try:
        result = await scrapex.navigator(_settings(), _args())
    finally:
        anchor._ANCHOR_STATE.reset(token)

    assert result["success"] is True
    assert len(scrapex.request_calls) == 1
    call = scrapex.request_calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/navigator/tasks/task-ymm-1/act"
    assert call["body"]["action"] == "select_vehicle"
    payload = json.loads(call["body"]["text"])
    assert payload == {
        "year": 2025,
        "make": "Ford",
        "model": "Explorer",
        "trim": None,
        "engine": None,
    }
    assert state["forced"] is True
    assert state["ymm_mechanically_selected"] is True


@pytest.mark.asyncio
async def test_ymm_task_fails_closed_when_provider_cannot_prove_selection():
    scrapex = _ScrapeX(selected=True, signal=False)
    anchor = _Anchor()
    agent = SimpleNamespace()
    ymm_anchor.install(agent, anchor, scrapex)
    state = {"role": "primary", "mode": "year_make_model", "forced": False}
    token = anchor._ANCHOR_STATE.set(state)
    try:
        result = await scrapex.navigator(_settings(), _args())
    finally:
        anchor._ANCHOR_STATE.reset(token)

    assert result["success"] is False
    assert result["verified"] is False
    assert result["status"] == "vehicle_anchor_failed"
    assert state["forced"] is False


@pytest.mark.asyncio
async def test_ymm_task_reports_ambiguous_variants_instead_of_guessing():
    scrapex = _ScrapeX(selected=False, signal=False, ambiguous=True)
    anchor = _Anchor()
    agent = SimpleNamespace()
    ymm_anchor.install(agent, anchor, scrapex)
    state = {"role": "primary", "mode": "year_make_model", "forced": False}
    token = anchor._ANCHOR_STATE.set(state)
    try:
        result = await scrapex.navigator(_settings(), _args())
    finally:
        anchor._ANCHOR_STATE.reset(token)

    assert result["success"] is False
    assert result["status"] == "vehicle_identity_ambiguous"
    assert len(result["error"]["detail"]["candidates"]) == 2


@pytest.mark.asyncio
async def test_vin_task_stays_on_existing_exact_vin_fast_path():
    scrapex = _ScrapeX()
    anchor = _Anchor()
    agent = SimpleNamespace()
    ymm_anchor.install(agent, anchor, scrapex)
    state = {"role": "primary", "mode": "vin", "forced": False}
    token = anchor._ANCHOR_STATE.set(state)
    try:
        result = await scrapex.navigator(
            _settings(), _args(vin="1FMUK8DH8SGB00001")
        )
    finally:
        anchor._ANCHOR_STATE.reset(token)

    assert result["success"] is True
    assert scrapex.request_calls == []


@pytest.mark.asyncio
async def test_unmanaged_adapter_does_not_gain_browser_side_effects():
    scrapex = _ScrapeX()
    anchor = _Anchor()
    agent = SimpleNamespace()
    ymm_anchor.install(agent, anchor, scrapex)
    state = {"role": "primary", "mode": "year_make_model", "forced": False}
    token = anchor._ANCHOR_STATE.set(state)
    try:
        result = await scrapex.navigator(_settings(managed=False), _args())
    finally:
        anchor._ANCHOR_STATE.reset(token)

    assert result["success"] is True
    assert scrapex.request_calls == []
