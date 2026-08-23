from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.services import adas_artifact_catalog as artifacts
from core.services import calibration_iq_work_prep as prep
from core.services import calibration_iq_weekly_queue as weekly_queue
from core.tools.registry import MAX_RESULT_BYTES, Registry


def _snapshot(ro_id: str, ro_number: str, vin: str) -> dict:
    return {
        "repair_order": {"id": ro_id, "ro_number": ro_number, "vin": vin},
        "vehicle": {
            "vin": vin,
            "year": 2020,
            "make": "Example",
            "model": f"Model {ro_number}",
        },
        "calibrations": [
            {
                "id": f"cal-{ro_id}",
                "calibration_type": "Occupant Classification / Seat-weight sensor calibration",
                "determination": "REQUIRED",
                "method": "UNKNOWN",
                "version": 1,
            }
        ],
    }


class _Catalog:
    def __init__(self) -> None:
        self.coverage_calls: list[str] = []

    def discover(self, **query):
        ro_number = query.get("ro_number")
        if ro_number == "300":
            raise OSError("one damaged catalog entry")
        return {
            "status": artifacts.DISCOVERY_VERIFIED,
            "record": {
                "ro_number": ro_number,
                "vin": query.get("vin"),
                "vehicle": {
                    "year": 2020,
                    "make": "Example",
                    "model": f"Model {ro_number}",
                },
                "inspection_id": f"inspection-{ro_number}",
                "requirements": [
                    {"label": "Occupant Classification System", "method": "UNKNOWN"}
                ],
                "verified": True,
                "sources": [
                    {
                        "kind": "physical_pdf",
                        "relative_path": f"{ro_number} adas map.pdf",
                    }
                ],
            },
            "index": {"scan_complete": True},
            "reason": None,
        }

    def requirement_coverage(self, requirements, **query):
        ro_number = query["ro_number"]
        self.coverage_calls.append(ro_number)
        state = artifacts.MISSING if ro_number == "200" else artifacts.COVERED
        return {
            "status": state,
            "requirements": [
                {
                    "requirement": requirements[0],
                    "state": state,
                    "sources": (
                        []
                        if state == artifacts.MISSING
                        else [{"relative_path": f"{ro_number} adas map.pdf"}]
                    ),
                }
            ],
            "reason": None
            if state == artifacts.COVERED
            else "complete scan found none",
        }


@pytest.mark.asyncio
async def test_weekly_pipeline_separates_covered_missing_and_unverified_and_queues_only_missing(
    monkeypatch, tmp_path
):
    rows = [
        {"id": "ro-1", "ro_number": "100", "phase": 5},
        {"id": "ro-2", "ro_number": "200", "phase": 5},
        {"id": "ro-3", "ro_number": "300", "phase": 5},
    ]
    snapshots = {
        "ro-1": _snapshot("ro-1", "100", "1HGCM82633A004352"),
        "ro-2": _snapshot("ro-2", "200", "1HGCM82633A004353"),
        "ro-3": _snapshot("ro-3", "300", "1HGCM82633A004354"),
    }
    catalog = _Catalog()

    async def query_repair_orders(_settings, _filters):
        return {"status": "verified", "items": rows}

    async def load_snapshot(_settings, identifier):
        return {"status": "verified", "snapshot": snapshots[identifier]}

    monkeypatch.setattr(prep.calibration_iq, "query_repair_orders", query_repair_orders)
    monkeypatch.setattr(prep, "_load_ro_snapshot", load_snapshot)
    monkeypatch.setattr(prep, "_catalog_for", lambda _adas: catalog)

    result = await prep._week_readiness(
        SimpleNamespace(root=tmp_path),
        SimpleNamespace(),
        {
            "phase": "5",
            prep._CONTEXT_KEY: {
                "conversation_id": 77,
                "message_id": 88,
                "tool_call_id": "weekly-test",
                "user_id": "owner",
                "role": "owner",
            },
        },
    )

    assert result["status"] == "partial_success"
    assert result["queue_count"] == 3
    assert result["ready_count"] == 1
    assert result["adas_map_verified_count"] == 2
    assert result["adas_map_missing_count"] == 0
    assert result["adas_map_unverified_count"] == 1
    assert result["adas_map_unavailable_count"] == 1
    assert result["si_covered_count"] == 1
    assert result["si_missing_count"] == 1
    assert result["si_unverified_count"] == 1
    assert result["alldata_queued_count"] == 1
    assert catalog.coverage_calls == ["100", "200"]

    by_ro = {row["ro_number"]: row for row in result["repair_orders"]}
    assert by_ro["100"]["coverage_status"] == artifacts.COVERED
    assert by_ro["200"]["coverage_status"] == artifacts.MISSING
    assert by_ro["300"]["coverage_status"] == artifacts.UNVERIFIED
    assert by_ro["300"]["missing_si"] == []

    stored = weekly_queue.get_store(tmp_path).get("77")
    assert stored is not None
    assert [item.ro_number for item in stored.items] == ["200"]


