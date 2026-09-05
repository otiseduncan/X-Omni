from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from core.config import ROOT
from core.orchestrator.loop import (
    Orchestrator,
    tool_result_json_for_model,
    tool_result_visible_to_model,
)
from core.services import scrapex
from core.state.db import Store
from core.tools.registry import (
    Registry,
    TOOL_SCHEMAS,
    ToolBlocked,
    scrapex_apply_new_quarantine,
    scrapex_evidence_from_result,
    validate_scrapex_batch_binding,
)


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
        "adas_map_contract_version": 3,
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
            {
                "verified": True,
                "snapshot_verified": True,
                "receipt_count": 1,
                "adas_map_attachment": {
                    "attached": True,
                    "document_id": "doc-adas-map",
                    "semantic_type": "ADAS_MAP_REPORT",
                },
            }
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
        "adas_map_attachment": {
            "attached": True,
            "document_id": "doc-9",
            "semantic_type": "ADAS_MAP_REPORT",
        },
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
                        "adas_map_contract_version": 3,
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
    assert provenance["contract_version"] == 3
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
async def test_read_preview_rejects_ro_number_before_network(monkeypatch):
    def fail_if_called(**_kwargs):
        raise AssertionError("irrelevant preview fields must not reach ScrapeX")

    monkeypatch.setattr(scrapex.httpx, "AsyncClient", fail_if_called)
    result = await scrapex.read(
        FakeSettings(),
        {
            "action": "preview_ciq_queue",
            "phases": ["5"],
            "ro_number": "9000000009",
        },
    )

    assert result["status"] == "invalid_request"
    assert result["executed"] is False
    assert "ro_number" in result["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected_path"),
    [
        ({"action": "list_batches"}, "/api/batches"),
        (
            {"action": "batch_summary", "batch_id": "batch-9"},
            "/api/batches/batch-9/summary",
        ),
        (
            {"action": "batch_exceptions", "batch_id": "batch-9"},
            "/api/batches/batch-9/exceptions",
        ),
    ],
)
async def test_read_list_and_batch_views_follow_production_paths(
    monkeypatch, arguments, expected_path
):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == expected_path
        return httpx.Response(200, json={"ok": True})

    _install_transport(monkeypatch, handler)
    result = await scrapex.read(FakeSettings(), arguments)

    assert result["status"] == "verified"
    assert result["success"] is True


def test_read_schema_uses_complete_action_specific_branches() -> None:
    parameters = scrapex.SCRAPEX_READ_SCHEMA["parameters"]
    Draft202012Validator.check_schema(parameters)
    validator = Draft202012Validator(parameters)

    valid_arguments = [
        {"action": "list_batches"},
        {"action": "batch_summary", "batch_id": "batch-9"},
        {"action": "batch_exceptions", "batch_id": "batch-9"},
        {
            "action": "batch_item",
            "batch_id": "batch-9",
            "ro_number": "9000000009",
        },
        {
            "action": "preview_ciq_queue",
            "phases": ["5", "6"],
            "shop": "Macon",
            "source_scope": "active",
        },
    ]
    for arguments in valid_arguments:
        assert validator.is_valid(arguments), arguments

    invalid_arguments = [
        {
            "action": "preview_ciq_queue",
            "phases": ["5"],
            "ro_number": "9000000009",
        },
        {"action": "preview_ciq_queue"},
        {
            "action": "batch_summary",
            "batch_id": "batch-9",
            "ro_number": "9000000009",
        },
        {"action": "batch_item", "batch_id": "batch-9"},
        {"action": "list_batches", "batch_id": "batch-9"},
    ]
    for arguments in invalid_arguments:
        assert not validator.is_valid(arguments), arguments

    branches = parameters["oneOf"]
    assert len(branches) == len(scrapex.READ_ACTIONS)
    assert all(branch["additionalProperties"] is False for branch in branches)
    assert {branch["properties"]["action"]["const"] for branch in branches} == (
        scrapex.READ_ACTIONS
    )
    exposed_fields = {
        branch["properties"]["action"]["const"]: set(branch["properties"])
        for branch in branches
    }
    assert exposed_fields == {
        "list_batches": {"action"},
        "batch_summary": {"action", "batch_id"},
        "batch_exceptions": {"action", "batch_id"},
        "batch_item": {"action", "batch_id", "ro_number"},
        "preview_ciq_queue": {"action", "phases", "shop", "source_scope"},
    }
    action_descriptions = {
        branch["properties"]["action"]["const"]: branch["properties"]["action"].get(
            "description", ""
        )
        for branch in branches
    }
    assert "stored ScrapeX batches" in action_descriptions["list_batches"]
    assert "existing ADAS Map evidence" in action_descriptions["list_batches"]
    preview_description = action_descriptions["preview_ciq_queue"]
    assert "Calibration IQ candidate work" in preview_description
    assert "not stored ADAS Map evidence" in preview_description
    assert "does not provide an existing ScrapeX batch or batch item" in (
        preview_description
    )
    assert "list_batches discovers existing evidence" in preview_description


