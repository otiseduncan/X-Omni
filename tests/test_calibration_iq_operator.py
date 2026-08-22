from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.routes import create_router
from core.orchestrator.loop import Orchestrator
from core.services import calibration_iq as ciq
from core.state.db import Store
from core.tools.registry import NeedsApproval, Registry, TOOL_SCHEMAS, ToolBlocked


@dataclass
class FakeSettings:
    calibration_iq_base_url: str
    calibration_iq_project_path: Path


@pytest.fixture
def settings(tmp_path: Path) -> FakeSettings:
    project = tmp_path / "calibration iq"
    project.mkdir()
    (project / ".env").write_text("TOOL_SERVICE_TOKEN=test-service-token\n", encoding="utf-8")
    return FakeSettings(
        "http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq",
        project,
    )


def _context(
    call_id: str = "call-operator-1", *, message_id: int = 99
) -> dict[str, Any]:
    return {
        "conversation_id": 41,
        "tool_call_id": call_id,
        "message_id": message_id,
        "user_id": "local-dev",
        "role": "owner",
    }


def _verified_receipt(action: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "mutation_id": f"mutation-{index}",
        "idempotency_key": action["idempotency_key"],
        "correlation_id": action["correlation_id"],
        "operation": action["operation"],
        "risk": "routine",
        "status": "completed",
        "success": True,
        "replayed": False,
        "repair_order_id": action.get("repair_order_id"),
        "resource_type": "note" if action["operation"] == "add_note" else None,
        "resource_id": f"resource-{index}",
        "before": {},
        "after": {},
        "verification": {"verified": True},
        "error": None,
    }


def _capabilities(
    routine: list[str], destructive: list[str] | None = None
) -> dict[str, Any]:
    return {
        "policy": {
            "routine": routine,
            "destructive": destructive or [],
        },
        "capabilities": ["calibration_iq.operator"],
    }


def _persisted_evidence_document(
    *,
    document_id: str = "doc-1",
    calibration_ids: list[str] | None = None,
    source_name: str = "OEM Calibration Procedure.pdf",
    version: int = 1,
) -> dict[str, Any]:
    return {
        "id": document_id,
        "version": version,
        "document_type": "oem_procedure",
        "title": "OEM Calibration Procedure",
        "status": "validated",
        "source_uri": f"adas-si:///{ciq.quote(source_name)}",
        "source_name": source_name,
        "storage_relative_path": f"supporting-documents/{source_name}",
        "file_size": 4096,
        "sha256": "a" * 64,
        "page_references": ["p. 7"],
        "citation": f"{source_name}, p. 7",
        "calibration_item_ids": calibration_ids or ["cal-1"],
        "archived_at": None,
        "download_url": (
            "/api/v1/tools/v1/calibration-iq/operator/documents/"
            f"{document_id}/download"
        ),
    }