@pytest.mark.asyncio
async def test_weekly_result_stays_verified_at_tool_gateway_with_many_snapshot_rereads(
    monkeypatch,
):
    rows = [
        {"id": f"ro-{index}", "ro_number": f"RO-{index}", "phase": 5}
        for index in range(49)
    ]

    async def query_repair_orders(_settings, _filters):
        return {"status": "verified", "items": rows}

    async def load_snapshot(_settings, identifier):
        return {
            "status": "verified",
            "snapshot": _snapshot(identifier, identifier.upper(), f"VIN-{identifier}"),
        }

    async def discover(_catalog, _snapshot_value):
        return {
            "status": "verified",
            "discovery_status": "verified",
            "governing_source": "ADAS Map",
            "requirements": [
                {
                    "label": "Occupant Classification System",
                    "family": "occupant_classification",
                }
            ],
            "sources": [
                {
                    "kind": "physical_pdf",
                    "artifact_kind": "adas_map_report",
                    "relative_path": "example.pdf",
                    "sha256": "a" * 64,
                    "identity_verified": True,
                }
            ],
            "requirement_count": 1,
            "explicit_no_calibration": False,
                "artifact_index": {
                    "status": "success",
                    "scan_complete": True,
                    "physical_pdf_count": 137,
                    "unreadable": 15,
                    "errors": [
                        {"code": "index_error", "message": f"failed file {index}"}
                        for index in range(137)
                    ],
                },
            }

    async def reconcile(_settings, _adas, snapshot, _map_info, _context):
        receipt = {
            "status": "completed",
            "success": True,
            "verified": True,
            "verification": {
                "verified": True,
                "source": "authoritative_reread",
                "resource_type": "calibration_item",
                "resource_id": f"cal-{snapshot['repair_order']['id']}",
            },
            "operation": "update_calibration",
            "mutation_id": f"mutation-{snapshot['repair_order']['id']}",
            "idempotency_key": f"idem-{snapshot['repair_order']['id']}",
            "before": {"payload": "b" * 20_000},
            "after": {"payload": "a" * 20_000},
            "error": {
                "code": "fixture_detail",
                "message": "receipt truth is retained without its large snapshots",
                "details": {"payload": "e" * 20_000},
            },
        }
        return snapshot, [], {
            "status": "succeeded",
            "executed": True,
            "success": True,
            "verified": True,
            "requested_count": 1,
            "processed_count": 1,
            "receipts": [receipt],
            # This is the duplicated authoritative data that pushed the real
            # 49-RO production result beyond the 256 KiB gateway boundary.
            "final_snapshots": {"ro": {"payload": "x" * 40_000}},
            "authoritative_reread": {
                "status": "verified",
                "verified": True,
                "snapshot": {"payload": "z" * 40_000},
            },
        }

    async def coverage(_catalog, _snapshot_value, _map_info):
        return [
            {
                "calibration": "Occupant Classification System",
                "state": artifacts.COVERED,
                "available": True,
                "documents": [f"document-{index}.pdf" for index in range(137)],
                "sources": [{"payload": "y" * 5_000}],
            }
        ]

    monkeypatch.setattr(prep.calibration_iq, "query_repair_orders", query_repair_orders)
    monkeypatch.setattr(prep, "_load_ro_snapshot", load_snapshot)
    monkeypatch.setattr(prep, "_discover_adas_map", discover)
    monkeypatch.setattr(prep, "_reconcile_one", reconcile)
    monkeypatch.setattr(prep, "_catalog_coverage", coverage)

    result = await prep._week_readiness(SimpleNamespace(), SimpleNamespace(), {})
    encoded = json.dumps(result, default=str).encode("utf-8")

    assert result["status"] == "success"
    assert result["executed"] is True
    assert result["ciq_mutations_processed_count"] == 49
    assert result["ciq_receipt_count"] == 49
    assert result["ciq_verified_receipt_count"] == 49
    assert len(result["repair_orders"]) == 49
    assert len(encoded) < MAX_RESULT_BYTES
    assert all(
        "final_snapshots" not in (row.get("reconciliation") or {})
        for row in result["repair_orders"]
    )
    assert all(
        (row.get("reconciliation") or {}).get("authoritative_reread")
        == {"status": "verified", "verified": True}
        for row in result["repair_orders"]
    )
    assert all(
        (row.get("reconciliation") or {}).get("receipts")
        for row in result["repair_orders"]
    )
    assert all(
        "before" not in row["reconciliation"]["receipts"][0]
        and "after" not in row["reconciliation"]["receipts"][0]
        and row["reconciliation"]["receipts"][0]["error"]["code"]
        == "fixture_detail"
        for row in result["repair_orders"]
    )
    assert all(
        row["adas_map"]["artifact_index"]["error_count"] == 137
        and len(row["adas_map"]["artifact_index"]["errors"]) == 3
        and row["coverage"][0]["document_count"] == 137
        and len(row["coverage"][0]["documents"]) == 3
        for row in result["repair_orders"]
    )

    registry = Registry("config/tools.yaml")
    registry.register(prep.TOOL_NAME, lambda _args: result)
    gateway_result = await registry._invoke_handler(prep.TOOL_NAME, {})
    assert gateway_result["status"] == "success"
    assert gateway_result.get("truncated") is not True