def test_adas_map_schema_uses_complete_action_specific_branches() -> None:
    parameters = scrapex.SCRAPEX_ADAS_MAP_SCHEMA["parameters"]
    Draft202012Validator.check_schema(parameters)
    validator = Draft202012Validator(parameters)

    valid_arguments = [
        {"action": "open_authentication"},
        {"action": "acquire_exact", "ro_number": "9000000009"},
        {"action": "create_exact_batch", "ro_numbers": ["9000000009"]},
        {"action": "create_phase_batch", "phases": ["5", "6"]},
        {
            "action": "process_one",
            "batch_id": "batch-9",
            "ro_number": "9000000009",
        },
        {"action": "start_batch", "batch_id": "batch-9"},
        {"action": "pause_batch", "batch_id": "batch-9"},
    ]
    for arguments in valid_arguments:
        assert validator.is_valid(arguments), arguments

    invalid_arguments = [
        {"action": "open_authentication", "batch_id": "batch-9"},
        {"action": "acquire_exact"},
        {"action": "acquire_exact", "ro_number": "9000000009", "batch_id": "batch-9"},
        {
            "action": "create_exact_batch",
            "ro_numbers": ["9000000009"],
            "batch_id": "batch-9",
        },
        {"action": "create_phase_batch", "phases": ["5"], "ro_numbers": ["9"]},
        {"action": "process_one", "batch_id": "batch-9"},
        {
            "action": "process_one",
            "batch_id": "batch-9",
            "ro_number": "9000000009",
            "phases": ["5"],
        },
        {"action": "start_batch"},
        {"action": "pause_batch", "batch_id": "batch-9", "ro_number": "9"},
    ]
    for arguments in invalid_arguments:
        assert not validator.is_valid(arguments), arguments

    branches = parameters["oneOf"]
    assert len(branches) == len(scrapex.ADAS_MAP_ACTIONS)
    assert all(branch["additionalProperties"] is False for branch in branches)
    assert {branch["properties"]["action"]["const"] for branch in branches} == (
        scrapex.ADAS_MAP_ACTIONS
    )
    assert "allOf" not in parameters


@pytest.mark.asyncio
async def test_acquire_exact_composite_saves_attaches_and_returns_chat_document(
    tmp_path, monkeypatch
):
    ro_number = "2400911578"
    pdf = tmp_path / "ADAS Map" / ro_number / f"{ro_number} ADAS Map.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\n" + (b"0" * 512))

    async def base_map(_settings, args):
        assert args == {
            "action": "acquire_exact",
            "ro_number": ro_number,
            "source_scope": "active",
        }
        return {
            "service": "ScrapeX",
            "action": "acquire_exact",
            "status": "completed",
            "success": True,
            "executed": True,
            "verified": True,
            "work_complete": True,
            "data": {"readiness": {"ready": True}},
        }

    class Adas:
        def page_count(self, path):
            assert path == pdf
            return 3

    from core.services import calibration_iq as ciq

    async def ro_read(_settings, args):
        assert args == {"ro_number": ro_number}
        source_uri = (
            "adas-si:///ADAS%20Map/2400911578/"
            "2400911578%20ADAS%20Map.pdf"
        )
        return {
            "status": "verified",
            "repair_order": {"id": "ro-internal-1", "RO": ro_number},
            "raw": {
                "documents": [
                    {
                        "id": "doc-1",
                        "version": 1,
                        "title": "2400911578 ADAS Map",
                        "document_type": "adas_map_report",
                        "semantic_type": "ADAS_MAP_REPORT",
                        "source_name": "2400911578 ADAS Map.pdf",
                        "source_uri": source_uri,
                        "download_url": (
                            "/api/calibration-iq/documents/doc-1/download"
                        ),
                    }
                ],
                "research": {
                    "id": "research-1",
                    "state": "research_in_progress",
                    "version": 4,
                },
            },
        }

    async def should_not_mutate(*_args, **_kwargs):
        raise AssertionError(
            "X must verify ScrapeX's CIQ handoff, not perform a second attachment"
        )

    monkeypatch.setattr(scrapex, "adas_map", base_map)
    monkeypatch.setattr(ciq, "get_repair_order", ro_read)
    monkeypatch.setattr(ciq, "operator_execute", should_not_mutate)

    adas = Adas()
    settings = SimpleNamespace(adas_si_root=tmp_path)
    result = await scrapex.adas_map_with_ciq_attachment(
        settings,
        adas,
        {
            "action": "acquire_exact",
            "ro_number": ro_number,
            "source_scope": "active",
            scrapex._INVOCATION_CONTEXT_KEY: {
                "conversation_id": 12,
                "message_id": 34,
                "tool_call_id": "map-call",
                "user_id": "owner",
                "role": "owner",
            },
        },
    )

    assert result["status"] == "completed"
    assert result["success"] is True
    assert result["work_complete"] is True
    assert result["local_report"]["verified"] is True
    assert result["chat_document"]["pages_total"] == 3
    assert result["ciq_attachment"]["attached"] is True
    assert result["ciq_attachment"]["document_id"] == "doc-1"
    assert result["ciq_attachment"]["semantic_type"] == "ADAS_MAP_REPORT"
    assert result["ciq_attachment"]["research_state"] == "research_in_progress"


