from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services import adas_si_research as research
from core.services import adas_si_research_provenance_guard as guard
from core.services import calibration_iq


def test_captured_calibration_provenance_blocks_cross_objective_reuse():
    metadata = {
        "objective": {
            "calibration_item_id": "cal-camera",
            "component": "Forward Recognition Camera",
        }
    }
    assert guard.provenance_allows_reuse(
        metadata, {"calibration_id": "cal-ocs"}
    ) is False


def test_matching_captured_calibration_provenance_allows_reuse():
    metadata = {"objective": {"calibration_item_id": "cal-camera"}}
    assert guard.provenance_allows_reuse(
        metadata, {"calibration_id": "cal-camera"}
    ) is True


def test_legacy_library_document_without_objective_provenance_stays_reviewable():
    assert guard.provenance_allows_reuse(
        {}, {"calibration_id": "cal-bsm"}
    ) is True


def test_legacy_active_job_is_reset_for_revalidation():
    record = {
        "state": "running",
        "objectives": [
            {
                "objective_id": "ocs",
                "status": "finished",
                "outcome": "attached",
                "result": {"title": "Front Camera - Adjustment"},
                "attachments": [{"attached": True}],
                "started_at": "2026-09-15T22:00:00+00:00",
                "finished_at": "2026-09-15T22:01:00+00:00",
            },
            {"objective_id": "radar", "status": "pending"},
        ],
        "result": {"counts": {"attached": 1}},
        "error": "stale",
        "finished_at": "2026-09-15T22:01:00+00:00",
        "notified": True,
    }

    assert guard._reset_legacy_active_record(record) is True
    assert record["research_contract_version"] == guard.RESEARCH_CONTRACT_VERSION
    assert record["state"] == "running"
    assert record["notified"] is False
    assert "result" not in record
    assert "finished_at" not in record
    assert "error" not in record
    for objective in record["objectives"]:
        assert objective["status"] == "pending"
        assert "result" not in objective
        assert "attachments" not in objective
        assert "outcome" not in objective
        assert "finished_at" not in objective


def test_current_contract_job_is_not_reset():
    record = {
        "state": "running",
        "research_contract_version": guard.RESEARCH_CONTRACT_VERSION,
        "objectives": [{"status": "finished", "result": {"ok": True}}],
    }
    assert guard._reset_legacy_active_record(record) is False
    assert record["objectives"][0]["status"] == "finished"


@pytest.mark.asyncio
async def test_existing_ro_document_must_be_linked_to_current_calibration(
    monkeypatch,
):
    relative = "2023/Toyota/Tacoma/Front Camera Adjustment.pdf"
    source_uri = "adas-si:///2023/Toyota/Tacoma/Front%20Camera%20Adjustment.pdf"
    objective = {
        "objective_id": "ocs-objective",
        "repair_order_id": "ro-1",
        "calibration_id": "cal-ocs",
        "calibration_title": "Occupant Classification System",
    }
    document = {
        "artifact": {
            "relative_path": relative,
            "sha256": "a" * 64,
            "already_present": True,
        }
    }
    existing = {
        "id": "doc-camera",
        "version": 4,
        "source_uri": source_uri,
        "calibration_item_ids": ["cal-camera"],
    }
    linked = {
        **existing,
        "version": 5,
        "calibration_item_ids": ["cal-camera", "cal-ocs"],
    }
    reads = [
        {"status": "verified", "raw": {"research": {"documents": [existing]}}},
        {"status": "verified", "raw": {"research": {"documents": [linked]}}},
    ]
    executions = []

    async def get_ro(_settings, _args):
        return reads.pop(0)

    async def execute(_settings, _adas, args, **_kwargs):
        executions.append(args)
        return {"status": "completed", "success": True, "verified": True}

    monkeypatch.setattr(calibration_iq, "get_repair_order", get_ro)
    monkeypatch.setattr(calibration_iq, "operator_execute", execute)

    attach = research.default_attach(SimpleNamespace(), object())
    result = await attach(
        objective,
        document,
        {
            "conversation_id": 1,
            "message_id": 2,
            "tool_call_id": "research",
            "user_id": "local-dev",
            "role": "owner",
        },
    )

    assert result["attached"] is True
    assert result["status"] == "linked_existing"
    assert result["calibration_item_id"] == "cal-ocs"
    assert len(executions) == 1
    action = executions[0]["actions"][0]
    assert action == {
        "operation": "link_document",
        "target_id": "doc-camera",
        "expected_version": 4,
        "arguments": {
            "calibration_item_ids": ["cal-ocs"],
            "evidence_role": "PROCEDURE",
        },
    }