def test_readiness_compaction_prioritizes_receipt_risk_and_declares_list_samples():
    receipts = [
        {
            "mutation_id": f"ok-{index}",
            "operation": "add_calibration",
            "status": "completed",
            "success": True,
            "verification": {"verified": True},
        }
        for index in range(4)
    ]
    receipts.append(
        {
            "mutation_id": "critical-last",
            "operation": "add_calibration",
            "status": "completed",
            "success": True,
            "verification": {
                "verified": False,
                "reason": "authoritative reread mismatch",
            },
        }
    )
    evidence = [
        {
            "calibration": f"Requirement {index}",
            "state": artifacts.MISSING,
            "reason": "complete scan found no compatible procedure",
        }
        for index in range(15)
    ]
    row = {
        "repair_order_id": "ro-1",
        "ro_number": "100",
        "status": "reconciliation_failed",
        "ready": False,
        "adas_map": {"status": "verified"},
        "reconciliation": {
            "status": "failed",
            "executed": True,
            "success": False,
            "verified": False,
            "may_have_executed": True,
            "indeterminate": True,
            "verification_recovered_by_reread": False,
            "processed_count": 5,
            "receipts": receipts,
        },
        "missing_si": evidence,
        "unverified_si": [{**item, "state": artifacts.UNVERIFIED} for item in evidence],
    }

    compact = prep._compact_readiness_row(row, minimal=True)

    assert compact["reconciliation"]["receipt_count"] == 5
    assert compact["reconciliation"]["critical_receipt_count"] == 1
    assert compact["reconciliation"]["may_have_executed"] is True
    assert compact["reconciliation"]["indeterminate"] is True
    assert compact["reconciliation"]["verification_recovered_by_reread"] is False
    assert compact["reconciliation"]["receipts"][0]["mutation_id"] == "critical-last"
    assert (
        compact["reconciliation"]["receipts"][0]["verification"]["reason"]
        == "authoritative reread mismatch"
    )
    assert compact["missing_si_count"] == 15
    assert compact["missing_si_truncated"] is True
    assert compact["unverified_si_count"] == 15
    assert compact["unverified_si_truncated"] is True


def test_adaptive_readiness_compaction_retains_every_executed_ro_skeleton():
    rows = []
    for index in range(49):
        receipts = [
            {
                "mutation_id": f"mutation-{index}-{receipt_index}",
                "idempotency_key": "i" * 320,
                "correlation_id": "c" * 320,
                "operation": "update_calibration",
                "status": "completed",
                "success": True,
                "verification": {
                    "verified": True,
                    "reason": "r" * 320,
                },
            }
            for receipt_index in range(12)
        ]
        evidence = [
            {
                "calibration": f"Requirement {item}",
                "state": artifacts.UNVERIFIED,
                "reason": "u" * 320,
            }
            for item in range(15)
        ]
        rows.append(
            {
                "repair_order_id": f"ro-{index}",
                "ro_number": f"RO-{index}",
                "vehicle": "v" * 320,
                "status": "reconciliation_failed",
                "ready": False,
                "adas_map": {"status": "verified", "reason": "m" * 320},
                "reconciliation": {
                    "status": "succeeded",
                    "executed": True,
                    "success": True,
                    "verified": True,
                    "processed_count": 12,
                    "receipts": receipts,
                },
                "unverified_si": evidence,
            }
        )

    compact, metadata = prep._bounded_readiness_rows(rows)

    assert metadata["repair_orders_total"] == 49
    assert metadata["repair_orders_shown"] == 49
    assert metadata["repair_orders_truncated"] is False
    assert all((row.get("reconciliation") or {}).get("executed") is True for row in compact)
    assert len(json.dumps(compact, default=str).encode("utf-8")) <= prep._READINESS_ROWS_BYTE_BUDGET


