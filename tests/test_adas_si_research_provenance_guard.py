from __future__ import annotations

from core.services import adas_si_research_provenance_guard as guard


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
