from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from core.services import scrapex


@dataclass
class FakeSettings:
    scrapex_base_url: str = "http://127.0.0.1:8125"


def _install_transport(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        scrapex.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )


def test_navigator_schema_uses_complete_action_specific_branches() -> None:
    parameters = scrapex.SCRAPEX_NAVIGATOR_SCHEMA["parameters"]
    Draft202012Validator.check_schema(parameters)
    validator = Draft202012Validator(parameters)

    valid_arguments = [
        {
            "action": "create_task",
            "provider": "alldata",
            "target": {"year": 2023, "make": "Toyota", "model": "Camry"},
            "topic": "blind spot monitor calibration",
        },
        {"action": "create_task", "provider": "alldata", "target": {}, "topic": "t", "action_budget": 30},
        {"action": "observe", "task_id": "task-1"},
        {"action": "verify", "task_id": "task-1"},
        {"action": "get_evidence", "task_id": "task-1"},
        {"action": "click", "task_id": "task-1", "ref": "e1"},
        {"action": "fill", "task_id": "task-1", "ref": "e1", "text": "Camry"},
        {"action": "press", "task_id": "task-1", "ref": "e1", "key": "Enter"},
        {"action": "back", "task_id": "task-1"},
        {"action": "open", "task_id": "task-1", "url": "https://my.alldata.com/x"},
        {"action": "extract", "task_id": "task-1"},
        {"action": "done", "task_id": "task-1"},
    ]
    for arguments in valid_arguments:
        assert validator.is_valid(arguments), arguments

    invalid_arguments = [
        {"action": "create_task", "provider": "alldata", "target": {}, "topic": "t", "task_id": "x"},
        {"action": "observe"},
        {"action": "click", "task_id": "task-1"},
        {"action": "click", "task_id": "task-1", "ref": "e1", "text": "x"},
        {"action": "fill", "task_id": "task-1", "ref": "e1"},
        {"action": "press", "task_id": "task-1", "ref": "e1"},
        {"action": "open", "task_id": "task-1"},
        {"action": "create_task", "provider": "nope", "target": {}, "topic": "t"},
    ]
    for arguments in invalid_arguments:
        assert not validator.is_valid(arguments), arguments

    branches = parameters["oneOf"]
    assert len(branches) == len(scrapex.NAVIGATOR_ACTIONS)
    assert all(branch["additionalProperties"] is False for branch in branches)
    assert {branch["properties"]["action"]["const"] for branch in branches} == (
        scrapex.NAVIGATOR_ACTIONS
    )


def test_navigator_tool_is_registered_in_the_static_schema_map() -> None:
    assert scrapex.SCRAPEX_TOOL_SCHEMAS["scrapex_navigator"] is scrapex.SCRAPEX_NAVIGATOR_SCHEMA