@pytest.mark.asyncio
async def test_work_prep_uses_exact_operator_snapshot_resolver(monkeypatch):
    observed = {}

    async def resolve(_settings, identifier):
        observed["identifier"] = identifier
        return {
            "status": "verified",
            "snapshot": _snapshot("ro-1", "100", "1HGCM82633A004352"),
        }

    monkeypatch.setattr(prep.calibration_iq, "operator_resolve_snapshot", resolve)
    result = await prep._load_ro_snapshot(SimpleNamespace(), "100")
    assert result["status"] == "verified"
    assert result["snapshot"]["repair_order"]["id"] == "ro-1"
    assert observed == {"identifier": "100"}


@pytest.mark.asyncio
async def test_map_discovery_rejects_optional_identity_contradictions():
    snapshot = _snapshot("ro-1", "100", "1HGCM82633A004352")
    snapshot["vehicle"]["trim"] = "LE"
    snapshot["vehicle"]["configuration"] = {"adas_map_model_configuration": "FWD"}

    class ConflictingCatalog:
        @staticmethod
        def discover(**_query):
            return {
                "status": artifacts.DISCOVERY_VERIFIED,
                "record": {
                    "ro_number": "100",
                    "ciq_ro_id": "ro-1",
                    "vin": "1HGCM82633A004352",
                    "vehicle": {
                        "year": 2020,
                        "make": "Example",
                        "model": "Model 100",
                        "trim": "XSE",
                        "configuration": "AWD",
                    },
                    "inspection_id": "inspection-100",
                    "requirements": [{"label": "Occupant Classification System"}],
                    "sources": [],
                },
                "index": {"scan_complete": True},
            }

    result = await prep._discover_adas_map(ConflictingCatalog(), snapshot)

    assert result["status"] == "ambiguous"
    assert result["identity_conflicts"] == ["trim", "configuration"]


def test_alias_parity_is_bounded_and_does_not_let_si_invent_requirements():
    snapshot = {
        "calibrations": [
            {
                "id": "cal-1",
                "calibration_type": "Passenger Seat Weight Sensor Zero Point",
                "determination": "REQUIRED",
                "method": "UNKNOWN",
                "version": 1,
            }
        ]
    }
    map_info = {
        "status": "verified",
        "requirements": [
            {"label": "Occupant Classification System", "method": "UNKNOWN"}
        ],
    }
    assert prep.build_reconciliation_actions(snapshot, map_info, "ro-1") == []
    assert prep._reconciliation_issues(snapshot, map_info) == []

    # A document-library term never enters the action planner; only the typed
    # governing ADAS Map requirement set is considered.
    unrelated_si_label = "Windshield mono-camera calibration"
    assert unrelated_si_label not in str(
        prep.build_reconciliation_actions(snapshot, map_info, "ro-1")
    )


def test_reconciliation_parity_rejects_extra_active_ciq_requirements():
    snapshot = {
        "calibrations": [
            {
                "id": "cal-bsm",
                "calibration_type": "Blind Spot Monitor Calibration",
                "determination": "REQUIRED",
                "method": "STATIC",
                "version": 1,
            },
            {
                "id": "cal-radar",
                "calibration_type": "Front Radar Calibration",
                "determination": "REQUIRED",
                "method": "STATIC",
                "version": 1,
            },
        ]
    }
    map_info = {
        "status": "verified",
        "requirements": [
            {"label": "Blind Spot Monitor Calibration", "method": "STATIC"}
        ],
    }

    assert prep._reconciliation_issues(snapshot, map_info) == [
        {"code": "extra_active_item", "calibration": "Front Radar Calibration"}
    ]