@pytest.mark.asyncio
async def test_acquire_exact_refuses_complete_when_ciq_research_is_still_required(
    tmp_path, monkeypatch
):
    ro_number = "2400911578"
    pdf = tmp_path / "ADAS Map" / ro_number / f"{ro_number} ADAS Map.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\n" + (b"0" * 512))

    async def base_map(_settings, _args):
        return {
            "status": "completed",
            "success": True,
            "verified": True,
            "work_complete": True,
        }

    class Adas:
        def page_count(self, _path):
            return 1

    from core.services import calibration_iq as ciq

    async def ro_read(_settings, _args):
        return {
            "status": "verified",
            "repair_order": {"id": "ro-internal-1"},
            "raw": {
                "documents": [
                    {
                        "id": "doc-1",
                        "document_type": "adas_map_report",
                        "semantic_type": "ADAS_MAP_REPORT",
                        "source_name": f"{ro_number} ADAS Map.pdf",
                    }
                ],
                "research": {
                    "id": "research-1",
                    "state": "research_required",
                    "version": 2,
                },
            },
        }

    monkeypatch.setattr(scrapex, "adas_map", base_map)
    monkeypatch.setattr(ciq, "get_repair_order", ro_read)

    result = await scrapex.adas_map_with_ciq_attachment(
        SimpleNamespace(adas_si_root=tmp_path),
        Adas(),
        {"action": "acquire_exact", "ro_number": ro_number},
    )

    assert result["status"] == "attachment_failed"
    assert result["success"] is False
    assert result["work_complete"] is False
    assert "research required" in result["message"].casefold()


@pytest.mark.asyncio
async def test_acquire_exact_composite_refuses_ready_when_pdf_is_not_on_disk(
    tmp_path, monkeypatch
):
    ro_number = "2400911578"

    async def base_map(_settings, _args):
        return {
            "service": "ScrapeX",
            "action": "acquire_exact",
            "status": "completed",
            "success": True,
            "executed": True,
            "verified": True,
            "work_complete": True,
            "data": {},
        }

    from core.services import calibration_iq as ciq

    async def should_not_resolve(*_args, **_kwargs):
        raise AssertionError(
            "CIQ attachment must not run without the canonical PDF"
        )

    monkeypatch.setattr(scrapex, "adas_map", base_map)
    monkeypatch.setattr(ciq, "get_repair_order", should_not_resolve)

    result = await scrapex.adas_map_with_ciq_attachment(
        SimpleNamespace(adas_si_root=tmp_path),
        SimpleNamespace(),
        {
            "action": "acquire_exact",
            "ro_number": ro_number,
            scrapex._INVOCATION_CONTEXT_KEY: {
                "conversation_id": 12,
                "message_id": 34,
                "tool_call_id": "map-call",
            },
        },
    )

    assert result["status"] == "attachment_failed"
    assert result["success"] is False
    assert result["verified"] is False
    assert result["work_complete"] is False
    assert result["local_report"]["verified"] is False


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
async def test_open_authentication_is_a_parameterless_provider_setup_handoff(
    monkeypatch,
):
    requests: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={"active": True, "authenticated": False, "title": "ADAS Map"},
        )

    _install_transport(monkeypatch, handler)
    result = await scrapex.adas_map(FakeSettings(), {"action": "open_authentication"})

    assert requests == [("POST", "/api/adas-map/open", {})]
    assert result["status"] == "authentication_required"
    assert result["executed"] is True
    assert result["verified"] is False
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
    assert result["data"]["id"] == "batch-2"
    assert "evidence_id" not in result


