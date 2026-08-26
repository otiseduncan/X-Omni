from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

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


def _assert_no_credentials(value: Any) -> None:
    serialized = json.dumps(value, sort_keys=True).casefold()
    for forbidden in (
        "top-secret",
        "hunter2",
        "bearer abc.def",
        '"password"',
        '"service_token"',
        '"authorization"',
        '"cookie"',
    ):
        assert forbidden not in serialized


def _completed_item(batch_id: str, ro_number: str) -> dict[str, Any]:
    source_url = "https://opus.adasmap.com/details/9900001"
    inspection_id = "inspection-9900001"
    return {
        "id": "item-9900001",
        "batch_id": batch_id,
        "ro_number": ro_number,
        "adas_map_contract_version": 1,
        "adas_map_state": "adas_map_complete",
        "adas_map_requirements_proven": 1,
        "adas_map_inspection_id": inspection_id,
        "adas_map_source_url": source_url,
        "adas_map_checked_at": "2026-08-25T20:00:00Z",
        "adas_map_requirements_json": json.dumps(
            [{"calibration_type": "Blind Spot Monitor"}]
        ),
        "adas_map_raw_result_json": json.dumps(
            {
                "success": True,
                "status": "complete",
                "ro_number": ro_number,
                "inspection_id": inspection_id,
                "source_url": source_url,
                "requirements_proven": True,
                "explicit_no_calibration": False,
                "row_binding_confirmed": True,
                "modal_inspection_confirmed": True,
                "required_region_confirmed": True,
                "modal_runtime_id": "42.9900001",
                "requirement_records": [
                    {
                        "label": "Blind Spot Monitor",
                        "source": "adas_map_required_list_item",
                        "source_context": "selected_required_modal",
                        "source_context_runtime_id": "42.9900001",
                        "source_control_class": "btn btn-link custom-link",
                    }
                ],
            }
        ),
        "ciq_reconciliation_state": "complete",
        "ciq_reconciliation_json": json.dumps(
            {"verified": True, "snapshot_verified": True, "receipt_count": 1}
        ),
    }