def test_reconciliation_parity_rejects_unparseable_active_ciq_requirement():
    snapshot = {
        "calibrations": [
            {
                "id": "cal-bsm",
                "calibration_type": "Blind Spot Monitor Calibration",
                "determination": "REQUIRED",
                "method": "STATIC",
                "version": 1,
            },
            {
                "id": "cal-unparsed",
                "calibration_type": "Calibration",
                "determination": "REQUIRED",
                "method": "UNKNOWN",
                "version": 1,
            },
        ]
    }
    map_info = {
        "status": "verified",
        "requirements": [
            {"label": "Blind Spot Monitor Calibration", "method": "STATIC"}
        ],
    }

    assert prep._reconciliation_issues(snapshot, map_info) == [
        {
            "code": "extra_active_item",
            "calibration": "Calibration",
            "reason": "active CIQ label could not be normalized",
        }
    ]


@pytest.mark.asyncio
async def test_si_coverage_receives_optional_vehicle_identity_from_ciq():
    snapshot = _snapshot("ro-1", "100", "1HGCM82633A004352")
    snapshot["vehicle"].update(
        {
            "trim": "LE",
            "configuration": {"adas_map_model_configuration": "FWD"},
        }
    )
    observed = {}

    class CapturingCatalog:
        @staticmethod
        def requirement_coverage(requirements, **query):
            observed.update(query)
            return {
                "status": artifacts.MISSING,
                "requirements": [
                    {
                        "requirement": requirements[0],
                        "state": artifacts.MISSING,
                        "sources": [],
                    }
                ],
            }

    coverage = await prep._catalog_coverage(
        CapturingCatalog(),
        snapshot,
        {
            "status": "verified",
            "requirements": [{"label": "Occupant Classification System"}],
        },
    )

    assert observed == {
        "ro_number": "100",
        "vin": "1HGCM82633A004352",
        "year": 2020,
        "make": "Example",
        "model": "Model 100",
        "trim": "LE",
        "configuration": "fwd",
    }
    assert coverage[0]["state"] == artifacts.MISSING


def test_weekly_summary_is_bounded_and_reports_three_state_counts():
    rows = [
        {
            "ro_number": str(100 + index),
            "vehicle": f"Vehicle {index}",
            "ready": False,
            "status": "adas_map_unverified",
            "adas_map": {"status": "unverified"},
        }
        for index in range(10)
    ]
    summary = prep.summarize(
        "week_readiness",
        {
            "verified": True,
            "queue_count": 10,
            "ready_count": 0,
            "exception_count": 10,
            "readiness_complete": False,
            "adas_map_verified_count": 7,
            "adas_map_unavailable_count": 3,
            "si_covered_count": 6,
            "si_missing_count": 1,
            "si_unverified_count": 3,
            "alldata_queued_count": 1,
            "repair_orders": rows,
        },
    )
    assert "ADAS Map: 7 verified; 0 genuinely missing; 3 unverified" in summary
    assert "ADAS SI: 6 fully covered; 1 genuinely missing; 3 unverified" in summary
    assert summary.count("\nRO ") == 3
    assert "7 additional RO exception(s)" in summary


def _operator_context() -> dict:
    return {
        "conversation_id": 11,
        "message_id": 22,
        "tool_call_id": "weekly-reconcile",
        "user_id": "owner",
        "role": "owner",
    }


def _research_snapshot(state: str, version: int, calibrations=None) -> dict:
    snapshot = _snapshot("ro-1", "100", "1HGCM82633A004352")
    snapshot["calibrations"] = list(calibrations or [])
    snapshot["research"] = {
        "id": "research-1",
        "state": state,
        "version": version,
    }
    return snapshot


def _completed_receipt(operation: str, after: dict) -> dict:
    return {
        "operation": operation,
        "repair_order_id": "ro-1",
        "status": "completed",
        "success": True,
        "verification": {"verified": True},
        "after": after,
    }


def _failed_receipt(operation: str, code: str) -> dict:
    return {
        "operation": operation,
        "repair_order_id": "ro-1",
        "status": "failed",
        "success": False,
        "verification": {"verified": False},
        "error": {"code": code},
    }


def _operator_result(receipt: dict, snapshot: dict, *, success: bool) -> dict:
    return {
        "status": "success" if success else "failed",
        "executed": True,
        "success": success,
        "verified": success,
        "partial": False,
        "requested_count": 1,
        "processed_count": 1,
        "receipts": [receipt],
        "final_snapshots": {"ro-1": {"status": "verified", "snapshot": snapshot}},
        **({} if success else {"error": receipt.get("error")}),
    }


def _front_camera_map() -> dict:
    return {
        "status": "verified",
        "requirements": [{"label": "Front Camera", "method": "STATIC"}],
        "sources": [],
    }