@pytest.mark.asyncio
async def test_create_task_echoes_provider_target_and_topic(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/navigator/tasks"
        body = json.loads(request.content)
        assert body == {
            "provider": "alldata",
            "target": {"year": 2023, "make": "Toyota", "model": "Camry"},
            "topic": "blind spot monitor calibration",
        }
        return httpx.Response(
            200,
            json={
                "id": "task-1",
                "task_id": "task-1",
                "provider": "alldata",
                "target": body["target"],
                "topic": body["topic"],
                "state": "pending",
                "step_count": 0,
                "action_budget": 50,
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.navigator(
        FakeSettings(),
        {
            "action": "create_task",
            "provider": "alldata",
            "target": {"year": 2023, "make": "Toyota", "model": "Camry"},
            "topic": "blind spot monitor calibration",
        },
    )

    assert result["status"] == "created"
    assert result["success"] is True
    assert result["data"]["id"] == "task-1"


@pytest.mark.asyncio
async def test_create_task_rejects_mismatched_provider_echo(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "task-1",
                "provider": "some-other-provider",
                "target": {},
                "topic": "t",
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.navigator(
        FakeSettings(),
        {"action": "create_task", "provider": "alldata", "target": {}, "topic": "t"},
    )

    assert result["success"] is False
    assert result["status"] == "indeterminate"
    assert result["error"]["contract_code"] == "navigator_provider_mismatch"


@pytest.mark.asyncio
async def test_observe_requires_well_formed_elements(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/navigator/tasks/task-1/observe"
        return httpx.Response(
            200,
            json={"url": "https://my.alldata.com/x", "title": "X", "elements": [{"ref": "e1"}]},
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.navigator(
        FakeSettings(), {"action": "observe", "task_id": "task-1"}
    )

    assert result["success"] is False
    assert result["error"]["contract_code"] == "navigator_observation_malformed"


@pytest.mark.asyncio
async def test_click_sends_the_exact_ref_and_task_id(monkeypatch):
    requests: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "url": "https://my.alldata.com/systems",
                "title": "Systems",
                "elements": [{"ref": "e2", "role": "link", "name": "Adjustments"}],
                "action_executed": True,
                "is_search_action": False,
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.navigator(
        FakeSettings(), {"action": "click", "task_id": "task-1", "ref": "e1"}
    )

    assert requests == [
        ("POST", "/api/navigator/tasks/task-1/act", {"action": "click", "ref": "e1"})
    ]
    assert result["status"] == "acted"
    assert result["success"] is True
    assert result["work_complete"] is False


@pytest.mark.asyncio
async def test_done_marks_work_complete(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"url": "https://x/", "title": "X", "elements": []}
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.navigator(
        FakeSettings(), {"action": "done", "task_id": "task-1"}
    )
    assert result["work_complete"] is True


@pytest.mark.asyncio
async def test_verify_reports_unverified_without_failing_the_call(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/navigator/tasks/task-1/verify"
        return httpx.Response(
            200,
            json={
                "vehicle_verified": True,
                "subject_verified": False,
                "procedure_leaf_verified": False,
                "content_extracted": False,
                "verified": False,
                "reason": "No target-scoped search/navigation action was submitted.",
                "provider": "alldata",
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.navigator(
        FakeSettings(), {"action": "verify", "task_id": "task-1"}
    )

    assert result["status"] == "unverified"
    assert result["success"] is False
    assert result["verified"] is False
    assert result["data"]["vehicle_verified"] is True


@pytest.mark.asyncio
async def test_verify_reports_verified_when_all_gates_pass(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "vehicle_verified": True,
                "subject_verified": True,
                "procedure_leaf_verified": True,
                "content_extracted": True,
                "verified": True,
                "reason": None,
                "provider": "alldata",
                "evidence_sha256": "a" * 64,
                "source_url": "https://my.alldata.com/leaf",
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.navigator(
        FakeSettings(), {"action": "verify", "task_id": "task-1"}
    )

    assert result["status"] == "verified"
    assert result["success"] is True
    assert result["verified"] is True
    assert result["work_complete"] is True


@pytest.mark.asyncio
async def test_get_evidence_rejects_a_task_id_mismatch(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"task_id": "task-999", "provider": "alldata", "verified": True},
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.navigator(
        FakeSettings(), {"action": "get_evidence", "task_id": "task-1"}
    )

    assert result["success"] is False
    assert result["error"]["contract_code"] == "navigator_task_mismatch"


@pytest.mark.asyncio
async def test_unsupported_provider_is_rejected_before_any_request(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not contact ScrapeX for an unsupported provider")

    _install_transport(monkeypatch, handler)
    result = await scrapex.navigator(
        FakeSettings(),
        {"action": "create_task", "provider": "carfax", "target": {}, "topic": "t"},
    )

    assert result["status"] == "invalid_request"
    assert result["success"] is False


@pytest.mark.asyncio
async def test_current_page_signals_returns_the_bounded_signal_list(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/navigator/providers/alldata/current-page-signals"
        return httpx.Response(
            200,
            json={
                "provider": "alldata",
                "authenticated": True,
                "signals": ["2023 Toyota Camry - ALLDATA"],
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.navigator_current_page_signals(FakeSettings(), "alldata")

    assert result["success"] is True
    assert result["data"]["authenticated"] is True
    assert result["data"]["signals"] == ["2023 Toyota Camry - ALLDATA"]


@pytest.mark.asyncio
async def test_current_page_signals_rejects_a_provider_echo_mismatch(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"provider": "other", "authenticated": True, "signals": []}
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.navigator_current_page_signals(FakeSettings(), "alldata")

    assert result["success"] is False
    assert result["error"]["contract_code"] == "navigator_provider_mismatch"


@pytest.mark.asyncio
async def test_current_page_signals_rejects_unsupported_provider(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not contact ScrapeX for an unsupported provider")

    _install_transport(monkeypatch, handler)
    result = await scrapex.navigator_current_page_signals(FakeSettings(), "carfax")

    assert result["status"] == "invalid_request"
    assert result["success"] is False


@pytest.mark.asyncio
async def test_malformed_task_id_is_rejected_before_any_request(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not contact ScrapeX for a malformed task_id")

    _install_transport(monkeypatch, handler)
    result = await scrapex.navigator(
        FakeSettings(), {"action": "observe", "task_id": "not a valid id!"}
    )

    assert result["status"] == "invalid_request"
    assert result["success"] is False
