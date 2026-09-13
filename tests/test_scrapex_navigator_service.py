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
        {"action": "scroll", "task_id": "task-1", "delta_y": 800},
        {"action": "wait", "task_id": "task-1", "milliseconds": 700},
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
        {"action": "scroll", "task_id": "task-1", "delta_y": 5000},
        {"action": "wait", "task_id": "task-1", "milliseconds": 20},
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
async def test_scroll_and_wait_send_bounded_action_payloads(monkeypatch):
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"url": "https://my.alldata.com/x", "title": "X", "elements": []},
        )

    _install_transport(monkeypatch, handler)
    scroll = await scrapex.navigator(
        FakeSettings(), {"action": "scroll", "task_id": "task-1", "delta_y": 900}
    )
    wait = await scrapex.navigator(
        FakeSettings(), {"action": "wait", "task_id": "task-1", "milliseconds": 650}
    )

    assert scroll["success"] is True
    assert wait["success"] is True
    assert requests == [
        {"action": "scroll", "delta_y": 900},
        {"action": "wait", "milliseconds": 650},
    ]


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


@pytest.mark.asyncio
async def test_navigator_screenshot_is_task_bound_jpeg(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/navigator/tasks/task-1/screenshot"
        return httpx.Response(
            200,
            content=b"\xff\xd8\xffjpeg",
            headers={
                "content-type": "image/jpeg",
                "x-scrapex-task-id": "task-1",
            },
        )

    _install_transport(monkeypatch, handler)
    raw, mime = await scrapex.navigator_screenshot(FakeSettings(), "task-1")
    assert raw.startswith(b"\xff\xd8\xff")
    assert mime == "image/jpeg"


@pytest.mark.asyncio
async def test_navigator_screenshot_rejects_wrong_task_echo(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"\xff\xd8\xffjpeg",
            headers={
                "content-type": "image/jpeg",
                "x-scrapex-task-id": "task-999",
            },
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(scrapex.ScrapeXContract) as exc:
        await scrapex.navigator_screenshot(FakeSettings(), "task-1")
    assert exc.value.code == "navigator_task_mismatch"


@pytest.mark.asyncio
async def test_alldata_authentication_required_is_not_mislabeled_as_adas_map(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/navigator/tasks/task-1/observe"
        return httpx.Response(
            409,
            json={"detail": "ALLDATA requires interactive authentication."},
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.navigator(
        FakeSettings(), {"action": "observe", "task_id": "task-1"}
    )

    assert result["status"] == "authentication_required"
    assert result["provider"] == "alldata"
    assert result["requires_human"] is True
    assert "ALLDATA requires interactive authentication" in result["message"]
    assert "ADAS Map" not in result["message"]


# --------------------------------------------------------------------------
# The wire contract for observation-bound actions. These pin the exact bodies
# X Omni sends and the exact shapes it reads back, against the FastAPI routes
# ScrapeX serves -- the seam where a change in either repo would otherwise
# only show up live.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bound_ref_actions_carry_the_observation_and_marks_are_opt_in(monkeypatch):
    requests: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "observation_id": "obs_abc123",
                "page_identity": "pid",
                "url": "https://my.alldata.com/x",
                "title": "X",
                "viewport": {"width": 1280, "height": 720},
                "elements": [{"ref": "e1", "role": "link", "name": "Adjustment", "box": [10, 10, 90, 20]}],
                "marks": [{"mark": 21, "tag": "span", "name": "print", "box": [1200, 40, 24, 24]}],
                "controls_without_refs": 3,
            },
        )

    _install_transport(monkeypatch, handler)
    observed = await scrapex.navigator(
        FakeSettings(), {"action": "observe", "task_id": "task-1", "marks": True}
    )
    typed = await scrapex.navigator(
        FakeSettings(),
        {"action": "type", "task_id": "task-1", "ref": "e1", "text": "1HGCV1F1XPA000000", "observation_id": "obs_abc123"},
    )
    clicked = await scrapex.navigator(
        FakeSettings(), {"action": "click", "task_id": "task-1", "ref": "e1", "observation_id": "obs_abc123"}
    )

    assert requests[0] == ("/api/navigator/tasks/task-1/observe", {"marks": True})
    assert requests[1] == (
        "/api/navigator/tasks/task-1/act",
        {"action": "type", "observation_id": "obs_abc123", "ref": "e1", "text": "1HGCV1F1XPA000000"},
    )
    assert requests[2] == (
        "/api/navigator/tasks/task-1/act",
        {"action": "click", "observation_id": "obs_abc123", "ref": "e1"},
    )
    assert observed["data"]["observation_id"] == "obs_abc123"
    assert observed["data"]["marks"][0]["mark"] == 21
    assert typed["success"] is True and clicked["success"] is True


@pytest.mark.asyncio
async def test_mark_visual_and_vin_actions_send_their_exact_contracts(monkeypatch):
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "observation_id": "obs_next",
                "url": "https://my.alldata.com/y",
                "title": "Y",
                "elements": [],
                "action_target": {"kind": "mark", "mark": 21},
            },
        )

    _install_transport(monkeypatch, handler)
    await scrapex.navigator(
        FakeSettings(), {"action": "click_mark", "task_id": "task-1", "mark": 21, "observation_id": "obs_abc123"}
    )
    await scrapex.navigator(
        FakeSettings(),
        {"action": "click_visual", "task_id": "task-1", "x_norm": 0.94, "y_norm": 0.06, "observation_id": "obs_abc123"},
    )
    await scrapex.navigator(
        FakeSettings(), {"action": "select_vehicle", "task_id": "task-1", "vin": " 1hgcv1f1xpa000000 "}
    )

    assert requests == [
        {"action": "click_mark", "mark": 21, "observation_id": "obs_abc123"},
        {"action": "click_visual", "x_norm": 0.94, "y_norm": 0.06, "observation_id": "obs_abc123"},
        # The VIN is normalized before it leaves X Omni; ScrapeX validates it again.
        {"action": "select_vehicle", "vin": "1HGCV1F1XPA000000"},
    ]