@pytest.mark.asyncio
async def test_completed_research_reopens_then_rebuilds_and_reconciles_requirements(
    monkeypatch,
):
    initial = _research_snapshot("research_complete", 4)
    reopened = _research_snapshot("research_in_progress", 5)
    calibration = {
        "id": "cal-front-camera",
        "calibration_type": "Front Camera",
        "determination": "REQUIRED",
        "method": "STATIC",
        "version": 1,
    }
    reconciled = _research_snapshot("research_in_progress", 5, [calibration])
    calls: list[dict] = []

    async def execute(_settings, _adas, arguments):
        calls.append(arguments)
        operation = arguments["actions"][0]["operation"]
        if operation == "update_research":
            return _operator_result(
                _completed_receipt(operation, reopened["research"]),
                reopened,
                success=True,
            )
        assert operation == "add_calibration"
        return _operator_result(
            _completed_receipt(operation, calibration), reconciled, success=True
        )

    monkeypatch.setattr(prep.calibration_iq, "operator_execute", execute)
    final, actions, result = await prep._reconcile_one(
        SimpleNamespace(), SimpleNamespace(), initial, _front_camera_map(),
        _operator_context(),
    )

    assert [call["actions"][0]["operation"] for call in calls] == [
        "update_research", "add_calibration"
    ]
    reopen = calls[0]["actions"][0]
    assert reopen["expected_version"] == 4
    assert reopen["arguments"]["state"] == "research_in_progress"
    assert "governing ADAS Map" in reopen["arguments"]["reason"]
    context_key = prep.calibration_iq._INVOCATION_CONTEXT_KEY
    assert calls[0][context_key]["tool_call_id"] != calls[1][context_key]["tool_call_id"]
    assert actions[0]["operation"] == "add_calibration"
    assert final == reconciled
    assert prep._reconciliation_issues(final, _front_camera_map()) == []
    assert result["success"] is result["verified"] is True
    assert result["research_reopened"] is True
    assert result["research_version_before"] == 4
    assert result["research_version_after"] == 5
    assert (result["requested_count"], result["processed_count"], result["verified_count"]) == (2, 2, 2)
    assert [item["operation"] for item in result["receipts"]] == [
        "update_research", "add_calibration"
    ]


@pytest.mark.asyncio
async def test_failed_research_reopen_stops_and_returns_verified_post_state(monkeypatch):
    initial = _research_snapshot("research_complete", 4)
    post_state = _research_snapshot("research_complete", 5)
    calls = 0

    async def execute(_settings, _adas, _arguments):
        nonlocal calls
        calls += 1
        receipt = _failed_receipt("update_research", "version_conflict")
        return _operator_result(receipt, post_state, success=False)

    monkeypatch.setattr(prep.calibration_iq, "operator_execute", execute)
    final, actions, result = await prep._reconcile_one(
        SimpleNamespace(), SimpleNamespace(), initial, _front_camera_map(),
        _operator_context(),
    )

    assert calls == 1
    assert actions[0]["operation"] == "add_calibration"
    assert final == post_state
    assert result["success"] is result["verified"] is False
    assert result["research_reopened"] is False
    assert result["receipts"][0]["error"]["code"] == "version_conflict"


@pytest.mark.asyncio
async def test_failed_reopen_without_final_snapshot_uses_authoritative_fallback(monkeypatch):
    initial = _research_snapshot("research_complete", 4)
    post_state = _research_snapshot("research_complete", 5)
    execute_calls = snapshot_calls = 0

    async def execute(_settings, _adas, _arguments):
        nonlocal execute_calls
        execute_calls += 1
        receipt = _failed_receipt("update_research", "version_conflict")
        result = _operator_result(receipt, post_state, success=False)
        result.pop("final_snapshots")
        return result

    async def reread(_settings, _ro_id):
        nonlocal snapshot_calls
        snapshot_calls += 1
        return {"status": "verified", "snapshot": post_state}

    monkeypatch.setattr(prep.calibration_iq, "operator_execute", execute)
    monkeypatch.setattr(prep.calibration_iq, "operator_snapshot", reread)
    final, _actions, result = await prep._reconcile_one(
        SimpleNamespace(), SimpleNamespace(), initial, _front_camera_map(),
        _operator_context(),
    )

    assert (execute_calls, snapshot_calls) == (1, 1)
    assert final == post_state
    assert result["research_reopened"] is False
    assert result["authoritative_reread"]["status"] == "verified"