@pytest.mark.asyncio
async def test_acquire_exact_runs_one_ro_through_terminal_processing(monkeypatch):
    requests: list[tuple[str, str]] = []

    async def start_native(_settings):
        return {
            "status": "ready",
            "success": True,
            "executed": False,
            "verified": True,
        }

    monkeypatch.setattr(scrapex, "start_native", start_native)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/batches/from-ciq/exact":
            assert json.loads(request.content) == {
                "name": "RO 9701 ADAS Map",
                "ro_numbers": ["9701"],
                "source_scope": "active",
            }
            return httpx.Response(
                200,
                json={
                    "id": "batch-9701",
                    "state": "pending",
                    "requested_ro_numbers": ["9701"],
                    "source_scope": "active",
                    "items": [
                        {
                            "id": "item-9701",
                            "batch_id": "batch-9701",
                            "ro_number": "9701",
                        }
                    ],
                    "readiness": {
                        "ready": False,
                        "total": 1,
                        "adas_map_unresolved": 1,
                    },
                },
            )
        if request.url.path == "/api/adas-map/status":
            return httpx.Response(
                200,
                json={"active": True, "authenticated": True},
            )
        if request.url.path == "/api/batches/batch-9701/adas-map/process-one/9701":
            return httpx.Response(
                200,
                json={
                    "attempted": True,
                    "completed": True,
                    "status": "completed",
                    "batch_id": "batch-9701",
                    "ro_number": "9701",
                    "item": _completed_item("batch-9701", "9701"),
                    "readiness": {"ready": True},
                },
            )
        raise AssertionError(request.url.path)

    _install_transport(monkeypatch, handler)
    result = await scrapex.adas_map(
        FakeSettings(),
        {
            "action": "acquire_exact",
            "ro_number": "9701",
            "source_scope": "active",
        },
    )

    assert result["status"] == "completed"
    assert result["success"] is True
    assert result["verified"] is True
    assert result["work_complete"] is True
    assert result["exact_batch_id"] == "batch-9701"
    assert result["requested_ro_number"] == "9701"
    assert requests == [
        ("POST", "/api/batches/from-ciq/exact"),
        ("GET", "/api/adas-map/status"),
        ("POST", "/api/batches/batch-9701/adas-map/process-one/9701"),
    ]


@pytest.mark.asyncio
async def test_process_one_copies_the_exact_created_batch_data_id(monkeypatch):
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/batches/from-ciq/exact":
            return httpx.Response(
                200,
                json={
                    "id": "batch-resource-42",
                    "state": "pending",
                    "requested_ro_numbers": ["9601"],
                    "source_scope": "all",
                    "items": [
                        {
                            "id": "item-9601",
                            "batch_id": "batch-resource-42",
                            "ro_number": "9601",
                        }
                    ],
                    "readiness": {
                        "ready": False,
                        "total": 1,
                        "adas_map_unresolved": 1,
                    },
                },
            )
        if request.url.path == "/api/adas-map/status":
            return httpx.Response(200, json={"active": True, "authenticated": True})
        assert request.url.path == (
            "/api/batches/batch-resource-42/adas-map/process-one/9601"
        )
        return httpx.Response(
            200,
            json={
                "attempted": True,
                "completed": True,
                "status": "completed",
                "batch_id": "batch-resource-42",
                "ro_number": "9601",
                "item": _completed_item("batch-resource-42", "9601"),
                "readiness": {"ready": True},
            },
        )

    _install_transport(monkeypatch, handler)
    created = await scrapex.adas_map(
        FakeSettings(),
        {"action": "create_exact_batch", "ro_numbers": ["9601"]},
    )
    authoritative_batch_id = created["data"]["id"]
    processed = await scrapex.adas_map(
        FakeSettings(),
        {
            "action": "process_one",
            "batch_id": authoritative_batch_id,
            "ro_number": "9601",
        },
    )

    assert authoritative_batch_id == "batch-resource-42"
    assert processed["status"] == "completed"
    assert processed["data"]["batch_id"] == authoritative_batch_id
    assert requests == [
        ("POST", "/api/batches/from-ciq/exact"),
        ("GET", "/api/adas-map/status"),
        ("POST", "/api/batches/batch-resource-42/adas-map/process-one/9601"),
    ]


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