@pytest.mark.asyncio
async def test_mark_and_visual_actions_require_their_observation_before_any_request(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("an unbound visual action must never reach ScrapeX")

    _install_transport(monkeypatch, handler)
    unbound_mark = await scrapex.navigator(
        FakeSettings(), {"action": "click_mark", "task_id": "task-1", "mark": 21}
    )
    unbound_visual = await scrapex.navigator(
        FakeSettings(), {"action": "click_visual", "task_id": "task-1", "x_norm": 0.5, "y_norm": 0.5}
    )
    out_of_range = await scrapex.navigator(
        FakeSettings(),
        {"action": "click_visual", "task_id": "task-1", "x_norm": 1.4, "y_norm": 0.5, "observation_id": "obs_a"},
    )

    for result in (unbound_mark, unbound_visual, out_of_range):
        assert result["status"] == "invalid_request"
        assert result["executed"] is False


@pytest.mark.asyncio
async def test_a_stale_target_refusal_reaches_the_caller_as_a_definitive_409(monkeypatch):
    """The refusal X's loop branches on: it must be definitive, carry ScrapeX's
    own code, and never be mistaken for an authentication boundary."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "detail": {
                    "code": "stale_target",
                    "message": "'e1' (link 'ADAS Quick Reference') moved from where it was observed.",
                    "observed_box": {"x": 10, "y": 10, "w": 90, "h": 20},
                    "current_box": {"x": 10, "y": 240, "w": 90, "h": 20},
                }
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.navigator(
        FakeSettings(), {"action": "click", "task_id": "task-1", "ref": "e1", "observation_id": "obs_abc123"}
    )

    assert result["success"] is False and result["executed"] is False
    assert result["status"] == "conflict" and result["http_status"] == 409
    assert result.get("authentication_required") is not True
    assert result["detail"]["code"] == "stale_target"

    from core.services import research_navigator_agent as agent

    # The loop reads ScrapeX's code, offers the mark/visual fallback, and
    # treats the rejection as definitive enough to re-observe.
    assert agent._failure_code(result) == "stale_target"
    assert agent._can_refresh_after_failure(result) is True
    message = agent._navigator_failure_message(result, "click")
    assert "moved from where it was observed" in message


@pytest.mark.asyncio
async def test_the_screenshot_is_bound_to_the_observation_it_belongs_to(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            content=b"\xff\xd8\xffjpeg",
            headers={
                "content-type": "image/jpeg",
                "x-scrapex-task-id": "task-1",
                "x-scrapex-observation-id": "obs_abc123",
            },
        )

    _install_transport(monkeypatch, handler)
    raw, mime = await scrapex.navigator_screenshot(FakeSettings(), "task-1", "obs_abc123")
    assert raw.startswith(b"\xff\xd8\xff") and mime == "image/jpeg"
    assert "observation_id=obs_abc123" in seen[0]

@pytest.mark.asyncio
async def test_a_screenshot_of_a_newer_observation_is_refused(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"\xff\xd8\xffjpeg",
            headers={
                "content-type": "image/jpeg",
                "x-scrapex-task-id": "task-1",
                "x-scrapex-observation-id": "obs_newer",
            },
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(scrapex.ScrapeXContract) as exc:
        await scrapex.navigator_screenshot(FakeSettings(), "task-1", "obs_abc123")
    assert exc.value.code == "navigator_observation_mismatch"


@pytest.mark.asyncio
async def test_capture_carries_the_review_and_reads_back_the_text_sidecar(monkeypatch):
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": "success",
                "saved": True,
                "already_present": False,
                "task_id": "task-1",
                "provider": "alldata",
                "relative_path": "2025/Kia/K4/Front Radar (ADAS) - Adjustment ALLDATA 20260913-120000.pdf",
                "source_sidecar": "2025/Kia/K4/Front Radar (ADAS) - Adjustment ALLDATA 20260913-120000.source.json",
                "text_sidecar": "2025/Kia/K4/Front Radar (ADAS) - Adjustment ALLDATA 20260913-120000.text.txt",
                "sha256": "f" * 64,
                "extracted_text_sha256": "a" * 64,
                "capture_method": "rendered_page_images",
            },
        )

    _install_transport(monkeypatch, handler)
    review = {"classification": "ACTUAL_PROCEDURE", "decision": "ACCEPT", "confidence": 0.9}
    result = await scrapex.navigator_capture(
        FakeSettings(), "task-1", semantic_review=review, objective={"objective": "front radar calibration"}
    )

    assert bodies[0]["semantic_review"] == review
    assert bodies[0]["objective"] == {"objective": "front radar calibration"}
    assert result["status"] == "captured" and result["verified"] is True
    assert result["data"]["text_sidecar"].endswith(".text.txt")
    assert result["data"]["capture_method"] == "rendered_page_images"


@pytest.mark.asyncio
async def test_a_malformed_observation_id_or_mark_never_reaches_scrapex(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("a malformed identifier must be refused locally")

    _install_transport(monkeypatch, handler)
    bad_observation = await scrapex.navigator(
        FakeSettings(),
        {"action": "click_mark", "task_id": "task-1", "mark": 21, "observation_id": "obs abc/../"},
    )
    bad_mark = await scrapex.navigator(
        FakeSettings(),
        {"action": "click_mark", "task_id": "task-1", "mark": 0, "observation_id": "obs_a"},
    )
    assert bad_observation["status"] == "invalid_request"
    assert bad_mark["status"] == "invalid_request"