def _install_transport(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        ciq.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    async def resolved(_settings):
        return _settings.calibration_iq_base_url

    monkeypatch.setattr(ciq, "resolve_base", resolved)


@pytest.mark.asyncio
async def test_legacy_ro_detail_unwraps_current_ro_wrapper(settings, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/operator/ros/" in request.url.path:
            return httpx.Response(404, json={"detail": "operator route unavailable"})
        assert request.url.path.endswith("/ros/2400911667")
        return httpx.Response(
            200,
            json={
                "success": True,
                "ro": {
                    "id": "ro-id",
                    "ro_number": "2400911667",
                    "status": "Research",
                    "vehicle": {"vin": "VIN-1"},
                },
                "verification": {"verified": True},
            },
        )

    _install_transport(monkeypatch, handler)
    result = await ciq.get_repair_order(settings, {"repair_order_id": "2400911667"})
    assert result["status"] == "verified"
    assert result["repair_order"]["RO"] == "2400911667"
    assert result["raw"]["id"] == "ro-id"
    assert "ro" not in result["raw"]


@pytest.mark.asyncio
async def test_specific_ro_query_uses_full_operator_snapshot_and_proxy_urls(
    settings, monkeypatch,
):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/operator/ros/ro-full/snapshot")
        return httpx.Response(200, json={
            "repair_order": {
                "id": "ro-full", "ro_number": "2400911777", "version": 8,
                "year": 2024, "make": "Toyota", "model": "Camry",
            },
            "shop": {"id": "shop-1", "name": "Macon"},
            "workflow": {"status": "RESEARCH", "version": 8},
            "calibrations": [{"id": "cal-1", "name": "front camera"}],
            "blockers": [{"id": "block-1", "title": "reassembly"}],
            "notes": [{"id": "note-1", "body": "Bumper pending"}],
            "research": {"state": "research_in_progress", "documents": [{
                "id": "doc-1", "document_type": "oem_procedure",
                "download_url": (
                    "http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq/"
                    "operator/documents/doc-1/download"
                ),
            }]},
            "activity": [{"id": "activity-1", "description": "Research opened"}],
            "audit": [{"id": "audit-1", "action": "update"}],
        })

    _install_transport(monkeypatch, handler)
    result = await ciq.get_repair_order(settings, {"repair_order_id": "ro-full"})
    assert result["status"] == "verified"
    assert result["repair_order"]["Status"] == "RESEARCH"
    assert result["repair_order"]["Shop"] == "Macon"
    assert result["repair_order"]["requirements"][0]["id"] == "cal-1"
    assert result["raw"]["activity"][0]["id"] == "activity-1"
    assert result["raw"]["research"]["documents"][0]["download_url"] == (
        "/api/calibration-iq/documents/doc-1/download"
    )


def test_document_url_mapping_does_not_invent_links_for_other_receipt_resources():
    payload = {
        "receipts": [
            {
                "resource_type": "note",
                "after": {
                    "id": "note-1", "body": "Ready", "source_uri": "customer-app"
                },
            },
            {
                "resource_type": "calibration_item",
                "before": {"id": "cal-1", "status": "required"},
            },
            {
                "resource_type": "repair_order",
                "after": {"id": "ro-1", "status": "research"},
            },
        ]
    }
    mapped = ciq._map_document_urls(payload)
    assert "download_url" not in mapped["receipts"][0]
    assert "download_url" not in mapped["receipts"][0]["after"]
    assert "download_url" not in mapped["receipts"][1]["before"]
    assert "download_url" not in mapped["receipts"][2]["after"]


def test_operator_url_mapping_rewrites_only_explicit_genuine_backend_urls():
    payload = {
        "research": {
            "documents": [
                {
                    "id": "doc-1",
                    "document_type": "oem_procedure",
                    "download_url": (
                        "http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq/"
                        "operator/documents/doc-1/download"
                    ),
                },
                {
                    "id": "doc-without-url",
                    "document_type": "oem_procedure",
                },
            ],
            "workspace": [{
                "kind": "file",
                "path": "notes/case summary.txt",
                "download_url": (
                    "/api/v1/tools/v1/calibration-iq/operator/ros/ro-1/files"
                    "?path=notes%2Fcase+summary.txt"
                ),
            }],
        },
        "photos": [{
            "id": "photo-1",
            "resource_type": "photo",
            "download_url": (
                "http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq/"
                "operator/photos/photo-1/download"
            ),
            "thumbnail_url": (
                "/api/v1/tools/v1/calibration-iq/operator/photos/photo-1/thumbnail"
            ),
        }],
        "note": {
            "id": "note-1",
            "download_url": "http://127.0.0.1:8084/internal/not-a-file-endpoint",
        },
    }

    mapped = ciq._map_document_urls(payload)

    assert mapped["research"]["documents"][0]["download_url"] == (
        "/api/calibration-iq/documents/doc-1/download"
    )
    assert "download_url" not in mapped["research"]["documents"][1]
    assert mapped["research"]["workspace"][0]["download_url"] == (
        "/api/calibration-iq/workspace-file?"
        "repair_order_id=ro-1&path=notes%2Fcase+summary.txt"
    )
    assert mapped["photos"][0]["download_url"] == (
        "/api/calibration-iq/photos/photo-1/download"
    )
    assert mapped["photos"][0]["thumbnail_url"] == (
        "/api/calibration-iq/photos/photo-1/thumbnail"
    )
    assert "download_url" not in mapped["note"]


@pytest.mark.asyncio
async def test_new_routine_parity_operations_reach_one_verified_backend_batch(
    settings, monkeypatch,
):
    operations = [
        "import_photo",
        "update_photo",
        "copy_entry",
        "archive_entry",
        "restore_entry",
        "unlink_document",
        "replace_document",
        "undo_status",
        "mark_no_calibration_required",
        "reopen_calibration_review",
        "create_location",
    ]
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/operator/capabilities"):
            return httpx.Response(200, json=_capabilities(operations))
        if request.url.path.endswith("/operator/actions"):
            body = json.loads(request.content)
            sent.extend(body["actions"])
            receipts = [
                _verified_receipt(action, index)
                for index, action in enumerate(body["actions"])
            ]
            return httpx.Response(200, json={
                "success": True,
                "partial": False,
                "requested_count": len(receipts),
                "processed_count": len(receipts),
                "stopped_on_error": False,
                "receipts": receipts,
            })
        raise AssertionError(str(request.url))

    _install_transport(monkeypatch, handler)
    result = await ciq.operator_execute(settings, object(), {
        "actions": [
            {"operation": operation, "arguments": {}}
            for operation in operations
        ],
        ciq._INVOCATION_CONTEXT_KEY: _context("parity-routine-call"),
    })

    assert result["status"] == "success"
    assert [item["operation"] for item in sent] == operations


@pytest.mark.asyncio
async def test_new_destructive_parity_operations_are_accepted_only_by_destructive_handler(
    settings, monkeypatch,
):
    operations = ["delete_photo", "delete_prerequisite"]
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/operator/capabilities"):
            return httpx.Response(200, json=_capabilities([], operations))
        if request.url.path.endswith("/operator/actions"):
            body = json.loads(request.content)
            sent.extend(body["actions"])
            receipts = [
                _verified_receipt(action, index)
                for index, action in enumerate(body["actions"])
            ]
            return httpx.Response(200, json={
                "success": True,
                "partial": False,
                "requested_count": len(receipts),
                "processed_count": len(receipts),
                "stopped_on_error": False,
                "receipts": receipts,
            })
        raise AssertionError(str(request.url))

    _install_transport(monkeypatch, handler)
    result = await ciq.operator_execute(
        settings,
        object(),
        {
            "actions": [
                {"operation": operation, "target_id": f"target-{index}"}
                for index, operation in enumerate(operations)
            ],
            ciq._INVOCATION_CONTEXT_KEY: _context("parity-destructive-call"),
        },
        destructive=True,
    )

    assert result["status"] == "success"
    assert [item["operation"] for item in result["receipts"]] == operations
    assert [action["arguments"] for action in sent] == [
        {"confirm": True},
        {"confirm": True},
    ]


@pytest.mark.asyncio
async def test_operator_batch_uses_stable_registry_bound_ids_and_rereads_snapshot(
    settings, monkeypatch,
):
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/operator/capabilities"):
            return httpx.Response(200, json=_capabilities(["add_note"]))
        if request.url.path.endswith("/operator/actions"):
            body = json.loads(request.content)
            requests.append(body)
            receipts = [_verified_receipt(action, i) for i, action in enumerate(body["actions"])]
            return httpx.Response(200, json={
                "success": True,
                "partial": False,
                "requested_count": len(receipts),
                "processed_count": len(receipts),
                "stopped_on_error": False,
                "receipts": receipts,
            })
        if request.url.path.endswith("/operator/ros/ro-1/snapshot"):
            return httpx.Response(200, json={
                "snapshot": {"id": "ro-1", "notes": [{"body": "Bumper installed"}]}
            })
        raise AssertionError(str(request.url))

    _install_transport(monkeypatch, handler)
    args = {
        "actions": [{
            "operation": "add_note",
            "repair_order_id": "ro-1",
            "arguments": {"body": "Bumper installed"},
            # Model-supplied identifiers are deliberately ignored.
            "idempotency_key": "spoofed-idempotency-key",
            "correlation_id": "spoofed-correlation",
        }],
        ciq._INVOCATION_CONTEXT_KEY: _context(),
    }
    first = await ciq.operator_execute(settings, object(), args)
    second_args = dict(args)
    second_args[ciq._INVOCATION_CONTEXT_KEY] = _context("call-operator-2")
    second = await ciq.operator_execute(settings, object(), second_args)
    third_args = dict(args)
    third_args[ciq._INVOCATION_CONTEXT_KEY] = _context(
        "call-operator-3", message_id=100
    )
    third = await ciq.operator_execute(settings, object(), third_args)

    assert first["status"] == "success"
    assert second["status"] == "success"
    assert third["status"] == "success"
    assert first["success"] is True and first["verified"] is True
    assert first["final_snapshots"]["ro-1"]["snapshot"]["id"] == "ro-1"
    first_action = requests[0]["actions"][0]
    second_action = requests[1]["actions"][0]
    third_action = requests[2]["actions"][0]
    assert first_action["idempotency_key"] == second_action["idempotency_key"]
    assert first_action["correlation_id"] != second_action["correlation_id"]
    assert first_action["idempotency_key"] != third_action["idempotency_key"]
    assert first_action["idempotency_key"].startswith("xomni-")
    assert first_action["idempotency_key"] != "spoofed-idempotency-key"
    assert requests[0]["delegation"]["channel"] == "x"
    assert "on_behalf_of_user_id" not in requests[0]["delegation"]


def test_idempotency_is_reorder_and_lost_response_retry_stable_with_duplicate_ordinals():
    context = _context("first-attempt")
    note = {
        "operation": "add_note",
        "repair_order_id": "ro-1",
        "arguments": {"body": "Bumper installed"},
    }
    blocker = {
        "operation": "add_blocker",
        "repair_order_id": "ro-1",
        "arguments": {"reason": "Waiting on reassembly"},
    }
    note_in_batch = ciq._stable_action_ids(context, 0, note, action_index=1)[0]
    note_reordered = ciq._stable_action_ids(context, 0, note, action_index=0)[0]
    note_retry_alone = ciq._stable_action_ids(
        _context("retry-call"), 0, note, action_index=0
    )[0]
    blocker_reordered = ciq._stable_action_ids(context, 0, blocker, action_index=1)[0]
    duplicate_note = ciq._stable_action_ids(context, 1, note, action_index=2)[0]

    assert note_in_batch == note_reordered == note_retry_alone
    assert blocker_reordered != note_in_batch
    assert duplicate_note != note_in_batch


@pytest.mark.asyncio
async def test_live_ro_number_batch_resolves_once_normalizes_aliases_and_hashes_uuid(
    settings, monkeypatch,
):
    ro_number = "20260821205527"
    ro_id = "6e12d275-f516-4f31-a9d4-d816838b81e7"
    collection_requests = 0
    number_snapshot_requests = 0
    posted_batches: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal collection_requests, number_snapshot_requests
        if request.url.path.endswith("/operator/capabilities"):
            return httpx.Response(
                200, json=_capabilities(["add_note", "create_folder"])
            )
        if request.url.path.endswith(f"/operator/ros/{ro_number}/snapshot"):
            number_snapshot_requests += 1
            return httpx.Response(404, json={"detail": "Repair order not found"})
        if request.url.path.endswith("/collection/ros"):
            collection_requests += 1
            assert request.url.params["q"] == ro_number
            assert request.url.params["source_scope"] == "all"
            return httpx.Response(200, json={
                "count": 1,
                "items": [{"id": ro_id, "ro_number": ro_number}],
            })
        if request.url.path.endswith(f"/operator/ros/{ro_id}/snapshot"):
            return httpx.Response(200, json={
                "repair_order": {"id": ro_id, "ro_number": ro_number, "version": 3},
                "workflow": {"status": "RESEARCH"},
            })
        if request.url.path.endswith("/operator/actions"):
            body = json.loads(request.content)
            posted_batches.append(body)
            receipts = [
                _verified_receipt(action, index)
                for index, action in enumerate(body["actions"])
            ]
            return httpx.Response(200, json={
                "success": True,
                "partial": False,
                "requested_count": len(receipts),
                "processed_count": len(receipts),
                "stopped_on_error": False,
                "receipts": receipts,
            })
        raise AssertionError(str(request.url))

    _install_transport(monkeypatch, handler)
    live_prompt_batch = {
        "actions": [
            {
                "operation": "add_note",
                "repair_order_id": ro_number,
                "arguments": {"note": "X natural-language operator proof"},
            },
            {
                "operation": "create_folder",
                "repair_order_id": ro_number,
                "arguments": {"folder_name": "x-natural-proof"},
            },
        ],
        ciq._INVOCATION_CONTEXT_KEY: _context("live-model-batch"),
    }
    first = await ciq.operator_execute(settings, object(), live_prompt_batch)
    canonical_retry = {
        "actions": [
            {
                "operation": "add_note",
                "repair_order_id": ro_id,
                "arguments": {"body": "X natural-language operator proof"},
            },
            {
                "operation": "create_folder",
                "repair_order_id": ro_id,
                "arguments": {"path": "x-natural-proof"},
            },
        ],
        ciq._INVOCATION_CONTEXT_KEY: _context("canonical-retry"),
    }
    second = await ciq.operator_execute(settings, object(), canonical_retry)

    assert first["status"] == second["status"] == "success"
    assert collection_requests == 1
    assert number_snapshot_requests == 1
    assert set(first["final_snapshots"]) == {ro_id}
    assert [item["repair_order_id"] for item in posted_batches[0]["actions"]] == [
        ro_id, ro_id,
    ]
    assert [item["arguments"] for item in posted_batches[0]["actions"]] == [
        {"body": "X natural-language operator proof"},
        {"path": "x-natural-proof"},
    ]
    assert [item["idempotency_key"] for item in posted_batches[0]["actions"]] == [
        item["idempotency_key"] for item in posted_batches[1]["actions"]
    ]


@pytest.mark.asyncio
async def test_ro_number_resolution_fails_closed_when_exact_number_is_ambiguous(
    settings, monkeypatch,
):
    posted = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posted
        if request.url.path.endswith("/operator/capabilities"):
            return httpx.Response(200, json=_capabilities(["add_note"]))
        if request.url.path.endswith("/operator/ros/2400911667/snapshot"):
            return httpx.Response(404, json={"detail": "Repair order not found"})
        if request.url.path.endswith("/collection/ros"):
            return httpx.Response(200, json={
                "count": 2,
                "items": [
                    {"id": "ro-macon", "ro_number": "2400911667"},
                    {"id": "ro-warner-robins", "ro_number": "2400911667"},
                ],
            })
        if request.url.path.endswith("/operator/actions"):
            posted = True
        raise AssertionError(str(request.url))

    _install_transport(monkeypatch, handler)
    result = await ciq.operator_execute(settings, object(), {
        "actions": [{
            "operation": "add_note",
            "repair_order_id": "2400911667",
            "arguments": {"body": "Do not apply ambiguously"},
        }],
        ciq._INVOCATION_CONTEXT_KEY: _context("ambiguous-ro-number"),
    })

    assert result["status"] == "ambiguous_identifier"
    assert result["executed"] is False
    assert result["error"]["details"]["exact_match_count"] == 2
    assert posted is False


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("add_note", {"body": "one", "note": "two"}),
        ("add_note", {"text": "one", "content": "two"}),
        ("create_folder", {"path": "one", "name": "two"}),
        ("create_folder", {"folder_name": "one", "name": "two"}),
    ],
)
def test_operator_argument_alias_conflicts_fail_before_network(
    settings, operation: str, arguments: dict[str, str],
):
    result = asyncio.run(ciq.operator_execute(settings, object(), {
        "actions": [{
            "operation": operation,
            "repair_order_id": "2400911667",
            "arguments": arguments,
        }],
        ciq._INVOCATION_CONTEXT_KEY: _context("alias-conflict"),
    }))

    assert result["status"] == "invalid_input"
    assert result["executed"] is False
    assert "conflicting argument aliases" in result["message"]