def test_schemas_name_authoritative_batch_id_and_safe_provider_preflights() -> None:
    status_text = json.dumps(scrapex.SCRAPEX_STATUS_SCHEMA).casefold()
    adas_map_text = json.dumps(scrapex.SCRAPEX_ADAS_MAP_SCHEMA).casefold()
    read_text = json.dumps(scrapex.SCRAPEX_READ_SCHEMA).casefold()

    assert "safe, non-mutating provider preflight" in status_text
    assert "before acquisition or provider setup" in status_text
    assert "result.data.id" in adas_map_text
    assert "never copy evidence_id" in adas_map_text
    assert "parameterless browser-opening human/provider handoff" in adas_map_text
    assert "user explicitly requests provider setup" in adas_map_text
    assert "result.data.id, never evidence_id" in read_text


def _verified_scrapex_result(action: str, data: Any) -> dict[str, Any]:
    return {
        "service": "ScrapeX",
        "action": action,
        "status": "verified",
        "success": True,
        "executed": True,
        "verified": True,
        "data": data,
    }


def test_scrapex_evidence_is_minted_only_from_verified_structured_results() -> None:
    listed = scrapex_evidence_from_result(
        "scrapex_read",
        {"action": "list_batches"},
        _verified_scrapex_result(
            "list_batches",
            {"batches": [{"id": "batch-list-1"}, {"id": "batch-list-2"}]},
        ),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="list-call",
    )
    assert listed is not None
    assert listed.batch_ids == ("batch-list-1", "batch-list-2")
    assert listed.source_tool_call_ids == ("list-call",)

    created = scrapex_evidence_from_result(
        "scrapex_adas_map",
        {"action": "create_exact_batch", "ro_numbers": ["9000000009"]},
        _verified_scrapex_result(
            "create_exact_batch", {"id": "batch-created-3"}
        ),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="create-call",
        previous=listed,
    )
    assert created is not None
    assert created.batch_ids == (
        "batch-created-3",
        "batch-list-1",
        "batch-list-2",
    )
    assert created.source_tool_call_ids == ("create-call", "list-call")

    malformed = _verified_scrapex_result(
        "list_batches", {"batches": [{"id": "batch-ok"}, {"name": "missing-id"}]}
    )
    assert scrapex_evidence_from_result(
        "scrapex_read",
        {"action": "list_batches"},
        malformed,
        conversation_id=11,
        message_id=22,
        source_tool_call_id="bad-list",
    ) is None

    for unsafe_result in (
        {
            **_verified_scrapex_result(
                "create_exact_batch", {"id": "batch-untrusted"}
            ),
            "action": "create_phase_batch",
        },
        {
            **_verified_scrapex_result(
                "create_exact_batch", {"id": "batch-untrusted"}
            ),
            "verified": False,
        },
        {
            **_verified_scrapex_result(
                "create_exact_batch", {"id": "batch-untrusted"}
            ),
            "authentication_required": True,
        },
        {
            **_verified_scrapex_result(
                "create_exact_batch", {"id": "batch-untrusted"}
            ),
            "status": "indeterminate",
            "may_have_executed": True,
        },
    ):
        assert scrapex_evidence_from_result(
            "scrapex_adas_map",
            {"action": "create_exact_batch", "ro_numbers": ["9000000009"]},
            unsafe_result,
            conversation_id=11,
            message_id=22,
            source_tool_call_id="unsafe-call",
        ) is None


