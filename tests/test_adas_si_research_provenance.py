from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services import adas_si_research as research
from core.services import adas_si_research_source_cascade as cascade
from core.services import calibration_iq


def test_captured_calibration_provenance_blocks_cross_objective_reuse():
    metadata = {
        "objective": {
            "calibration_item_id": "cal-camera",
            "component": "Forward Recognition Camera",
        }
    }
    assert cascade.provenance_allows_reuse(
        metadata, {"calibration_id": "cal-ocs"}
    ) is False


def test_matching_captured_calibration_provenance_allows_reuse():
    metadata = {"objective": {"calibration_item_id": "cal-camera"}}
    assert cascade.provenance_allows_reuse(
        metadata, {"calibration_id": "cal-camera"}
    ) is True


def test_legacy_library_document_without_objective_provenance_stays_reviewable():
    assert cascade.provenance_allows_reuse(
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

    assert research.reset_outdated_running_record(record) is True
    assert record["research_contract_version"] == research.RESEARCH_CONTRACT_VERSION
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
        "research_contract_version": research.RESEARCH_CONTRACT_VERSION,
        "objectives": [{"status": "finished", "result": {"ok": True}}],
    }
    assert research.reset_outdated_running_record(record) is False
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


_CONTEXT = {"conversation_id": 1, "message_id": 2, "tool_call_id": "research", "user_id": "local-dev", "role": "owner"}


class _Adas:
    def __init__(self, root):
        self.root = root

    def resolve_relative(self, relative):
        return self.root / relative


@pytest.mark.asyncio
async def test_a_fresh_import_counts_only_when_the_reread_links_this_calibration(monkeypatch, tmp_path):
    relative = "2023/Toyota/Tacoma/BSM Operation Check.pdf"
    source_uri = "adas-si:///2023/Toyota/Tacoma/BSM%20Operation%20Check.pdf"
    objective = {"objective_id": "bsm", "repair_order_id": "ro-1", "calibration_id": "cal-bsm", "calibration_title": "Blind Spot Monitor"}
    document = {"title": "BSM Operation Check", "artifact": {"relative_path": relative, "sha256": "b" * 64}, "review": {"decision": "ACCEPT", "confidence": 0.9}}
    # CIQ stored the document, but not against the calibration it was imported for.
    reads = [
        {"status": "verified", "raw": {"research": {"documents": []}}},
        {"status": "verified", "raw": {"research": {"documents": [{"id": "doc-1", "source_uri": source_uri, "calibration_item_ids": ["cal-seat-belt"]}]}}},
    ]
    executions = []

    async def get_ro(_settings, _args):
        return reads.pop(0)

    async def execute(_settings, _adas, args, **_kwargs):
        executions.append(args)
        return {"status": "completed"}

    monkeypatch.setattr(calibration_iq, "get_repair_order", get_ro)
    monkeypatch.setattr(calibration_iq, "operator_execute", execute)
    result = await research.default_attach(SimpleNamespace(), _Adas(tmp_path))(objective, document, _CONTEXT)

    assert result["attached"] is False
    assert result["status"] == "not_confirmed"
    assert result["calibration_item_id"] == "cal-bsm"
    assert "not linked to this calibration item" in result["reason"]
    imported = executions[0]["actions"][1]
    assert imported["operation"] == "import_document"
    assert imported["arguments"]["calibration_item_ids"] == ["cal-bsm"]
    assert imported["arguments"]["status"] == "validated"


@pytest.mark.asyncio
async def test_a_fresh_import_linked_to_this_calibration_is_attached(monkeypatch, tmp_path):
    relative = "2023/Toyota/Tacoma/Radar Adjustment.pdf"
    source_uri = "adas-si:///2023/Toyota/Tacoma/Radar%20Adjustment.pdf"
    objective = {"objective_id": "radar", "repair_order_id": "ro-1", "calibration_id": "cal-radar"}
    document = {"artifact": {"relative_path": relative, "sha256": "c" * 64}}
    reads = [
        {"status": "verified", "raw": {"research": {"documents": []}}},
        {"status": "verified", "raw": {"research": {"documents": [{"id": "doc-2", "source_uri": source_uri, "calibration_item_ids": ["cal-radar"]}]}}},
    ]

    async def get_ro(_settings, _args):
        return reads.pop(0)

    async def execute(_settings, _adas, args, **_kwargs):
        return {"status": "completed"}

    monkeypatch.setattr(calibration_iq, "get_repair_order", get_ro)
    monkeypatch.setattr(calibration_iq, "operator_execute", execute)
    result = await research.default_attach(SimpleNamespace(), _Adas(tmp_path))(objective, document, _CONTEXT)
    assert result == {
        "attached": True,
        "status": "attached",
        "document_id": "doc-2",
        "receipt_status": "completed",
        "receipt_message": None,
        "source_uri": source_uri,
        "calibration_item_id": "cal-radar",
        "document_status": "candidate",
    }


@pytest.mark.asyncio
async def test_an_existing_document_already_linked_to_this_calibration_is_not_rewritten(monkeypatch):
    source_uri = "adas-si:///2023/Toyota/Tacoma/OCS.pdf"
    objective = {"objective_id": "ocs", "repair_order_id": "ro-1", "calibration_id": "cal-ocs"}
    document = {"artifact": {"relative_path": "2023/Toyota/Tacoma/OCS.pdf"}}

    async def get_ro(_settings, _args):
        return {"status": "verified", "raw": {"research": {"documents": [{"id": "doc-3", "source_uri": source_uri, "calibration_item_ids": ["cal-ocs"]}]}}}

    async def execute(*_args, **_kwargs):
        raise AssertionError("nothing to write")

    monkeypatch.setattr(calibration_iq, "get_repair_order", get_ro)
    monkeypatch.setattr(calibration_iq, "operator_execute", execute)
    result = await research.default_attach(SimpleNamespace(), object())(objective, document, _CONTEXT)
    assert result["attached"] is True and result["status"] == "already_attached"
    assert result["document_id"] == "doc-3"


@pytest.mark.asyncio
async def test_library_reuse_refuses_an_artifact_captured_for_another_calibration(tmp_path):
    import json

    relative = "2023/Toyota/Tacoma/Front Camera - Adjustment.pdf"
    pdf = tmp_path / relative
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4")
    pdf.with_suffix(".source.json").write_text(json.dumps({"objective": {"calibration_item_id": "cal-camera"}}), encoding="utf-8")

    service = SimpleNamespace(adas=_Adas(tmp_path), client=object())
    reviewed = await cascade._review_local(  # noqa: SLF001
        service, {"calibration_id": "cal-ocs", "topic": "t"}, {"relative_path": relative}
    )
    assert reviewed is None