@pytest.mark.asyncio
async def test_operator_conflict_is_structured_and_never_success(settings, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/operator/capabilities"):
            return httpx.Response(200, json=_capabilities(["update_ro"]))
        if request.url.path.endswith("/operator/ros/ro-1/snapshot"):
            return httpx.Response(200, json={"snapshot": {"id": "ro-1"}})
        if request.url.path.endswith("/operator/actions"):
            return httpx.Response(409, json={
                "detail": {
                    "code": "conflict",
                    "message": "Repair order version changed.",
                    "retryable": True,
                }
            })
        raise AssertionError(str(request.url))

    _install_transport(monkeypatch, handler)
    result = await ciq.operator_execute(settings, object(), {
        "actions": [{
            "operation": "update_ro",
            "repair_order_id": "ro-1",
            "expected_version": 4,
            "arguments": {"trim": "Limited"},
        }],
        ciq._INVOCATION_CONTEXT_KEY: _context(),
    })
    assert result["status"] == "conflict"
    assert result["executed"] is False
    assert result["success"] is False
    assert result["verified"] is False
    assert result["error"]["retryable"] is True


def test_capability_parser_uses_exact_backend_policy_and_not_permission_names():
    body = _capabilities(
        ["add_note", "update_ro"], ["delete_calibration", "delete_blocker"]
    )
    assert ciq._capability_operations(body) == {
        "add_note", "update_ro", "delete_calibration", "delete_blocker"
    }
    assert ciq._capability_operations({
        "capabilities": ["calibration_iq.operator"]
    }) == set()


def test_direct_research_completion_cannot_bypass_evidence_gate(settings):
    for state in ("research_complete", "Research Complete", "completed"):
        result = asyncio.run(ciq.operator_execute(settings, object(), {
            "actions": [{
                "operation": "update_research",
                "repair_order_id": "ro-1",
                "arguments": {"state": state},
            }],
            ciq._INVOCATION_CONTEXT_KEY: _context(f"direct-complete-{state}"),
        }))
        assert result["status"] == "prerequisite_missing"
        assert result["executed"] is False


def test_calibration_mutation_and_research_same_ro_require_sequential_operator_calls(
    settings,
):
    result = asyncio.run(ciq.operator_execute(settings, object(), {
        "actions": [
            {
                "operation": "add_calibration",
                "repair_order_id": "ro-1",
                "arguments": {"name": "front camera"},
            },
            {
                "operation": "research_ro",
                "repair_order_id": "ro-1",
                "arguments": {"complete_research": True},
            },
        ],
        ciq._INVOCATION_CONTEXT_KEY: _context("stale-composite"),
    }))
    assert result["status"] == "prerequisite_missing"
    assert result["executed"] is False
    assert "sequential" in result["message"].casefold()
    assert "same user turn" in result["message"].casefold()


@pytest.mark.asyncio
async def test_exact_backend_destructive_operation_executes_only_in_destructive_handler(
    settings, monkeypatch,
):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/operator/capabilities"):
            return httpx.Response(200, json=_capabilities(
                ["add_note"], ["delete_calibration", "delete_blocker"]
            ))
        if request.url.path.endswith("/operator/actions"):
            body = json.loads(request.content)
            posted.append(body)
            receipt = _verified_receipt(body["actions"][0], 0)
            receipt.update({"risk": "destructive", "resource_type": "calibration_item"})
            return httpx.Response(200, json={
                "success": True, "partial": False, "requested_count": 1,
                "processed_count": 1, "stopped_on_error": False, "receipts": [receipt],
            })
        raise AssertionError(str(request.url))

    _install_transport(monkeypatch, handler)
    result = await ciq.operator_execute(settings, object(), {
        "actions": [{"operation": "delete_calibration", "target_id": "cal-1"}],
        ciq._INVOCATION_CONTEXT_KEY: _context("approved-delete"),
    }, destructive=True)
    assert result["status"] == "success"
    assert result["verified"] is True
    assert posted[0]["actions"][0]["operation"] == "delete_calibration"


class FakeAdas:
    def __init__(self, source: Path, *, supported: bool = True):
        self.source = source
        self.supported = supported

    def resolve_relative(self, relative: str) -> Path:
        assert relative == self.source.name
        return self.source

    def search(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.supported:
            return {
                "status": "no_result",
                "exact_source_matched": False,
                "results": [],
                "matched_documents": [],
                "message": "No applicable OEM source found.",
            }
        return {
            "status": "success",
            "exact_source_matched": True,
            "results": [{
                "source": self.source.name,
                "title": self.source.stem,
                "relative_path": self.source.name,
                "page": 7,
                "excerpt": "Perform the forward recognition camera calibration.",
                "source_match_score": 18,
            }],
            "matched_documents": [{
                "source": self.source.name,
                "title": self.source.stem,
                "relative_path": self.source.name,
                "source_match_score": 18,
            }],
        }


@pytest.mark.asyncio
async def test_research_ro_imports_oem_pdf_links_pages_and_verifies_completion(
    settings, monkeypatch, tmp_path,
):
    source = tmp_path / "2023 Toyota Camry Front Camera Calibration.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    posted: list[dict[str, Any]] = []
    snapshot_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal snapshot_calls
        if request.url.path.endswith("/operator/capabilities"):
            return httpx.Response(200, json=_capabilities([
                "ensure_case_workspace", "import_document", "update_research"
            ]))
        if request.url.path.endswith("/operator/ros/ro-7/snapshot"):
            snapshot_calls += 1
            return httpx.Response(200, json={"snapshot": {
                "id": "ro-7",
                "vehicle": {"year": 2023, "make": "Toyota", "model": "Camry"},
                "calibration_items": [{"id": "cal-1", "name": "front camera"}],
                "research": {
                    "state": "research_complete" if snapshot_calls > 1 else "research_in_progress",
                    "version": 3,
                },
                "documents": ([
                    _persisted_evidence_document(source_name=source.name)
                ] if snapshot_calls > 1 else []),
            }})
        if request.url.path.endswith("/operator/actions"):
            body = json.loads(request.content)
            posted.append(body)
            receipts = []
            for index, action in enumerate(body["actions"]):
                receipt = _verified_receipt(action, index)
                if action["operation"] == "import_document":
                    receipt.update({
                        "resource_type": "research_document",
                        "resource_id": "doc-1",
                        "after": _persisted_evidence_document(source_name=source.name),
                    })
                receipts.append(receipt)
            return httpx.Response(200, json={
                "success": True,
                "partial": False,
                "requested_count": len(receipts),
                "processed_count": len(receipts),
                "stopped_on_error": False,
                "receipts": receipts,
            })
        raise AssertionError(str(request.url))

    _install_transport(monkeypatch, handler)
    result = await ciq.operator_execute(settings, FakeAdas(source), {
        "actions": [{
            "operation": "research_ro",
            "repair_order_id": "ro-7",
            "arguments": {"complete_research": True},
        }],
        "continue_on_error": True,
        ciq._INVOCATION_CONTEXT_KEY: _context("research-call"),
    })

    assert result["status"] == "success"
    assert [action["operation"] for action in posted[0]["actions"]] == [
        "ensure_case_workspace", "import_document", "update_research"
    ]
    imported = posted[0]["actions"][1]["arguments"]
    assert imported["source_path"] == str(source)
    assert imported["page_references"] == ["p. 7"]
    assert imported["calibration_item_ids"] == ["cal-1"]
    assert imported["status"] == "validated"
    assert imported["source_uri"].startswith("adas-si:///")
    assert posted[0]["actions"][2]["arguments"]["state"] == "research_complete"
    assert posted[0]["continue_on_error"] is False
    assert result["research"][0]["research_complete_verified"] is True
    assert result["receipts"][1]["after"]["download_url"] == (
        "/api/calibration-iq/documents/doc-1/download"
    )
    snapshot_doc = result["final_snapshots"]["ro-7"]["snapshot"]["documents"][0]
    assert snapshot_doc["download_url"] == "/api/calibration-iq/documents/doc-1/download"
    assert "127.0.0.1" not in json.dumps(result)


@pytest.mark.asyncio
async def test_research_resolve_failure_cannot_support_or_complete(
    settings, monkeypatch, tmp_path,
):
    source = tmp_path / "2023 Toyota Camry Front Camera Calibration.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    posted: list[dict[str, Any]] = []

    class UnresolvableAdas(FakeAdas):
        def resolve_relative(self, relative: str) -> Path:
            raise ValueError(f"unsafe or unavailable: {relative}")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/operator/capabilities"):
            return httpx.Response(200, json=_capabilities(["ensure_case_workspace"]))
        if request.url.path.endswith("/operator/ros/ro-unresolved/snapshot"):
            return httpx.Response(200, json={"snapshot": {
                "id": "ro-unresolved",
                "vehicle": {"year": 2023, "make": "Toyota", "model": "Camry"},
                "calibration_items": [{"id": "cal-1", "name": "front camera"}],
                "research": {"state": "research_in_progress", "version": 1},
            }})
        if request.url.path.endswith("/operator/actions"):
            body = json.loads(request.content)
            posted.append(body)
            receipts = [
                _verified_receipt(action, index)
                for index, action in enumerate(body["actions"])
            ]
            return httpx.Response(200, json={
                "success": True,
                "partial": False,
                "requested_count": len(receipts),
                "processed_count": len(receipts),
                "stopped_on_error": False,
                "receipts": receipts,
            })
        raise AssertionError(str(request.url))

    _install_transport(monkeypatch, handler)
    result = await ciq.operator_execute(settings, UnresolvableAdas(source), {
        "actions": [{
            "operation": "research_ro",
            "repair_order_id": "ro-unresolved",
            "arguments": {"complete_research": True},
        }],
        ciq._INVOCATION_CONTEXT_KEY: _context("unresolved-research"),
    })

    assert [action["operation"] for action in posted[0]["actions"]] == [
        "ensure_case_workspace"
    ]
    report = result["research"][0]
    assert report["findings"][0]["supported"] is False
    assert report["research_complete_action_added"] is False
    assert report["completion_withheld"] is True
    assert result["status"] == "failed"
    assert result["missing_documentation"] == ["front camera"]


@pytest.mark.asyncio
async def test_complete_state_without_persisted_evidence_for_every_requirement_is_unverified(
    settings, monkeypatch, tmp_path,
):
    source = tmp_path / "2023 Toyota Camry Front Camera Calibration.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    snapshot_calls = 0
    persisted_document = _persisted_evidence_document(source_name=source.name)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal snapshot_calls
        if request.url.path.endswith("/operator/capabilities"):
            return httpx.Response(200, json=_capabilities([
                "ensure_case_workspace", "import_document", "update_research"
            ]))
        if request.url.path.endswith("/operator/ros/ro-no-integrity/snapshot"):
            snapshot_calls += 1
            return httpx.Response(200, json={"snapshot": {
                "id": "ro-no-integrity",
                "vehicle": {"year": 2023, "make": "Toyota", "model": "Camry"},
                "calibration_items": [
                    {"id": "cal-1", "name": "front camera"},
                    *([{"id": "cal-2", "name": "blind spot monitor"}]
                      if snapshot_calls > 1 else []),
                ],
                "research": {
                    "state": (
                        "research_complete" if snapshot_calls > 1
                        else "research_in_progress"
                    ),
                    "version": 2,
                },
                "documents": [persisted_document] if snapshot_calls > 1 else [],
            }})
        if request.url.path.endswith("/operator/actions"):
            body = json.loads(request.content)
            receipts = [
                _verified_receipt(action, index)
                for index, action in enumerate(body["actions"])
            ]
            return httpx.Response(200, json={
                "success": True,
                "partial": False,
                "requested_count": len(receipts),
                "processed_count": len(receipts),
                "stopped_on_error": False,
                "receipts": receipts,
            })
        raise AssertionError(str(request.url))

    _install_transport(monkeypatch, handler)
    result = await ciq.operator_execute(settings, FakeAdas(source), {
        "actions": [{
            "operation": "research_ro",
            "repair_order_id": "ro-no-integrity",
            "arguments": {"complete_research": True},
        }],
        ciq._INVOCATION_CONTEXT_KEY: _context("missing-persisted-integrity"),
    })

    report = result["research"][0]
    assert report["research_state_verified"] is True
    assert report["persisted_evidence_verified"] is False
    assert report["research_complete_verified"] is False
    assert report["persisted_evidence_missing"][0]["calibration_id"] == "cal-2"
    assert result["status"] == "failed"
    assert result["success"] is False
    assert result["verified"] is False
    assert result["partial"] is False


@pytest.mark.asyncio
async def test_research_completion_is_withheld_and_result_not_success_when_evidence_missing(
    settings, monkeypatch, tmp_path,
):
    source = tmp_path / "placeholder.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/operator/capabilities"):
            return httpx.Response(200, json=_capabilities(["ensure_case_workspace"]))
        if request.url.path.endswith("/operator/ros/ro-8/snapshot"):
            return httpx.Response(200, json={"snapshot": {
                "id": "ro-8",
                "vehicle": {"year": 2024, "make": "Toyota", "model": "Tundra"},
                "calibration_items": [{"id": "cal-missing", "name": "blind spot monitor"}],
                "research": {"state": "research_in_progress", "version": 1},
            }})
        if request.url.path.endswith("/operator/actions"):
            body = json.loads(request.content)
            posted.append(body)
            receipts = [_verified_receipt(action, index) for index, action in enumerate(body["actions"])]
            return httpx.Response(200, json={
                "success": True,
                "partial": False,
                "requested_count": len(receipts),
                "processed_count": len(receipts),
                "stopped_on_error": False,
                "receipts": receipts,
            })
        raise AssertionError(str(request.url))

    _install_transport(monkeypatch, handler)
    result = await ciq.operator_execute(settings, FakeAdas(source, supported=False), {
        "actions": [{
            "operation": "research_ro",
            "repair_order_id": "ro-8",
            "arguments": {"complete_research": True},
        }],
        ciq._INVOCATION_CONTEXT_KEY: _context("missing-research-call"),
    })
    assert [action["operation"] for action in posted[0]["actions"]] == [
        "ensure_case_workspace"
    ]
    assert result["status"] == "failed"
    assert result["success"] is False and result["verified"] is False
    assert result["research"][0]["completion_withheld"] is True
    assert result["missing_documentation"] == ["blind spot monitor"]


@pytest.mark.asyncio
async def test_research_completion_requires_supported_coverage_for_every_active_spec(
    settings, monkeypatch, tmp_path,
):
    source = tmp_path / "2024 Toyota Camera Calibration.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/operator/capabilities"):
            return httpx.Response(200, json=_capabilities([
                "ensure_case_workspace", "import_document", "update_research"
            ]))
        if request.url.path.endswith("/operator/ros/ro-subset/snapshot"):
            return httpx.Response(200, json={"snapshot": {
                "id": "ro-subset",
                "vehicle": {"year": 2024, "make": "Toyota", "model": "Camry"},
                "calibration_items": [
                    {"id": "cal-camera", "name": "front camera"},
                    {"id": "cal-bsm", "name": "blind spot monitor"},
                ],
                "research": {"state": "research_in_progress", "version": 2},
            }})
        if request.url.path.endswith("/operator/actions"):
            body = json.loads(request.content)
            posted.append(body)
            receipts = [
                _verified_receipt(action, index)
                for index, action in enumerate(body["actions"])
            ]
            return httpx.Response(200, json={
                "success": True,
                "partial": False,
                "requested_count": len(receipts),
                "processed_count": len(receipts),
                "stopped_on_error": False,
                "receipts": receipts,
            })
        raise AssertionError(str(request.url))

    _install_transport(monkeypatch, handler)
    result = await ciq.operator_execute(settings, FakeAdas(source), {
        "actions": [{
            "operation": "research_ro",
            "repair_order_id": "ro-subset",
            "arguments": {
                "complete_research": True,
                "queries": [{
                    "calibration_id": "cal-camera",
                    "label": "front camera",
                    "query": "2024 Toyota Camry front camera calibration",
                }],
            },
        }],
        ciq._INVOCATION_CONTEXT_KEY: _context("subset-research-call"),
    })

    assert [action["operation"] for action in posted[0]["actions"]] == [
        "ensure_case_workspace", "import_document"
    ]
    report = result["research"][0]
    assert report["research_complete_eligible"] is False
    assert report["research_complete_action_added"] is False
    assert report["completion_withheld"] is True
    assert report["missing_documents"] == [{
        "calibration_id": "cal-bsm",
        "calibration": "blind spot monitor",
        "reason": "No supported OEM evidence query covered this required calibration.",
    }]
    assert result["success"] is False
    assert result["missing_documentation"] == ["blind spot monitor"]