def test_bound_scrapex_results_can_preserve_but_never_mint_an_opaque_id() -> None:
    created = scrapex_evidence_from_result(
        "scrapex_adas_map",
        {"action": "create_exact_batch", "ro_numbers": ["9000000009"]},
        _verified_scrapex_result("create_exact_batch", {"id": "batch-created-3"}),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="create-call",
    )
    assert created is not None

    preserved = scrapex_evidence_from_result(
        "scrapex_read",
        {"action": "batch_summary", "batch_id": "batch-created-3"},
        _verified_scrapex_result(
            "batch_summary", {"batch_id": "batch-created-3", "items": []}
        ),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="summary-call",
        previous=created,
    )
    assert preserved is not None
    assert preserved.batch_ids == ("batch-created-3",)
    assert preserved.source_tool_call_ids == ("create-call", "summary-call")

    invented = scrapex_evidence_from_result(
        "scrapex_read",
        {"action": "batch_summary", "batch_id": "batch-invented"},
        _verified_scrapex_result(
            "batch_summary", {"batch_id": "batch-invented", "items": []}
        ),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="invented-call",
        previous=created,
    )
    assert invented == created

    stale = scrapex_evidence_from_result(
        "scrapex_read",
        {"action": "batch_summary", "batch_id": "batch-created-3"},
        _verified_scrapex_result(
            "batch_summary", {"batch_id": "batch-created-3", "items": []}
        ),
        conversation_id=11,
        message_id=23,
        source_tool_call_id="stale-call",
        previous=created,
    )
    assert stale is None


def test_indeterminate_id_bound_mutation_quarantines_only_that_batch_for_turn() -> None:
    evidence = scrapex_evidence_from_result(
        "scrapex_read",
        {"action": "list_batches"},
        _verified_scrapex_result(
            "list_batches",
            {"batches": [{"id": "batch-risk"}, {"id": "batch-safe"}]},
        ),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="list-call",
    )
    assert evidence is not None

    quarantined = scrapex_evidence_from_result(
        "scrapex_adas_map",
        {
            "action": "process_one",
            "batch_id": "batch-risk",
            "ro_number": "9000000009",
        },
        {
            "service": "ScrapeX",
            "action": "process_one",
            "status": "indeterminate",
            "success": False,
            "executed": False,
            "verified": False,
            "may_have_executed": True,
            "indeterminate": True,
            "retryable": False,
        },
        conversation_id=11,
        message_id=22,
        source_tool_call_id="ambiguous-process",
        previous=evidence,
    )
    assert quarantined is not None
    assert quarantined.batch_ids == ("batch-safe",)
    assert quarantined.quarantined_batch_ids == ("batch-risk",)

    with pytest.raises(ToolBlocked, match="automatic retry is forbidden"):
        validate_scrapex_batch_binding(
            "scrapex_adas_map",
            {"action": "start_batch", "batch_id": "batch-risk"},
            quarantined,
            conversation_id=11,
            message_id=22,
        )
    validate_scrapex_batch_binding(
        "scrapex_adas_map",
        {"action": "start_batch", "batch_id": "batch-safe"},
        quarantined,
        conversation_id=11,
        message_id=22,
    )

    relisted = scrapex_evidence_from_result(
        "scrapex_read",
        {"action": "list_batches"},
        _verified_scrapex_result(
            "list_batches", {"batches": [{"id": "batch-risk"}]}
        ),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="relist-call",
        previous=quarantined,
    )
    assert relisted == quarantined


def test_sibling_overlay_applies_only_quarantine_and_never_new_batch_ids() -> None:
    round_evidence = scrapex_evidence_from_result(
        "scrapex_read",
        {"action": "list_batches"},
        _verified_scrapex_result(
            "list_batches", {"batches": [{"id": "batch-risk"}, {"id": "batch-safe"}]}
        ),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="prior-list",
    )
    assert round_evidence is not None

    newly_created = scrapex_evidence_from_result(
        "scrapex_adas_map",
        {"action": "create_exact_batch", "ro_numbers": ["9000000009"]},
        _verified_scrapex_result("create_exact_batch", {"id": "batch-new"}),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="create-call",
        previous=round_evidence,
    )
    assert newly_created is not None
    assert scrapex_apply_new_quarantine(round_evidence, newly_created) == round_evidence

    quarantined = scrapex_evidence_from_result(
        "scrapex_adas_map",
        {"action": "start_batch", "batch_id": "batch-risk"},
        {
            "service": "ScrapeX",
            "action": "start_batch",
            "status": "indeterminate",
            "success": False,
            "executed": False,
            "verified": False,
            "may_have_executed": True,
            "retryable": False,
        },
        conversation_id=11,
        message_id=22,
        source_tool_call_id="ambiguous-start",
        previous=newly_created,
    )
    sibling_evidence = scrapex_apply_new_quarantine(round_evidence, quarantined)
    assert sibling_evidence is not None
    assert sibling_evidence.batch_ids == ("batch-safe",)
    assert sibling_evidence.quarantined_batch_ids == ("batch-risk",)
    assert "batch-new" not in sibling_evidence.batch_ids