@pytest.mark.asyncio
async def test_status_reports_verified_authentication_required_without_credentials(
    monkeypatch,
):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url == httpx.URL("http://127.0.0.1:8125/api/health")
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(
            200,
            json={
                "ok": True,
                "service": "ScrapeX",
                "version": "0.5.0",
                "ciq": {
                    "reachable": True,
                    "authorized": True,
                    "service_token": "top-secret",
                },
                "adas_map": {
                    "active": True,
                    "authenticated": False,
                    "password": "hunter2",
                },
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.status(FakeSettings(), {})

    assert len(requests) == 1
    assert result["status"] == "authentication_required"
    assert result["authentication_required"] is True
    assert result["success"] is False
    assert result["executed"] is True
    assert result["verified"] is True
    _assert_no_credentials(result)


@pytest.mark.asyncio
async def test_status_rejects_non_loopback_configuration_before_network(monkeypatch):
    def fail_if_called(**_kwargs):
        raise AssertionError("unsafe endpoint must not reach the network")

    monkeypatch.setattr(scrapex.httpx, "AsyncClient", fail_if_called)
    result = await scrapex.status(FakeSettings("https://scrapex.example.com:8125"), {})

    assert result["status"] == "configuration_error"
    assert result["executed"] is False
    assert "loopback" in result["error"]["message"].casefold()


@pytest.mark.asyncio
async def test_read_batch_item_passes_through_structured_canonical_provenance(monkeypatch):
    raw_result = {
        "success": True,
        "status": "complete",
        "inspection_id": "inspection-9",
        "source_url": "https://opus.adasmap.com/details/9",
        "row_binding_confirmed": True,
        "modal_inspection_confirmed": True,
        "required_region_confirmed": True,
        "modal_runtime_id": "42.9",
        "requirement_records": [
            {
                "label": "Blind Spot Monitor",
                "source": "adas_map_required_list_item",
                "source_context": "selected_required_modal",
                "source_context_runtime_id": "42.9",
                "source_control_class": "btn btn-link custom-link",
            }
        ],
        "authorization": "Bearer abc.def",
    }
    reconciliation = {
        "verified": True,
        "snapshot_verified": True,
        "receipt_count": 1,
        "cookie": "top-secret",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/batches/batch-9"
        return httpx.Response(
            200,
            json={
                "id": "batch-9",
                "name": "weekly",
                "state": "paused",
                "readiness": {"ready": True, "adas_map_complete": 1, "total": 1},
                "items": [
                    {
                        "id": "item-9",
                        "ro_number": "9000000009",
                        "adas_map_contract_version": 1,
                        "adas_map_state": "adas_map_complete",
                        "adas_map_requirements_proven": 1,
                        "adas_map_inspection_id": "inspection-9",
                        "adas_map_source_url": "https://opus.adasmap.com/details/9",
                        "adas_map_checked_at": "2026-08-25T20:00:00Z",
                        "adas_map_requirements_json": json.dumps(
                            [{"calibration_type": "Blind Spot Monitor"}]
                        ),
                        "adas_map_raw_result_json": json.dumps(raw_result),
                        "ciq_reconciliation_state": "complete",
                        "ciq_reconciliation_json": json.dumps(reconciliation),
                    }
                ],
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.read(
        FakeSettings(),
        {"action": "batch_item", "batch_id": "batch-9", "ro_number": "9000000009"},
    )

    assert result["status"] == "verified"
    provenance = result["data"]["provenance"]
    assert provenance["contract_version"] == 1
    assert provenance["requirements_proven"] is True
    assert provenance["inspection_id"] == "inspection-9"
    assert provenance["source_url"] == "https://opus.adasmap.com/details/9"
    assert provenance["raw_result"]["modal_runtime_id"] == "42.9"
    assert provenance["raw_result"]["requirement_records"][0]["label"] == (
        "Blind Spot Monitor"
    )
    assert provenance["ciq_reconciliation"]["receipt_count"] == 1
    _assert_no_credentials(result)


@pytest.mark.asyncio
async def test_read_batch_item_rejects_wrong_returned_batch_resource(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "stale-batch",
                "items": [{"id": "item-1", "ro_number": "9000000009"}],
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.read(
        FakeSettings(),
        {"action": "batch_item", "batch_id": "batch-9", "ro_number": "9000000009"},
    )

    assert result["status"] == "invalid_response"
    assert result["success"] is False
    assert result["verified"] is False
    assert result["may_have_executed"] is False
    assert result["error"]["contract_code"] == "batch_mismatch"


@pytest.mark.asyncio
async def test_read_preview_is_structured_and_non_mutating(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/ciq/preview"
        assert json.loads(request.content) == {
            "phases": ["5", "6"],
            "shop": "Macon",
            "source_scope": "active",
        }
        return httpx.Response(
            200,
            json={"count": 2, "phases": ["5", "6"], "vehicles": []},
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.read(
        FakeSettings(),
        {
            "action": "preview_ciq_queue",
            "phases": ["5", "6", "5"],
            "shop": "Macon",
        },
    )

    assert result["status"] == "verified"
    assert result["data"]["count"] == 2
    assert result["executed"] is True


@pytest.mark.asyncio
async def test_start_batch_stops_truthfully_when_authentication_is_required(monkeypatch):
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/adas-map/status":
            return httpx.Response(
                200,
                json={"active": True, "authenticated": False, "title": "ADAS Map"},
            )
        raise AssertionError("start must not run before interactive authentication")

    _install_transport(monkeypatch, handler)
    result = await scrapex.adas_map(
        FakeSettings(), {"action": "start_batch", "batch_id": "batch-1"}
    )

    assert requests == [("GET", "/api/adas-map/status")]
    assert result["status"] == "authentication_required"
    assert result["executed"] is False
    assert result["work_complete"] is False
    assert result["requires_human"] is True


@pytest.mark.asyncio
async def test_create_exact_batch_uses_only_bounded_structured_fields(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/batches/from-ciq/exact"
        assert "authorization" not in request.headers
        assert json.loads(request.content) == {
            "name": "Tuesday ADAS Map",
            "ro_numbers": ["9001", "9002"],
            "source_scope": "all",
        }
        return httpx.Response(
            200,
            json={
                "id": "batch-2",
                "state": "pending",
                "requested_ro_numbers": ["9001", "9002"],
                "source_scope": "all",
                "items": [
                    {"id": "item-1", "batch_id": "batch-2", "ro_number": "9001"},
                    {"id": "item-2", "batch_id": "batch-2", "ro_number": "9002"},
                ],
                "readiness": {"ready": False, "total": 2, "adas_map_unresolved": 2},
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.adas_map(
        FakeSettings(),
        {
            "action": "create_exact_batch",
            "name": "Tuesday ADAS Map",
            "ro_numbers": ["9001", "9002", "9001"],
        },
    )

    assert result["status"] == "queued"
    assert result["success"] is True
    assert result["executed"] is True
    assert result["verified"] is True
    assert result["work_complete"] is False


@pytest.mark.parametrize(
    ("variant", "contract_code"),
    [
        ("missing_id", "malformed_identifier"),
        ("wrong_ros", "requested_ro_contract_mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_create_exact_2xx_requires_exact_batch_and_ro_contract(
    monkeypatch,
    variant: str,
    contract_code: str,
):
    payload = {
        "id": "batch-exact",
        "requested_ro_numbers": ["9101", "9102"],
        "source_scope": "all",
        "items": [
            {"id": "item-1", "batch_id": "batch-exact", "ro_number": "9101"},
            {"id": "item-2", "batch_id": "batch-exact", "ro_number": "9102"},
        ],
        "readiness": {"ready": False},
    }
    if variant == "missing_id":
        payload.pop("id")
    else:
        payload["requested_ro_numbers"] = ["stale-ro"]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    _install_transport(monkeypatch, handler)
    result = await scrapex.adas_map(
        FakeSettings(),
        {"action": "create_exact_batch", "ro_numbers": ["9101", "9102"]},
    )

    assert result["status"] == "indeterminate"
    assert result["success"] is False
    assert result["executed"] is False
    assert result["verified"] is False
    assert result["may_have_executed"] is True
    assert result["indeterminate"] is True
    assert result["retryable"] is False
    assert result["error"]["contract_code"] == contract_code


@pytest.mark.asyncio
async def test_create_phase_2xx_requires_requested_phase_shop_and_scope_contract(
    monkeypatch,
):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "batch-phase",
                "phases": ["8"],
                "shop": "Macon",
                "source_scope": "active",
                "items": [
                    {
                        "id": "item-phase",
                        "batch_id": "batch-phase",
                        "ro_number": "9201",
                    }
                ],
                "readiness": {"ready": False},
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.adas_map(
        FakeSettings(),
        {
            "action": "create_phase_batch",
            "phases": ["5", "6"],
            "shop": "Macon",
            "source_scope": "active",
        },
    )

    assert result["status"] == "indeterminate"
    assert result["may_have_executed"] is True
    assert result["retryable"] is False
    assert result["error"]["contract_code"] == "requested_phase_contract_mismatch"


@pytest.mark.asyncio
async def test_create_phase_verified_success_is_bound_to_returned_batch(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "batch-phase-ok",
                "phases": ["5", "6"],
                "shop": "Macon",
                "source_scope": "active",
                "items": [
                    {
                        "id": "item-phase-ok",
                        "batch_id": "batch-phase-ok",
                        "ro_number": "9202",
                    }
                ],
                "readiness": {"ready": False, "total": 1},
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.adas_map(
        FakeSettings(),
        {
            "action": "create_phase_batch",
            "phases": ["5", "6"],
            "shop": "Macon",
        },
    )

    assert result["status"] == "queued"
    assert result["success"] is True
    assert result["verified"] is True
    assert result["data"]["id"] == "batch-phase-ok"


@pytest.mark.asyncio
async def test_process_one_completed_success_is_bound_to_exact_item_and_provenance(
    monkeypatch,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/adas-map/status":
            return httpx.Response(200, json={"active": True, "authenticated": True})
        return httpx.Response(
            200,
            json={
                "attempted": True,
                "completed": True,
                "status": "completed",
                "batch_id": "batch-process",
                "ro_number": "9301",
                "item": _completed_item("batch-process", "9301"),
                "readiness": {"ready": True},
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.adas_map(
        FakeSettings(),
        {"action": "process_one", "batch_id": "batch-process", "ro_number": "9301"},
    )

    assert result["status"] == "completed"
    assert result["success"] is True
    assert result["executed"] is True
    assert result["verified"] is True
    assert result["work_complete"] is True
    assert result["data"]["provenance"]["inspection_id"] == "inspection-9900001"


@pytest.mark.parametrize(
    ("variant", "contract_code"),
    [
        ("wrong_outer_batch", "batch_mismatch"),
        ("wrong_item_ro", "item_ro_mismatch"),
        ("missing_provenance", "malformed_text"),
    ],
)
@pytest.mark.asyncio
async def test_process_one_mismatched_completed_2xx_is_indeterminate(
    monkeypatch,
    variant: str,
    contract_code: str,
):
    item = _completed_item("batch-process", "9301")
    payload = {
        "attempted": True,
        "completed": True,
        "status": "completed",
        "batch_id": "batch-process",
        "ro_number": "9301",
        "item": item,
        "readiness": {"ready": True},
    }
    if variant == "wrong_outer_batch":
        payload["batch_id"] = "stale-batch"
    elif variant == "wrong_item_ro":
        item["ro_number"] = "stale-ro"
    else:
        item["adas_map_source_url"] = None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/adas-map/status":
            return httpx.Response(200, json={"active": True, "authenticated": True})
        return httpx.Response(200, json=payload)

    _install_transport(monkeypatch, handler)
    result = await scrapex.adas_map(
        FakeSettings(),
        {"action": "process_one", "batch_id": "batch-process", "ro_number": "9301"},
    )

    assert result["status"] == "indeterminate"
    assert result["success"] is False
    assert result["verified"] is False
    assert result["may_have_executed"] is True
    assert result["retryable"] is False
    assert result["error"]["contract_code"] == contract_code


@pytest.mark.asyncio
async def test_start_acknowledgement_is_running_not_completed(monkeypatch):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/api/adas-map/status":
            return httpx.Response(200, json={"active": True, "authenticated": True})
        assert request.url.path == "/api/batches/batch-3/adas-map/start"
        return httpx.Response(
            200,
            json={
                "started": True,
                "stage": "adas_map",
                "batch": {
                    "id": "batch-3",
                    "state": "running_adas_map",
                    "readiness": {"ready": False, "adas_map_unresolved": 49},
                },
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.adas_map(
        FakeSettings(), {"action": "start_batch", "batch_id": "batch-3"}
    )

    assert requests == [
        "/api/adas-map/status",
        "/api/batches/batch-3/adas-map/start",
    ]
    assert result["status"] == "running"
    assert result["success"] is True
    assert result["executed"] is True
    assert result["verified"] is True
    assert result["work_complete"] is False


@pytest.mark.parametrize("action", ["start_batch", "pause_batch"])
@pytest.mark.asyncio
async def test_start_and_pause_2xx_are_bound_to_requested_batch(monkeypatch, action: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/adas-map/status":
            return httpx.Response(200, json={"active": True, "authenticated": True})
        if action == "start_batch":
            return httpx.Response(
                200,
                json={
                    "started": True,
                    "stage": "adas_map",
                    "batch": {"id": "stale-batch", "readiness": {"ready": False}},
                },
            )
        return httpx.Response(
            200,
            json={
                "paused": True,
                "stage": "adas_map",
                "batch": {"id": "stale-batch"},
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.adas_map(
        FakeSettings(), {"action": action, "batch_id": "batch-control"}
    )

    assert result["status"] == "indeterminate"
    assert result["success"] is False
    assert result["may_have_executed"] is True
    assert result["retryable"] is False
    assert result["error"]["contract_code"] == "batch_mismatch"


@pytest.mark.asyncio
async def test_pause_verified_success_is_bound_to_returned_batch(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "paused": True,
                "stage": "adas_map",
                "batch": {
                    "id": "batch-pause",
                    "state": "paused",
                    "readiness": {"ready": False, "total": 4},
                },
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.adas_map(
        FakeSettings(), {"action": "pause_batch", "batch_id": "batch-pause"}
    )

    assert result["status"] == "paused"
    assert result["success"] is True
    assert result["executed"] is True
    assert result["verified"] is True


@pytest.mark.asyncio
async def test_authentication_race_is_normalized_instead_of_reported_as_failure(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/adas-map/status":
            return httpx.Response(200, json={"active": True, "authenticated": True})
        return httpx.Response(
            409,
            json={
                "detail": "ADAS Map is not authenticated in the managed work Chrome window."
            },
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.adas_map(
        FakeSettings(),
        {"action": "process_one", "batch_id": "batch-4", "ro_number": "9004"},
    )

    assert result["status"] == "authentication_required"
    assert result["authentication_required"] is True
    assert result["executed"] is False
    assert result["requires_human"] is True


@pytest.mark.asyncio
async def test_mutation_timeout_is_indeterminate_and_not_reported_as_success(monkeypatch):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("simulated timeout", request=request)

    _install_transport(monkeypatch, handler)
    result = await scrapex.adas_map(
        FakeSettings(),
        {"action": "create_exact_batch", "ro_numbers": ["9006"]},
    )

    assert calls == 1
    assert result["status"] == "indeterminate"
    assert result["success"] is False
    assert result["executed"] is False
    assert result["verified"] is False
    assert result["may_have_executed"] is True
    assert result["indeterminate"] is True
    assert result["retryable"] is False


@pytest.mark.asyncio
async def test_mutation_remote_5xx_is_indeterminate_and_never_retry_safe(monkeypatch):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"detail": "worker response was lost"})

    _install_transport(monkeypatch, handler)
    result = await scrapex.adas_map(
        FakeSettings(),
        {"action": "create_exact_batch", "ro_numbers": ["9007"]},
    )

    assert calls == 1
    assert result["status"] == "indeterminate"
    assert result["success"] is False
    assert result["executed"] is False
    assert result["verified"] is False
    assert result["may_have_executed"] is True
    assert result["indeterminate"] is True
    assert result["retryable"] is False
    assert result["http_status"] == 503


@pytest.mark.asyncio
async def test_mutating_invalid_json_2xx_propagates_commit_ambiguity(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    _install_transport(monkeypatch, handler)
    result = await scrapex.adas_map(
        FakeSettings(), {"action": "create_exact_batch", "ro_numbers": ["9401"]}
    )

    assert result["status"] == "indeterminate"
    assert result["error"]["code"] == "indeterminate"
    assert result["error"]["transport_code"] == "invalid_response"
    assert result["may_have_executed"] is True
    assert result["retryable"] is False


@pytest.mark.asyncio
async def test_read_invalid_json_2xx_remains_definitive_invalid_response(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    _install_transport(monkeypatch, handler)
    result = await scrapex.read(FakeSettings(), {"action": "list_batches"})

    assert result["status"] == "invalid_response"
    assert result["may_have_executed"] is False
    assert result["retryable"] is False


@pytest.mark.asyncio
async def test_mutating_oversized_2xx_propagates_commit_ambiguity(monkeypatch):
    monkeypatch.setattr(scrapex, "MAX_RESPONSE_BYTES", 32)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "batch-big", "padding": "x" * 128})

    _install_transport(monkeypatch, handler)
    result = await scrapex.adas_map(
        FakeSettings(), {"action": "create_exact_batch", "ro_numbers": ["9501"]}
    )

    assert result["status"] == "indeterminate"
    assert result["may_have_executed"] is True
    assert result["retryable"] is False
    assert result["error"]["message"] == "ScrapeX returned too much data."
    assert result["error"]["transport_code"] == "response_too_large"


@pytest.mark.asyncio
async def test_structured_actions_reject_irrelevant_or_secret_arguments_before_network(
    monkeypatch,
):
    def fail_if_called(**_kwargs):
        raise AssertionError("invalid structured input must not reach ScrapeX")

    monkeypatch.setattr(scrapex.httpx, "AsyncClient", fail_if_called)
    result = await scrapex.adas_map(
        FakeSettings(),
        {
            "action": "start_batch",
            "batch_id": "batch-5",
            "password": "hunter2",
        },
    )

    assert result["status"] == "invalid_request"
    assert result["executed"] is False
    schema_text = json.dumps(scrapex.SCRAPEX_TOOL_SCHEMAS).casefold()
    assert "password" not in schema_text
    assert "credential" not in schema_text
    assert "base_url" not in schema_text