@pytest.mark.asyncio
async def test_indeterminate_reopen_response_rereads_state_and_stops(monkeypatch):
    initial = _research_snapshot("research_complete", 4)
    post_state = _research_snapshot("research_in_progress", 5)
    execute_calls = snapshot_calls = 0

    async def execute(_settings, _adas, _arguments):
        nonlocal execute_calls
        execute_calls += 1
        return None

    async def reread(_settings, _ro_id):
        nonlocal snapshot_calls
        snapshot_calls += 1
        return {"status": "verified", "snapshot": post_state}

    monkeypatch.setattr(prep.calibration_iq, "operator_execute", execute)
    monkeypatch.setattr(prep.calibration_iq, "operator_snapshot", reread)
    final, _actions, result = await prep._reconcile_one(
        SimpleNamespace(), SimpleNamespace(), initial, _front_camera_map(),
        _operator_context(),
    )

    assert (execute_calls, snapshot_calls) == (1, 1)
    assert final == post_state
    assert result["status"] == "invalid_response"
    assert result["may_have_executed"] is result["indeterminate"] is True
    assert result["success"] is result["verified"] is False
    assert result["research_reopened"] is False
    assert result["authoritative_reread"]["status"] == "verified"


@pytest.mark.asyncio
async def test_verified_reopen_then_failed_calibration_preserves_partial_truth(monkeypatch):
    initial = _research_snapshot("research_complete", 4)
    reopened = _research_snapshot("research_in_progress", 5)
    calls = 0

    async def execute(_settings, _adas, arguments):
        nonlocal calls
        calls += 1
        operation = arguments["actions"][0]["operation"]
        if calls == 1:
            return _operator_result(
                _completed_receipt(operation, reopened["research"]),
                reopened,
                success=True,
            )
        return _operator_result(
            _failed_receipt(operation, "prerequisite_missing"),
            reopened,
            success=False,
        )

    monkeypatch.setattr(prep.calibration_iq, "operator_execute", execute)
    final, actions, result = await prep._reconcile_one(
        SimpleNamespace(), SimpleNamespace(), initial, _front_camera_map(),
        _operator_context(),
    )

    assert calls == 2
    assert actions[0]["operation"] == "add_calibration"
    assert final == reopened
    assert prep._reconciliation_issues(final, _front_camera_map()) == [
        {"code": "required_item_missing", "calibration": "Front Camera"}
    ]
    assert result["status"] == "partial_success"
    assert result["executed"] is result["partial"] is True
    assert result["success"] is result["verified"] is False
    assert result["research_reopened"] is True
    assert (result["requested_count"], result["processed_count"], result["verified_count"]) == (2, 2, 1)
    assert result["error"]["code"] == "prerequisite_missing"


@pytest.mark.asyncio
async def test_weekly_partial_reopen_does_not_overstate_added_requirements(
    monkeypatch, tmp_path
):
    snapshot = _research_snapshot("research_in_progress", 5)
    reopen_receipt = _completed_receipt("update_research", snapshot["research"])
    failed_receipt = _failed_receipt("add_calibration", "prerequisite_missing")

    async def query(_settings, _filters):
        return {"status": "verified", "items": [{"id": "ro-1", "ro_number": "100", "phase": 5}]}

    async def load(_settings, _identifier):
        return {"status": "verified", "snapshot": snapshot}

    async def discover(_catalog, _snapshot):
        return _front_camera_map()

    async def reconcile(_settings, _adas, current, _map_info, _context):
        action = {"operation": "add_calibration", "repair_order_id": "ro-1", "arguments": {}}
        return current, [action], {
            "status": "partial_success",
            "executed": True,
            "success": False,
            "verified": False,
            "partial": True,
            "requested_count": 2,
            "processed_count": 2,
            "verified_count": 1,
            "receipts": [reopen_receipt, failed_receipt],
            "research_reopened": True,
        }

    monkeypatch.setattr(prep.calibration_iq, "query_repair_orders", query)
    monkeypatch.setattr(prep, "_load_ro_snapshot", load)
    monkeypatch.setattr(prep, "_discover_adas_map", discover)
    monkeypatch.setattr(prep, "_reconcile_one", reconcile)
    monkeypatch.setattr(prep, "_catalog_for", lambda _adas: _Catalog())
    result = await prep._week_readiness(
        SimpleNamespace(root=tmp_path), SimpleNamespace(),
        {"phase": "5", prep._CONTEXT_KEY: _operator_context()},
    )

    assert result["executed"] is True
    assert result["ciq_mutations_processed_count"] == 2
    assert result["ciq_receipt_count"] == 2
    assert result["ciq_verified_receipt_count"] == 1
    assert result["ciq_requirements_added_or_reactivated"] == 0
    assert result["reconciliation_failed_count"] == 1
    row = result["repair_orders"][0]
    assert row["status"] == "reconciliation_failed"
    assert row["reconciliation_issues"] == [
        {"code": "required_item_missing", "calibration": "Front Camera"}
    ]