@pytest.mark.asyncio
async def test_research_destination_folder_normalizes_and_appends_each_source_filename(
    tmp_path: Path,
):
    sources = [
        tmp_path / "Toyota BSM Procedure.pdf",
        tmp_path / "Toyota Position Statement.pdf",
    ]
    for source in sources:
        source.write_bytes(b"%PDF-1.4\n")

    class MultiAdas:
        def resolve_relative(self, relative: str) -> Path:
            return next(source for source in sources if source.name == relative)

        def search(self, _args: dict[str, Any]) -> dict[str, Any]:
            return {
                "status": "success",
                "exact_source_matched": True,
                "results": [
                    {
                        "source": source.name,
                        "title": source.stem,
                        "relative_path": source.name,
                        "page": index + 2,
                        "excerpt": "Applicable OEM procedure evidence.",
                        "source_match_score": 18,
                    }
                    for index, source in enumerate(sources)
                ],
                "matched_documents": [
                    {
                        "source": source.name,
                        "title": source.stem,
                        "relative_path": source.name,
                        "source_match_score": 18,
                    }
                    for source in sources
                ],
            }

    snapshot = {
        "vehicle": {"year": 2024, "make": "Toyota", "model": "Camry"},
        "calibration_items": [{"id": "cal-bsm", "name": "blind spot monitor"}],
        "research": {"state": "research_in_progress", "version": 1},
    }
    action = {
        "operation": "research_ro",
        "repair_order_id": "ro-folder",
        "arguments": {"destination_folder": "calibration-procedures\\BSM"},
    }
    expanded, report = await ciq._expand_research_action(MultiAdas(), action, snapshot)
    imports = [item for item in expanded if item["operation"] == "import_document"]
    assert [item["arguments"]["destination_path"] for item in imports] == [
        f"calibration-procedures/BSM/{source.name}" for source in sources
    ]
    assert len(report["documents_prepared"]) == 2

    invalid_action = {
        **action,
        "arguments": {"destination_path": "calibration-procedures/BSM/procedure.pdf"},
    }
    with pytest.raises(ciq.CalibrationIQOperatorInput, match="destination_folder"):
        await ciq._expand_research_action(MultiAdas(), invalid_action, snapshot)