def test_scrapex_evidence_never_exceeds_the_exact_model_visible_result() -> None:
    oversized = _verified_scrapex_result(
        "list_batches",
        {"batches": [{"id": f"batch-{index:04d}"} for index in range(800)]},
    )
    encoded = tool_result_json_for_model("scrapex_read", oversized)

    assert len(encoded) == 12_000
    with pytest.raises(json.JSONDecodeError):
        json.loads(encoded)
    assert tool_result_visible_to_model("scrapex_read", oversized) is None
    assert scrapex_evidence_from_result(
        "scrapex_read",
        {"action": "list_batches"},
        tool_result_visible_to_model("scrapex_read", oversized),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="oversized-list",
    ) is None


def test_scrapex_batch_binding_defers_new_evidence_until_the_next_model_round() -> None:
    round_evidence = None
    next_round_evidence = scrapex_evidence_from_result(
        "scrapex_adas_map",
        {"action": "create_exact_batch", "ro_numbers": ["9000000009"]},
        _verified_scrapex_result("create_exact_batch", {"id": "batch-created-3"}),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="create-call",
        previous=round_evidence,
    )
    arguments = {
        "action": "process_one",
        "batch_id": "batch-created-3",
        "ro_number": "9000000009",
    }

    with pytest.raises(ToolBlocked, match="verified same-turn"):
        validate_scrapex_batch_binding(
            "scrapex_adas_map",
            arguments,
            round_evidence,
            conversation_id=11,
            message_id=22,
        )
    validate_scrapex_batch_binding(
        "scrapex_adas_map",
        arguments,
        next_round_evidence,
        conversation_id=11,
        message_id=22,
    )