@pytest.mark.asyncio
async def test_single_ro_planned_reconciliation_without_turn_context_is_not_executed(
    monkeypatch,
):
    snapshot = _snapshot("ro-1", "100", "1HGCM82633A004352")
    snapshot["calibrations"] = []

    async def load(_settings, _identifier):
        return {"status": "verified", "snapshot": snapshot}

    async def discover(_catalog, _snapshot):
        return {
            "status": "verified",
            "requirements": [{"label": "Front Camera", "method": "STATIC"}],
            "sources": [],
        }

    monkeypatch.setattr(prep, "_load_ro_snapshot", load)
    monkeypatch.setattr(prep, "_discover_adas_map", discover)

    result = await prep._ro_requirements(
        SimpleNamespace(), SimpleNamespace(), {"repair_order_id": "100"}
    )

    assert result["reconciliation_actions"][0]["operation"] == "add_calibration"
    assert result["reconciliation"]["status"] == "context_missing"
    assert result["executed"] is False
    assert result["success"] is False
    assert result["verified"] is False
    assert result["snapshot_verified"] is True


@pytest.mark.asyncio
async def test_single_ro_preserves_executed_receipt_when_authoritative_reread_fails(
    monkeypatch,
):
    snapshot = _snapshot("ro-1", "100", "1HGCM82633A004352")
    snapshot["calibrations"] = []

    async def load(_settings, _identifier):
        return {"status": "verified", "snapshot": snapshot}

    async def discover(_catalog, _snapshot):
        return {
            "status": "verified",
            "requirements": [{"label": "Front Camera", "method": "STATIC"}],
            "sources": [],
        }

    async def execute(_settings, _adas, _arguments):
        return {
            "status": "succeeded",
            "executed": True,
            "success": True,
            "verified": True,
            "receipts": [
                {
                    "operation": "add_calibration",
                    "status": "completed",
                    "executed": True,
                    "success": True,
                    "verified": True,
                }
            ],
        }

    async def reread(_settings, _ro_id):
        return {"status": "unavailable", "message": "temporary read failure"}

    monkeypatch.setattr(prep, "_load_ro_snapshot", load)
    monkeypatch.setattr(prep, "_discover_adas_map", discover)
    monkeypatch.setattr(prep.calibration_iq, "operator_execute", execute)
    monkeypatch.setattr(prep.calibration_iq, "operator_snapshot", reread)

    result = await prep._ro_requirements(
        SimpleNamespace(),
        SimpleNamespace(),
        {
            "repair_order_id": "100",
            prep._CONTEXT_KEY: {
                "conversation_id": 1,
                "message_id": 2,
                "tool_call_id": "call-3",
            },
        },
    )

    assert result["executed"] is True
    assert result["success"] is False
    assert result["verified"] is False
    assert result["reconciliation"]["status"] == "verification_failed"
    assert result["reconciliation"]["receipts"][0]["executed"] is True
    assert result["reconciliation"]["authoritative_reread"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_single_ro_duplicate_required_alias_fails_final_parity(monkeypatch):
    snapshot = _snapshot("ro-1", "100", "1HGCM82633A004352")
    snapshot["calibrations"].append(
        {
            **snapshot["calibrations"][0],
            "id": "cal-duplicate",
            "calibration_type": "Seat Weight Sensor Zero Point",
        }
    )

    async def load(_settings, _identifier):
        return {"status": "verified", "snapshot": snapshot}

    async def discover(_catalog, _snapshot):
        return {
            "status": "verified",
            "requirements": [
                {"label": "Occupant Classification System", "method": "UNKNOWN"}
            ],
            "sources": [],
        }

    monkeypatch.setattr(prep, "_load_ro_snapshot", load)
    monkeypatch.setattr(prep, "_discover_adas_map", discover)

    result = await prep._ro_requirements(
        SimpleNamespace(), SimpleNamespace(), {"repair_order_id": "100"}
    )

    assert result["reconciliation_actions"] == []
    assert result["reconciliation_issues"] == [
        {
            "code": "duplicate_active_items",
            "calibration": "Occupant Classification System",
        }
    ]
    assert result["executed"] is False
    assert result["success"] is False
    assert result["verified"] is False