@pytest.mark.asyncio
async def test_repeat_research_recognizes_existing_source_and_does_not_reimport(
    settings, monkeypatch, tmp_path,
):
    source = tmp_path / "2023 Toyota Camry Front Camera Calibration.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    posted = []
    source_uri = f"adas-si:///{ciq.quote(source.name)}"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/operator/capabilities"):
            return httpx.Response(200, json=_capabilities([
                "ensure_case_workspace", "import_document", "update_document",
                "link_document", "update_research",
            ]))
        if request.url.path.endswith("/operator/ros/ro-repeat/snapshot"):
            return httpx.Response(200, json={"snapshot": {
                "id": "ro-repeat",
                "vehicle": {"year": 2023, "make": "Toyota", "model": "Camry"},
                "calibration_items": [{"id": "cal-1", "name": "front camera"}],
                "research": {"state": "research_complete", "version": 4},
                "documents": [{
                    **_persisted_evidence_document(
                        document_id="doc-existing", source_name=source.name
                    ),
                    "source_uri": source_uri,
                }],
            }})
        if request.url.path.endswith("/operator/actions"):
            body = json.loads(request.content)
            posted.append(body)
            receipts = [_verified_receipt(action, index) for index, action in enumerate(body["actions"])]
            return httpx.Response(200, json={
                "success": True, "partial": False,
                "requested_count": len(receipts), "processed_count": len(receipts),
                "stopped_on_error": False, "receipts": receipts,
            })
        raise AssertionError(str(request.url))

    _install_transport(monkeypatch, handler)
    result = await ciq.operator_execute(settings, FakeAdas(source), {
        "actions": [{
            "operation": "research_ro",
            "repair_order_id": "ro-repeat",
            "arguments": {"complete_research": True},
        }],
        ciq._INVOCATION_CONTEXT_KEY: _context("repeat-research"),
    })
    assert result["status"] == "success"
    assert [action["operation"] for action in posted[0]["actions"]] == [
        "ensure_case_workspace"
    ]
    report = result["research"][0]
    assert report["documents_prepared"] == []
    assert report["already_present"][0]["document_id"] == "doc-existing"
    assert report["already_present"][0]["new_links_requested"] == []
    assert report["research_complete_already_verified"] is True
    assert report["persisted_evidence_verified"] is True


@pytest.mark.asyncio
async def test_repeat_research_metadata_only_uses_authoritative_document_version(
    tmp_path,
):
    source = tmp_path / "2023 Toyota Camry Front Camera Calibration.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    snapshot = {
        "vehicle": {"year": 2023, "make": "Toyota", "model": "Camry"},
        "calibration_items": [{"id": "cal-1", "name": "front camera"}],
        "research": {"state": "research_complete", "version": 4},
        "documents": [{
            **_persisted_evidence_document(
                document_id="doc-existing",
                calibration_ids=["cal-1"],
                source_name=source.name,
                version=6,
            ),
            "page_references": ["p. 6"],
        }],
    }
    action = {
        "operation": "research_ro",
        "repair_order_id": "ro-versioned-metadata",
        "arguments": {"complete_research": True},
    }

    expanded, report = await ciq._expand_research_action(
        FakeAdas(source), action, snapshot
    )

    assert [item["operation"] for item in expanded] == [
        "ensure_case_workspace", "update_document",
    ]
    update = expanded[1]
    assert update["target_id"] == "doc-existing"
    assert update["expected_version"] == 6
    assert update["arguments"]["page_references"] == ["p. 6", "p. 7"]
    assert "changes" not in update["arguments"]
    assert "calibration_item_ids" not in update["arguments"]
    assert report["documents_prepared"] == []
    assert report["already_present"][0]["new_links_requested"] == []