@pytest.mark.asyncio
async def test_registry_blocks_unbound_or_stale_batch_ids_before_handler() -> None:
    registry = Registry(ROOT / "config" / "tools.yaml", profile="adas_operator")
    calls: list[tuple[str, dict[str, Any]]] = []

    async def read_handler(arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(("scrapex_read", arguments))
        return {"status": "sentinel"}

    async def map_handler(arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(("scrapex_adas_map", arguments))
        return {"status": "sentinel"}

    registry.register("scrapex_read", read_handler)
    registry.register("scrapex_adas_map", map_handler)
    evidence = scrapex_evidence_from_result(
        "scrapex_read",
        {"action": "list_batches"},
        _verified_scrapex_result(
            "list_batches", {"batches": [{"id": "batch-observed-7"}]}
        ),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="list-call",
    )
    assert evidence is not None

    for name, arguments, bound_evidence, conversation_id, message_id in (
        (
            "scrapex_read",
            {"action": "batch_summary", "batch_id": "batch-invented"},
            evidence,
            11,
            22,
        ),
        (
            "scrapex_adas_map",
            {"action": "start_batch", "batch_id": "batch-observed-7"},
            None,
            11,
            22,
        ),
        (
            "scrapex_adas_map",
            {"action": "pause_batch", "batch_id": "batch-observed-7"},
            evidence,
            11,
            23,
        ),
        (
            "scrapex_read",
            {"action": "batch_exceptions", "batch_id": "batch-observed-7"},
            evidence,
            12,
            22,
        ),
    ):
        with pytest.raises(ToolBlocked):
            await registry.invoke(
                name,
                arguments,
                conversation_id=conversation_id,
                message_id=message_id,
                scrapex_evidence=bound_evidence,
            )
    assert calls == []

    await registry.invoke(
        "scrapex_read",
        {"action": "batch_summary", "batch_id": "batch-observed-7"},
        conversation_id=11,
        message_id=22,
        scrapex_evidence=evidence,
    )
    await registry.invoke(
        "scrapex_adas_map",
        {"action": "create_exact_batch", "ro_numbers": ["9000000009"]},
        conversation_id=11,
        message_id=22,
    )
    assert calls == [
        (
            "scrapex_read",
            {"action": "batch_summary", "batch_id": "batch-observed-7"},
        ),
        (
            "scrapex_adas_map",
            {"action": "create_exact_batch", "ro_numbers": ["9000000009"]},
        ),
    ]


@pytest.mark.asyncio
async def test_loop_revokes_indeterminate_batch_before_later_sibling_call(
    tmp_path,
    monkeypatch,
) -> None:
    for name, schema in scrapex.SCRAPEX_TOOL_SCHEMAS.items():
        monkeypatch.setitem(TOOL_SCHEMAS, name, schema)

    store = Store(tmp_path / "scrapex-sibling-quarantine.sqlite")
    conversation_id = store.create_conversation("ScrapeX staging")
    message_id = store.add_message(
        conversation_id,
        "user",
        "Continue the already selected ScrapeX batches.",
    )
    registry = Registry(
        ROOT / "config" / "tools.yaml",
        store=store,
        profile="adas_operator",
    )
    read_calls: list[dict[str, Any]] = []
    map_calls: list[dict[str, Any]] = []

    async def read_handler(arguments: dict[str, Any]) -> dict[str, Any]:
        read_calls.append(dict(arguments))
        return _verified_scrapex_result(
            "list_batches",
            {"batches": [{"id": "batch-risk"}, {"id": "batch-safe"}]},
        )

    async def map_handler(arguments: dict[str, Any]) -> dict[str, Any]:
        map_calls.append(dict(arguments))
        if arguments["batch_id"] == "batch-risk":
            return {
                "service": "ScrapeX",
                "action": "start_batch",
                "status": "indeterminate",
                "success": False,
                "executed": False,
                "verified": False,
                "may_have_executed": True,
                "indeterminate": True,
                "retryable": False,
                "message": "The remote outcome could not be verified.",
                "error": {
                    "detail": {
                        "records": [
                            f"ambiguous remote diagnostic {index:04d}"
                            for index in range(800)
                        ]
                    }
                },
            }
        return _verified_scrapex_result(
            "start_batch", {"batch_id": arguments["batch_id"], "started": True}
        )

    registry.register("scrapex_read", read_handler)
    registry.register("scrapex_adas_map", map_handler)

    class Router:
        active_name = "omni"

        @staticmethod
        def active_config():
            return SimpleNamespace(supports_vision=True, supports_audio=True)

    class Client:
        def __init__(self) -> None:
            self.round = 0

        async def stream(self, _messages, tools=None):
            self.round += 1
            catalog = {
                item["function"]["name"]: item["function"] for item in tools or []
            }
            map_actions = {
                branch["properties"]["action"]["const"]
                for branch in catalog["scrapex_adas_map"]["parameters"]["oneOf"]
            }
            if self.round == 1:
                assert "start_batch" not in map_actions
                yield {
                    "type": "tool_call",
                    "id": "list-call",
                    "name": "scrapex_read",
                    "arguments": json.dumps({"action": "list_batches"}),
                }
                return
            if self.round == 2:
                assert "start_batch" in map_actions
                for call_id, batch_id in (
                    ("risk-first", "batch-risk"),
                    ("risk-sibling", "batch-risk"),
                    ("safe-sibling", "batch-safe"),
                ):
                    yield {
                        "type": "tool_call",
                        "id": call_id,
                        "name": "scrapex_adas_map",
                        "arguments": json.dumps(
                            {"action": "start_batch", "batch_id": batch_id}
                        ),
                    }
                return
            yield {"type": "content", "text": "The verified outcomes are shown."}

    client = Client()
    orchestrator = Orchestrator(
        Router(),
        client,
        registry,
        store,
        SimpleNamespace(context_tokens=32_768, max_response_tokens=1_024),
    )
    events = [
        event
        async for event in orchestrator.run_turn(
            conversation_id,
            "Continue the already selected ScrapeX batches.",
            approval_context={
                "session_id": "local:local-dev",
                "user_id": "local-dev",
                "role": "owner",
                "message_id": message_id,
            },
        )
    ]

    assert client.round == 3
    assert read_calls == [{"action": "list_batches"}]
    assert map_calls == [
        {"action": "start_batch", "batch_id": "batch-risk"},
        {"action": "start_batch", "batch_id": "batch-safe"},
    ]
    map_results = [
        event["result"]
        for event in events
        if event.get("type") == "tool_result"
        and event.get("name") == "scrapex_adas_map"
    ]
    assert [result["status"] for result in map_results] == [
        "indeterminate",
        "blocked",
        "verified",
    ]
    assert "automatic retry is forbidden" in map_results[1]["message"]
    store.close()


def test_scrapex_staging_contract_has_no_conversational_text_input() -> None:
    for helper in (
        scrapex_evidence_from_result,
        validate_scrapex_batch_binding,
    ):
        parameter_names = set(inspect.signature(helper).parameters)
        assert parameter_names.isdisjoint(
            {"message", "request_text", "user_message", "user_text", "utterance"}
        )