@pytest.mark.asyncio
async def test_repeat_research_updates_existing_document_with_snapshot_version_and_links(
    settings, monkeypatch, tmp_path,
):
    source = tmp_path / "2023 Toyota Camry Front Camera Calibration.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    source_uri = f"adas-si:///{ciq.quote(source.name)}"
    posted: list[dict[str, Any]] = []
    snapshot_calls = 0

    def snapshot(*, final: bool) -> dict[str, Any]:
        return {
            "id": "ro-versioned-update",
            "vehicle": {"year": 2023, "make": "Toyota", "model": "Camry"},
            "calibration_items": [{"id": "cal-1", "name": "front camera"}],
            "research": {"state": "research_complete", "version": 4},
            "documents": [{
                **_persisted_evidence_document(
                    document_id="doc-existing",
                    calibration_ids=(
                        ["cal-old", "cal-1"] if final else ["cal-old"]
                    ),
                    source_name=source.name,
                    version=12 if final else 11,
                ),
                "source_uri": source_uri,
                "page_references": ["p. 6", "p. 7"] if final else ["p. 6"],
            }],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal snapshot_calls
        if request.url.path.endswith("/operator/capabilities"):
            return httpx.Response(200, json=_capabilities([
                "ensure_case_workspace", "import_document", "update_document",
                "link_document", "update_research",
            ]))
        if request.url.path.endswith("/operator/ros/ro-versioned-update/snapshot"):
            snapshot_calls += 1
            return httpx.Response(200, json={"snapshot": snapshot(final=snapshot_calls > 1)})
        if request.url.path.endswith("/operator/actions"):
            body = json.loads(request.content)
            posted.append(body)
            receipts = [
                _verified_receipt(action, index)
                for index, action in enumerate(body["actions"])
            ]
            return httpx.Response(200, json={
                "success": True,
                "partial": False,
                "requested_count": len(receipts),
                "processed_count": len(receipts),
                "stopped_on_error": False,
                "receipts": receipts,
            })
        raise AssertionError(str(request.url))

    _install_transport(monkeypatch, handler)
    result = await ciq.operator_execute(settings, FakeAdas(source), {
        "actions": [{
            "operation": "research_ro",
            "repair_order_id": "ro-versioned-update",
            "arguments": {"complete_research": True},
        }],
        ciq._INVOCATION_CONTEXT_KEY: _context("repeat-versioned-update"),
    })

    assert result["status"] == "success"
    assert [action["operation"] for action in posted[0]["actions"]] == [
        "ensure_case_workspace", "update_document",
    ]
    update = posted[0]["actions"][1]
    assert update["target_id"] == "doc-existing"
    assert update["expected_version"] == 11
    assert update["arguments"] == {
        "status": "validated",
        "page_references": ["p. 6", "p. 7"],
        "citation": f"{source.name}, p. 7",
        "notes": (
            "ADAS SI queries: 2023 Toyota Camry front camera"
        ),
        "calibration_item_ids": ["cal-1", "cal-old"],
    }
    assert "changes" not in update["arguments"]
    canonical_update = {
        key: update[key]
        for key in ("operation", "target_id", "expected_version", "arguments")
    }
    expected_idempotency, _ = ciq._stable_action_ids(
        _context("retry-with-new-tool-call"),
        0,
        canonical_update,
        action_index=1,
    )
    assert update["idempotency_key"] == expected_idempotency
    assert all(action["operation"] != "import_document" for action in posted[0]["actions"])
    report = result["research"][0]
    assert report["documents_prepared"] == []
    assert report["already_present"][0]["document_id"] == "doc-existing"
    assert report["already_present"][0]["new_links_requested"] == ["cal-1"]
    assert report["persisted_evidence_verified"] is True
    assert report["research_complete_already_verified"] is True


@pytest.mark.asyncio
async def test_repeat_research_links_existing_document_with_snapshot_version_only(
    settings, monkeypatch, tmp_path,
):
    source = tmp_path / "2023 Toyota Camry Front Camera Calibration.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    source_uri = f"adas-si:///{ciq.quote(source.name)}"
    posted: list[dict[str, Any]] = []
    snapshot_calls = 0

    def snapshot(*, final: bool) -> dict[str, Any]:
        return {
            "id": "ro-versioned-link",
            "vehicle": {"year": 2023, "make": "Toyota", "model": "Camry"},
            "calibration_items": [{"id": "cal-1", "name": "front camera"}],
            "research": {"state": "research_complete", "version": 4},
            "documents": [{
                **_persisted_evidence_document(
                    document_id="doc-existing",
                    calibration_ids=(
                        ["cal-old", "cal-1"] if final else ["cal-old"]
                    ),
                    source_name=source.name,
                    version=8 if final else 7,
                ),
                "source_uri": source_uri,
            }],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal snapshot_calls
        if request.url.path.endswith("/operator/capabilities"):
            return httpx.Response(200, json=_capabilities([
                "ensure_case_workspace", "import_document", "update_document",
                "link_document", "update_research",
            ]))
        if request.url.path.endswith("/operator/ros/ro-versioned-link/snapshot"):
            snapshot_calls += 1
            return httpx.Response(200, json={"snapshot": snapshot(final=snapshot_calls > 1)})
        if request.url.path.endswith("/operator/actions"):
            body = json.loads(request.content)
            posted.append(body)
            receipts = [
                _verified_receipt(action, index)
                for index, action in enumerate(body["actions"])
            ]
            return httpx.Response(200, json={
                "success": True,
                "partial": False,
                "requested_count": len(receipts),
                "processed_count": len(receipts),
                "stopped_on_error": False,
                "receipts": receipts,
            })
        raise AssertionError(str(request.url))

    _install_transport(monkeypatch, handler)
    result = await ciq.operator_execute(settings, FakeAdas(source), {
        "actions": [{
            "operation": "research_ro",
            "repair_order_id": "ro-versioned-link",
            "arguments": {"complete_research": True},
        }],
        ciq._INVOCATION_CONTEXT_KEY: _context("repeat-versioned-link"),
    })

    assert result["status"] == "success"
    assert [action["operation"] for action in posted[0]["actions"]] == [
        "ensure_case_workspace", "link_document",
    ]
    link = posted[0]["actions"][1]
    assert link["target_id"] == "doc-existing"
    assert link["expected_version"] == 7
    assert link["arguments"] == {"calibration_item_ids": ["cal-1"]}
    assert all(action["operation"] != "import_document" for action in posted[0]["actions"])
    report = result["research"][0]
    assert report["documents_prepared"] == []
    assert report["persisted_evidence_verified"] is True
    assert report["research_complete_already_verified"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_version", [None, 0, -1, True, "7"])
@pytest.mark.parametrize(
    ("page_references", "calibration_ids", "operation"),
    [
        (["p. 6"], ["cal-1"], "update_document"),
        (["p. 7"], ["cal-old"], "link_document"),
    ],
)
async def test_repeat_research_fails_closed_without_authoritative_document_version(
    tmp_path, invalid_version, page_references, calibration_ids, operation,
):
    source = tmp_path / "2023 Toyota Camry Front Camera Calibration.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    existing = {
        **_persisted_evidence_document(
            document_id="doc-existing",
            calibration_ids=calibration_ids,
            source_name=source.name,
        ),
        "version": invalid_version,
        "page_references": page_references,
    }
    snapshot = {
        "vehicle": {"year": 2023, "make": "Toyota", "model": "Camry"},
        "calibration_items": [{"id": "cal-1", "name": "front camera"}],
        "research": {"state": "research_complete", "version": 4},
        "documents": [existing],
    }
    action = {
        "operation": "research_ro",
        "repair_order_id": "ro-invalid-version",
        "arguments": {"complete_research": True},
    }

    with pytest.raises(
        ciq.CalibrationIQOperatorInput,
        match=f"positive document version required for {operation}",
    ):
        await ciq._expand_research_action(FakeAdas(source), action, snapshot)


@pytest.mark.asyncio
async def test_unverified_backend_receipt_cannot_become_operator_success(settings, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/operator/capabilities"):
            return httpx.Response(200, json=_capabilities(["add_note"]))
        if request.url.path.endswith("/operator/actions"):
            action = json.loads(request.content)["actions"][0]
            receipt = _verified_receipt(action, 0)
            receipt["verification"] = {"verified": False, "reason": "reread mismatch"}
            return httpx.Response(200, json={
                "success": True, "partial": False, "requested_count": 1,
                "processed_count": 1, "stopped_on_error": False, "receipts": [receipt],
            })
        if request.url.path.endswith("/operator/ros/ro-1/snapshot"):
            return httpx.Response(200, json={"snapshot": {"id": "ro-1"}})
        raise AssertionError(str(request.url))

    _install_transport(monkeypatch, handler)
    result = await ciq.operator_execute(settings, object(), {
        "actions": [{"operation": "add_note", "repair_order_id": "ro-1", "arguments": {"body": "x"}}],
        ciq._INVOCATION_CONTEXT_KEY: _context("unverified-call"),
    })
    assert result["status"] == "failed"
    assert result["success"] is False
    assert result["verified"] is False


@pytest.mark.asyncio
async def test_all_failed_backend_receipts_are_failed_not_partial_success(
    settings, monkeypatch,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/operator/capabilities"):
            return httpx.Response(200, json=_capabilities(["add_note"]))
        if request.url.path.endswith("/operator/actions"):
            action = json.loads(request.content)["actions"][0]
            receipt = _verified_receipt(action, 0)
            receipt.update({
                "status": "failed",
                "success": False,
                "verification": {"verified": False},
                "error": {"code": "conflict", "message": "The note was not added."},
            })
            return httpx.Response(200, json={
                "success": False,
                # Even if a backend labels the stopped batch partial, X must
                # not claim partial success when no receipt is successful.
                "partial": True,
                "requested_count": 1,
                "processed_count": 1,
                "stopped_on_error": True,
                "receipts": [receipt],
            })
        if request.url.path.endswith("/operator/ros/ro-1/snapshot"):
            return httpx.Response(200, json={"snapshot": {"id": "ro-1"}})
        raise AssertionError(str(request.url))

    _install_transport(monkeypatch, handler)
    result = await ciq.operator_execute(settings, object(), {
        "actions": [{
            "operation": "add_note",
            "repair_order_id": "ro-1",
            "arguments": {"body": "Bumper installed"},
        }],
        ciq._INVOCATION_CONTEXT_KEY: _context("all-failed-call"),
    })
    assert result["executed"] is True
    assert result["status"] == "failed"
    assert result["success"] is False
    assert result["partial"] is False
    assert result["error"]["code"] == "conflict"


@pytest.mark.asyncio
async def test_managed_document_fetch_uses_only_verified_backend_endpoint_and_is_bounded(
    settings, monkeypatch,
):
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        content = b"%PDF-1.4\nmanaged-copy"
        return httpx.Response(
            200,
            content=content,
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'inline; filename="OEM Procedure.pdf"',
                "content-length": str(len(content)),
                "x-content-length-verified": str(len(content)),
                "x-content-sha256": hashlib.sha256(content).hexdigest(),
            },
        )

    _install_transport(monkeypatch, handler)
    result = await ciq.fetch_operator_document(settings, "doc-verified-1")
    assert result["status"] == "verified"
    assert result["content"].startswith(b"%PDF-1.4")
    assert result["content_length"] == len(result["content"])
    assert result["sha256"] == hashlib.sha256(result["content"]).hexdigest()
    assert requested[0].url.path.endswith(
        "/operator/documents/doc-verified-1/download"
    )
    assert requested[0].headers["authorization"] == "Bearer test-service-token"
    safe_result = {key: value for key, value in result.items() if key != "content"}
    assert "test-service-token" not in json.dumps(safe_result)


@pytest.mark.asyncio
async def test_managed_document_fetch_rejects_backend_integrity_mismatch(settings, monkeypatch):
    content = b"%PDF bytes that do not match"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, headers={
            "content-type": "application/pdf",
            "content-length": str(len(content)),
            "x-content-length-verified": str(len(content)),
            "x-content-sha256": "0" * 64,
        })

    _install_transport(monkeypatch, handler)
    result = await ciq.fetch_operator_document(settings, "doc-corrupt")
    assert result["status"] == "invalid_response"
    assert result["success"] is False
    assert result["verified"] is False
    assert result["error"]["details"]["sha256_matches"] is False


@pytest.mark.asyncio
async def test_workspace_file_fetch_uses_confined_endpoint_and_allows_verified_text(
    settings, monkeypatch,
):
    requested: list[httpx.Request] = []
    content = b'{"repair_order_id":"ro-1"}'

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(200, content=content, headers={
            "content-type": "application/json; charset=utf-8",
            "content-length": str(len(content)),
            "x-content-length-verified": str(len(content)),
            "x-content-sha256": hashlib.sha256(content).hexdigest(),
        })

    _install_transport(monkeypatch, handler)
    result = await ciq.fetch_operator_workspace_file(
        settings, "ro-1", "notes/case summary.json"
    )

    assert result["status"] == "verified"
    assert result["content"] == content
    assert result["content_type"] == "application/json"
    assert requested[0].url.path.endswith("/operator/ros/ro-1/files")
    assert requested[0].url.params["path"] == "notes/case summary.json"
    assert requested[0].headers["authorization"] == "Bearer test-service-token"
    assert requested[0].headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
async def test_photo_fetch_enforces_image_type_and_fixed_byte_bound(
    settings, monkeypatch,
):
    content = b"not-really-a-photo"
    response_type = "application/pdf"
    verified_size = len(content)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, headers={
            "content-type": response_type,
            "content-length": str(len(content)),
            "x-content-length-verified": str(verified_size),
            "x-content-sha256": hashlib.sha256(content).hexdigest(),
        })

    _install_transport(monkeypatch, handler)
    wrong_type_result = await ciq.fetch_operator_photo(
        settings, "photo-1", "download"
    )
    assert wrong_type_result["status"] == "invalid_response"

    response_type = "image/jpeg"
    verified_size = len(content) - 1
    wrong_length_result = await ciq.fetch_operator_photo(
        settings, "photo-1", "thumbnail"
    )
    assert wrong_length_result["status"] == "invalid_response"

    verified_size = len(content)
    monkeypatch.setattr(ciq, "MAX_OPERATOR_PHOTO_BYTES", 4)
    oversized_result = await ciq.fetch_operator_photo(
        settings, "photo-1", "thumbnail"
    )
    assert oversized_result["status"] == "photo_too_large"
    assert oversized_result["executed"] is False


def test_same_origin_managed_document_route_streams_safe_headers(tmp_path: Path, monkeypatch):
    async def fake_fetch(_settings, document_id: str) -> dict[str, Any]:
        assert document_id == "doc-1"
        return {
            "status": "verified",
            "success": True,
            "verified": True,
            "content": b"%PDF safe",
            "content_type": "application/pdf",
            "content_disposition": 'inline; filename="OEM.pdf"',
            "content_length": len(b"%PDF safe"),
            "sha256": hashlib.sha256(b"%PDF safe").hexdigest(),
        }

    async def fake_workspace_fetch(
        _settings, repair_order_id: str, path: str
    ) -> dict[str, Any]:
        assert (repair_order_id, path) == ("ro-1", "notes/case.txt")
        content = b"verified workspace text"
        return {
            "status": "verified",
            "success": True,
            "verified": True,
            "content": content,
            "content_type": "text/plain",
            "content_disposition": 'attachment; filename="case.txt"',
            "content_length": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    async def fake_photo_fetch(
        _settings, photo_id: str, variant: str
    ) -> dict[str, Any]:
        assert (photo_id, variant) == ("photo-1", "thumbnail")
        content = b"\xff\xd8verified-photo\xff\xd9"
        return {
            "status": "verified",
            "success": True,
            "verified": True,
            "content": content,
            "content_type": "image/jpeg",
            "content_disposition": 'inline; filename="photo.jpg"',
            "content_length": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    monkeypatch.setattr(ciq, "fetch_operator_document", fake_fetch)
    monkeypatch.setattr(ciq, "fetch_operator_workspace_file", fake_workspace_fetch)
    monkeypatch.setattr(ciq, "fetch_operator_photo", fake_photo_fetch)
    store = Store(tmp_path / "route.sqlite")
    registry = Registry(_policy(tmp_path / "route-policy"), store=store)

    async def owner_session():
        return {"google_sub": "local-dev", "user_id": "local-dev", "role": "owner"}

    app = FastAPI()
    app.include_router(create_router(
        SimpleNamespace(),
        store,
        SimpleNamespace(active_name="omni"),
        registry,
        owner_session,
    ))
    client = TestClient(app)
    response = client.get("/api/calibration-iq/documents/doc-1/download")
    assert response.status_code == 200
    assert response.content == b"%PDF safe"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'inline; filename="OEM.pdf"'
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-content-sha256"] == hashlib.sha256(b"%PDF safe").hexdigest()
    assert response.headers["x-content-length-verified"] == str(len(b"%PDF safe"))

    workspace = client.get(
        "/api/calibration-iq/workspace-file",
        params={"repair_order_id": "ro-1", "path": "notes/case.txt"},
    )
    assert workspace.status_code == 200
    assert workspace.content == b"verified workspace text"
    assert workspace.headers["content-type"] == "text/plain; charset=utf-8"
    assert workspace.headers["x-content-sha256"] == hashlib.sha256(
        workspace.content
    ).hexdigest()

    photo = client.get("/api/calibration-iq/photos/photo-1/thumbnail")
    assert photo.status_code == 200
    assert photo.headers["content-type"] == "image/jpeg"
    assert photo.headers["x-content-length-verified"] == str(len(photo.content))

    async def oversized_photo(_settings, _photo_id: str, _variant: str) -> dict[str, Any]:
        return {
            "status": "photo_too_large",
            "success": False,
            "verified": False,
            "error": {"code": "photo_too_large", "message": "Photo exceeded proxy bound."},
        }

    monkeypatch.setattr(ciq, "fetch_operator_photo", oversized_photo)
    rejected = client.get("/api/calibration-iq/photos/photo-1/download")
    assert rejected.status_code == 413
    assert rejected.json()["detail"]["code"] == "photo_too_large"
    store.close()


def _policy(tmp_path: Path, *, operator_tier: str = "operator_authorized") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    policy = tmp_path / "tools.yaml"
    policy.write_text(
        "roots: []\nwrite_roots: []\ntools:\n"
        f"  calibration_iq_operator:\n    tier: {operator_tier}\n"
        "  calibration_iq_update:\n    tier: confirm_required\n"
        "  calibration_iq_destructive:\n    tier: confirm_required\n",
        encoding="utf-8",
    )
    return policy


def test_policy_tiers_are_explicit_and_invalid_values_fail_closed(tmp_path: Path):
    registry = Registry(_policy(tmp_path))
    assert registry.tier("calibration_iq_operator") == "operator_authorized"
    assert registry.tier("calibration_iq_destructive") == "confirm_required"
    assert Registry(_policy(tmp_path / "invalid", operator_tier="typo_allow"))\
        .tier("calibration_iq_operator") == "blocked"


def test_parity_operations_are_explicitly_split_between_routine_and_destructive_tools():
    routine = set(
        TOOL_SCHEMAS["calibration_iq_operator"]["parameters"]["properties"]
        ["actions"]["items"]["properties"]["operation"]["enum"]
    )
    destructive = set(
        TOOL_SCHEMAS["calibration_iq_destructive"]["parameters"]["properties"]
        ["actions"]["items"]["properties"]["operation"]["enum"]
    )
    new_routine = {
        "import_photo",
        "update_photo",
        "copy_entry",
        "archive_entry",
        "restore_entry",
        "unlink_document",
        "replace_document",
        "undo_status",
        "mark_no_calibration_required",
        "reopen_calibration_review",
        "create_location",
    }
    expected_destructive = {
        "delete_calibration",
        "delete_blocker",
        "delete_photo",
        "delete_prerequisite",
    }
    assert new_routine <= routine
    assert destructive == expected_destructive
    assert routine.isdisjoint(destructive)
    assert routine == ciq.ROUTINE_OPERATOR_OPERATIONS
    assert destructive == ciq.DESTRUCTIVE_OPERATOR_OPERATIONS


def test_operator_context_fails_closed_without_positive_persisted_message_id():
    for message_id in (None, 0, -1, True):
        with pytest.raises(ToolBlocked, match="persisted user turn"):
            Registry._calibration_iq_invocation_context(
                conversation_id=1,
                tool_call_id="call-1",
                message_id=message_id,
                user_id="local-dev",
                role="owner",
            )


def test_operator_failure_is_logged_failed_with_authoritative_invocation_context(tmp_path: Path):
    store = Store(tmp_path / "state.sqlite")
    registry = Registry(_policy(tmp_path), store=store)
    captured: dict[str, Any] = {}

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        captured.update(args)
        return {
            "status": "conflict", "executed": False, "success": False,
            "verified": False, "partial": False, "message": "Version conflict.",
        }

    registry.register("calibration_iq_operator", handler)
    conversation_id = store.create_conversation("operator receipt truth")
    message_id = store.add_message(conversation_id, "user", "update the RO")
    result = asyncio.run(registry.invoke(
        "calibration_iq_operator",
        {"actions": [{"operation": "update_ro", "repair_order_id": "ro-1"}],
         ciq._INVOCATION_CONTEXT_KEY: {"conversation_id": 999, "tool_call_id": "spoof"}},
        message_id=message_id,
        conversation_id=conversation_id,
        tool_call_id="real-call-id",
        user_id="local-dev",
        role="owner",
    ))
    assert result["success"] is False
    assert captured[ciq._INVOCATION_CONTEXT_KEY]["conversation_id"] == conversation_id
    assert captured[ciq._INVOCATION_CONTEXT_KEY]["tool_call_id"] == "real-call-id"
    row = store.conn.execute(
        "SELECT status, approved_by, args_json FROM tool_calls WHERE tool_call_id = ?", ("real-call-id",)
    ).fetchone()
    assert row["status"] == "failed"
    assert row["approved_by"] == "operator_authorized"
    assert ciq._INVOCATION_CONTEXT_KEY not in json.loads(row["args_json"])
    store.close()


def test_legacy_conflict_approval_receipt_is_failed_not_success(tmp_path: Path):
    store = Store(tmp_path / "state.sqlite")
    registry = Registry(_policy(tmp_path), store=store)
    registry.register("calibration_iq_update", lambda _args: {
        "status": "conflict",
        "executed": False,
        "message": "The repair order changed since it was read.",
    })
    conversation_id = store.create_conversation("legacy receipt truth")
    message_id = store.add_message(conversation_id, "user", "change it")
    approval_id = store.create_approval(
        "calibration_iq_update",
        "Change RO",
        {"name": "calibration_iq_update", "args": {
            "operation": "update_ro", "repair_order_id": "ro-1", "arguments": {},
        }},
        conversation_id=conversation_id,
        session_id="session-a",
        user_id="owner-a",
        message_id=message_id,
        tool_call_id="legacy-conflict-call",
    )
    outcome = asyncio.run(registry.resolve_approval(
        approval_id,
        True,
        conversation_id=conversation_id,
        session_id="session-a",
        user_id="owner-a",
    ))
    assert outcome["approval"]["status"] == "failed"
    assert outcome["receipt"]["status"] == "failed"
    assert outcome["receipt"]["success"] is False
    assert outcome["receipt"]["executed"] is False
    store.close()


def test_destructive_delete_requires_bound_approval_and_receipt_truth(tmp_path: Path):
    store = Store(tmp_path / "state.sqlite")
    registry = Registry(_policy(tmp_path), store=store)
    captured: dict[str, Any] = {}

    def destructive_handler(args: dict[str, Any]) -> dict[str, Any]:
        captured.update(args)
        return {
            "status": "success",
            "executed": True,
            "success": True,
            "verified": True,
            "partial": False,
            "receipts": [{
                "operation": "delete_calibration",
                "status": "completed",
                "success": True,
                "verification": {"verified": True},
            }],
        }

    registry.register("calibration_iq_destructive", destructive_handler)
    conversation_id = store.create_conversation("destructive operator")
    message_id = store.add_message(conversation_id, "user", "remove the bad calibration")
    args = {"actions": [{"operation": "delete_calibration", "target_id": "cal-1"}]}
    with pytest.raises(NeedsApproval):
        asyncio.run(registry.invoke(
            "calibration_iq_destructive",
            args,
            message_id=message_id,
            conversation_id=conversation_id,
            tool_call_id="delete-call",
            user_id="local-dev",
            role="owner",
        ))
    approval_id = store.create_approval(
        "calibration_iq_destructive",
        registry.approval_summary("calibration_iq_destructive", args),
        {"name": "calibration_iq_destructive", "args": args},
        conversation_id=conversation_id,
        session_id="session-owner",
        user_id="local-dev",
        message_id=message_id,
        tool_call_id="delete-call",
    )
    outcome = asyncio.run(registry.resolve_approval(
        approval_id,
        True,
        conversation_id=conversation_id,
        session_id="session-owner",
        user_id="local-dev",
    ))
    assert outcome["approval"]["status"] == "succeeded"
    assert outcome["receipt"]["success"] is True
    assert captured[ciq._INVOCATION_CONTEXT_KEY]["conversation_id"] == conversation_id
    assert captured[ciq._INVOCATION_CONTEXT_KEY]["tool_call_id"] == "delete-call"
    store.close()


@pytest.mark.parametrize(
    "operation",
    ["delete_calibration", "delete_blocker", "delete_photo", "delete_prerequisite"],
)
def test_backend_destructive_delete_is_rejected_from_routine_tool_before_network(
    settings, operation: str,
):
    result = asyncio.run(ciq.operator_execute(settings, object(), {
        "actions": [{"operation": operation, "target_id": "target-1"}],
        ciq._INVOCATION_CONTEXT_KEY: _context("destructive-call"),
    }))
    assert result["status"] == "approval_required"
    assert result["executed"] is False
    assert result["success"] is False


def test_routine_parity_operation_is_rejected_from_destructive_tool_before_network(settings):
    result = asyncio.run(ciq.operator_execute(
        settings,
        object(),
        {
            "actions": [{"operation": "import_photo", "repair_order_id": "ro-1"}],
            ciq._INVOCATION_CONTEXT_KEY: _context("wrong-tool-call"),
        },
        destructive=True,
    ))
    assert result["status"] == "invalid_operation"
    assert result["executed"] is False


@pytest.mark.parametrize(
    "operation", ["mark_no_calibration_required", "reopen_calibration_review"]
)
def test_no_calibration_decision_and_research_require_sequential_same_turn_calls(
    settings, operation: str,
):
    result = asyncio.run(ciq.operator_execute(settings, object(), {
        "actions": [
            {"operation": operation, "repair_order_id": "ro-1", "arguments": {}},
            {"operation": "research_ro", "repair_order_id": "ro-1", "arguments": {}},
        ],
        ciq._INVOCATION_CONTEXT_KEY: _context("no-calibration-research-call"),
    }))
    assert result["status"] == "prerequisite_missing"
    assert result["executed"] is False
    assert "sequential calibration_iq_operator calls" in result["message"]


def test_arbitrary_hard_delete_operation_is_never_accepted(settings):
    result = asyncio.run(ciq.operator_execute(settings, object(), {
        "actions": [{"operation": "hard_delete_repair_order", "repair_order_id": "ro-1"}],
        ciq._INVOCATION_CONTEXT_KEY: _context("invented-hard-delete-call"),
    }))
    assert result["status"] == "invalid_operation"
    assert result["executed"] is False


@pytest.mark.asyncio
async def test_natural_language_multi_action_request_executes_one_operator_batch(tmp_path: Path):
    store = Store(tmp_path / "multi-action.sqlite")
    conversation_id = store.create_conversation("Calibration IQ multi action")
    user_text = "Add a blocker, add a note, and keep working on the same RO."
    message_id = store.add_message(conversation_id, "user", user_text)
    registry = Registry(_policy(tmp_path / "multi-policy"), store=store)
    captured: list[dict[str, Any]] = []

    async def operator_handler(args: dict[str, Any]) -> dict[str, Any]:
        captured.append(args)
        return {
            "status": "success", "executed": True, "success": True,
            "verified": True, "partial": False,
            "receipts": [
                {"operation": action["operation"], "status": "completed", "success": True,
                 "verification": {"verified": True}}
                for action in args["actions"]
            ],
            "final_snapshots": {"ro-1": {"status": "verified", "snapshot": {"id": "ro-1"}}},
        }

    registry.register("calibration_iq_operator", operator_handler)

    class Router:
        active_name = "omni"

        @staticmethod
        def active_config():
            return SimpleNamespace(supports_vision=True, supports_audio=True)

    class Client:
        calls = 0

        async def stream(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_call",
                    "id": "multi-action-call",
                    "name": "calibration_iq_operator",
                    "arguments": json.dumps({"actions": [
                        {"operation": "add_blocker", "repair_order_id": "ro-1",
                         "arguments": {"reason": "waiting on reassembly"}},
                        {"operation": "add_note", "repair_order_id": "ro-1",
                         "arguments": {"body": "Bumper must be installed."}},
                    ]}),
                }
            else:
                assert any(message.get("role") == "tool" for message in messages)
                yield {"type": "content", "text": "Both verified changes are complete."}

    orchestrator = Orchestrator(
        Router(), Client(), registry, store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )
    events = [event async for event in orchestrator.run_turn(
        conversation_id,
        user_text,
        approval_context={
            "session_id": "local:local-dev",
            "user_id": "local-dev",
            "role": "owner",
            "message_id": message_id,
        },
    )]
    assert len(captured) == 1
    assert [action["operation"] for action in captured[0]["actions"]] == [
        "add_blocker", "add_note"
    ]
    assert captured[0][ciq._INVOCATION_CONTEXT_KEY]["tool_call_id"] == "multi-action-call"
    assert not any(event["type"] == "approval" for event in events)
    artifacts = [event["artifact"] for event in events if event["type"] == "artifact"]
    assert artifacts == [{
        "type": "calibration_iq_receipt",
        "data": next(event["result"] for event in events if event["type"] == "tool_result"),
    }]
    row = store.conn.execute(
        "SELECT status, approved_by FROM tool_calls WHERE tool_call_id = 'multi-action-call'"
    ).fetchone()
    assert row["status"] == "succeeded"
    assert row["approved_by"] == "operator_authorized"
    store.close()


@pytest.mark.asyncio
async def test_output_dependent_operator_calls_share_turn_and_consume_generated_id(
    tmp_path: Path,
):
    store = Store(tmp_path / "sequential-operator.sqlite")
    conversation_id = store.create_conversation("Add and research calibration")
    user_text = "Add the front camera calibration and research it."
    message_id = store.add_message(conversation_id, "user", user_text)
    registry = Registry(_policy(tmp_path / "sequential-policy"), store=store)
    captured: list[dict[str, Any]] = []
    idempotency_keys: list[str] = []

    async def operator_handler(args: dict[str, Any]) -> dict[str, Any]:
        captured.append(args)
        action = args["actions"][0]
        context = args[ciq._INVOCATION_CONTEXT_KEY]
        idempotency_keys.append(ciq._stable_action_ids(context, 0, action)[0])
        if action["operation"] == "add_calibration":
            receipt = {
                "operation": "add_calibration",
                "status": "completed",
                "success": True,
                "resource_type": "calibration_item",
                "resource_id": "cal-generated-1",
                "verification": {"verified": True},
            }
        else:
            assert action["operation"] == "research_ro"
            assert action["arguments"]["calibration_ids"] == ["cal-generated-1"]
            receipt = {
                "operation": "research_ro",
                "status": "completed",
                "success": True,
                "verification": {"verified": True},
            }
        return {
            "status": "success",
            "executed": True,
            "success": True,
            "verified": True,
            "partial": False,
            "receipts": [receipt],
            "final_snapshots": {
                "ro-1": {"status": "verified", "snapshot": {"id": "ro-1"}}
            },
        }

    registry.register("calibration_iq_operator", operator_handler)

    class Router:
        active_name = "omni"

        @staticmethod
        def active_config():
            return SimpleNamespace(supports_vision=True, supports_audio=True)

    class Client:
        calls = 0

        async def stream(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_call",
                    "id": "add-calibration-call",
                    "name": "calibration_iq_operator",
                    "arguments": json.dumps({"actions": [{
                        "operation": "add_calibration",
                        "repair_order_id": "ro-1",
                        "arguments": {"name": "front camera"},
                    }]}),
                }
            elif self.calls == 2:
                first_result = next(
                    json.loads(message["content"])
                    for message in reversed(messages)
                    if message.get("role") == "tool"
                    and message.get("tool_call_id") == "add-calibration-call"
                )
                generated_id = first_result["receipts"][0]["resource_id"]
                yield {
                    "type": "tool_call",
                    "id": "research-generated-calibration-call",
                    "name": "calibration_iq_operator",
                    "arguments": json.dumps({"actions": [{
                        "operation": "research_ro",
                        "repair_order_id": "ro-1",
                        "arguments": {"calibration_ids": [generated_id]},
                    }]}),
                }
            else:
                yield {
                    "type": "content",
                    "text": "The new calibration and its research were both verified.",
                }

    orchestrator = Orchestrator(
        Router(), Client(), registry, store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )
    events = [event async for event in orchestrator.run_turn(
        conversation_id,
        user_text,
        approval_context={
            "session_id": "local:local-dev",
            "user_id": "local-dev",
            "role": "owner",
            "message_id": message_id,
        },
    )]

    assert len(captured) == 2
    assert [item["actions"][0]["operation"] for item in captured] == [
        "add_calibration", "research_ro"
    ]
    assert all(
        item[ciq._INVOCATION_CONTEXT_KEY]["message_id"] == message_id
        for item in captured
    )
    assert idempotency_keys[0] != idempotency_keys[1]
    assert not any(event["type"] == "approval" for event in events)
    store.close()
