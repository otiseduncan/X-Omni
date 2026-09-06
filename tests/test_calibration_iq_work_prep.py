from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.services import calibration_iq_weekly_queue as weekly_queue
from core.services import calibration_iq_work_prep as prep
from core.tools import registry as registry_mod


def test_adas_map_is_only_promoted_when_explicitly_marked():
    snapshot = {
        "calibrations": [
            {
                "id": "existing",
                "calibration_type": "Forward camera calibration",
                "determination": "REQUIRED",
            }
        ],
        "assessments": [
            {"confirmed_calibrations": ["Blind Spot Monitor Calibration"]}
        ],
    }
    result = prep.extract_adas_map(snapshot)
    assert result["status"] == "not_found"
    assert result["requirements"] == []


def test_adas_map_nested_payload_extracts_governing_requirements_and_source():
    snapshot = {
        "vehicle": {
            "repair_information": {
                "adas_map": {
                    "provider": "ADAS Map",
                    "url": "https://example.invalid/adas-map/ro-1",
                    "required_calibrations": [
                        {"label": "Blind Spot Monitor Calibration", "method": "STATIC"},
                        {"calibration_type": "Steering Angle Sensor Reset", "method": "DYNAMIC"},
                    ],
                }
            }
        }
    }
    result = prep.extract_adas_map(snapshot)
    assert result["status"] == "verified"
    assert result["governing_source"] == "ADAS Map"
    assert result["requirement_count"] == 2
    labels = {item["label"] for item in result["requirements"]}
    assert labels == {"Blind Spot Monitor Calibration", "Steering Angle Sensor Reset"}
    assert result["sources"][0]["url"] == "https://example.invalid/adas-map/ro-1"


@pytest.mark.asyncio
async def test_discover_adas_map_drops_non_calibration_canonical_labels():
    """The canonical requirements list keeps real requirements and drops noise.

    Seat belt was previously excluded here as an SRS/inspection item on the
    grounds that it had no CIQ calibration_type to reconcile against. That is
    no longer true -- Calibration IQ carries Seat Belt as a REQUIRED
    calibration -- and excluding it meant it silently vanished from every
    requirement set, so its service information was never gathered and an RO
    whose only requirement was a seat belt reported zero requirements and
    could never verify. Rows that are not calibrations at all are still
    filtered, the same way extract_adas_map filters its own snapshot path."""

    class FakeCatalog:
        @staticmethod
        def discover(**_kwargs):
            return {
                "status": "verified",
                "record": {
                    "requirements": [
                        {"label": "Occupant Classification System"},
                        {"label": "Passenger Seat Weight Sensor"},
                        {"label": "Seat Belt"},
                        {"label": "Notes"},
                        {"label": "n/a"},
                    ],
                    "explicit_no_calibration": False,
                },
            }

    result = await prep._discover_adas_map(FakeCatalog(), {})  # noqa: SLF001

    assert result["status"] == "verified"
    labels = {item["label"] for item in result["requirements"]}
    assert labels == {
        "Occupant Classification System",
        "Passenger Seat Weight Sensor",
        "Seat Belt",
    }
    assert "Notes" not in labels and "n/a" not in labels


def test_requirement_identity_collapses_common_oem_label_variants():
    assert prep._calibration_key("Blind Spot Monitor Calibration") == prep._calibration_key("BSM calibration")
    assert prep._calibration_key("Steering Angle Sensor Reset") == prep._calibration_key("steering angle calibration")
    assert prep._calibration_key("Forward Facing Camera Calibration") == prep._calibration_key("windshield camera aiming")


def test_reconciliation_adds_only_missing_and_reactivates_historical_item():
    snapshot = {
        "calibrations": [
            {
                "id": "bsm-existing",
                "calibration_type": "BSM calibration",
                "determination": "REQUIRED",
                "method": "STATIC",
                "version": 2,
            },
            {
                "id": "steering-old",
                "calibration_type": "Steering Angle Sensor Reset",
                "determination": "REMOVED_AFTER_REVIEW",
                "method": "UNKNOWN",
                "version": 4,
            },
        ]
    }
    map_info = {
        "status": "verified",
        "requirements": [
            {"label": "Blind Spot Monitor Calibration", "method": "STATIC"},
            {"label": "Steering Angle Sensor Reset", "method": "STATIC"},
            {"label": "Forward Facing Camera Calibration", "method": "DYNAMIC"},
        ],
    }
    actions = prep.build_reconciliation_actions(snapshot, map_info, "ro-id")
    assert len(actions) == 2
    update = next(item for item in actions if item["operation"] == "update_calibration")
    add = next(item for item in actions if item["operation"] == "add_calibration")
    assert update["target_id"] == "steering-old"
    assert update["expected_version"] == 4
    assert update["arguments"]["determination"] == "REQUIRED"
    assert add["repair_order_id"] == "ro-id"
    assert add["arguments"]["calibration_type"] == "Forward Facing Camera Calibration"
    assert add["arguments"]["determination"] == "REQUIRED"
    assert all("Blind Spot Monitor" not in str(item) for item in actions)


def test_likely_requirement_is_promoted_without_creating_a_duplicate():
    snapshot = {
        "calibrations": [
            {
                "id": "1",
                "calibration_type": "Blind Spot Monitor Calibration",
                "determination": "LIKELY_REQUIRED",
                "method": "UNKNOWN",
                "version": 1,
            }
        ]
    }
    map_info = {
        "status": "verified",
        "requirements": [{"label": "BSM calibration", "method": "STATIC"}],
    }
    assert prep.build_reconciliation_actions(snapshot, map_info, "ro") == [
        {
            "operation": "update_calibration",
            "target_id": "1",
            "expected_version": 1,
            "arguments": {
                "determination": "REQUIRED",
                "research_status": "ADAS Map governing source",
                "method": "STATIC",
            },
        }
    ]


def test_selected_alldata_signal_matches_same_vehicle_not_same_make_only():
    row = {"year": 2023, "make": "Acura", "model": "TLX", "trim": "Type S"}
    assert prep._row_matches_signals(
        row,
        ["Vehicle Information - 2023 Acura TLX Type S AWD V6-3.0L Turbo - ALLDATA Collision"],
    ) is True
    assert prep._row_matches_signals(
        row,
        ["Vehicle Information - 2023 Acura MDX Type S AWD - ALLDATA Collision"],
    ) is False


@pytest.mark.asyncio
async def test_structured_work_prep_si_scan_requests_calibration_depth():
    seen: list[dict] = []

    class FakeAdas:
        @staticmethod
        def search(args):
            seen.append(dict(args))
            return {
                "exact_source_matched": True,
                "results": [
                    {
                        "excerpt": "Front camera aiming procedure",
                        "source_match_score": 10,
                        "relative_path": "OEM/front-camera.pdf",
                    }
                ],
            }

    result = await prep._adas_coverage(  # noqa: SLF001
        FakeAdas(),
        "2024 Ford F-150",
        [{"label": "Front camera calibration"}],
    )

    assert seen == [
        {
            "query": "2024 Ford F-150 Front camera calibration",
            "search_mode": "calibration_requirements",
        }
    ]
    assert result[0]["available"] is True


def test_week_summary_names_each_ro_that_needs_si():
    text = prep.summarize(
        "week_readiness",
        {
            "verified": True,
            "queue_count": 3,
            "ready_count": 1,
            "needs_si_count": 1,
            "adas_map_unavailable_count": 1,
            "ciq_requirements_added_or_reactivated": 1,
            "repair_orders": [
                {"ro_number": "100", "vehicle": "2023 Acura TLX", "ready": True},
                {
                    "ro_number": "101",
                    "vehicle": "2021 Jeep Cherokee",
                    "ready": False,
                    "adas_map": {"status": "verified"},
                    "missing_si": [{"calibration": "BSM calibration"}],
                },
                {
                    "ro_number": "102",
                    "vehicle": "2024 Ford Transit",
                    "ready": False,
                    "adas_map": {"status": "not_found"},
                    "missing_si": [],
                },
            ],
        },
    )
    assert text.startswith("No —")
    assert "RO 101" in text
    assert "BSM calibration" in text
    assert "RO 102" in text
    assert "ADAS Map" in text
    assert "1 requirement(s) added/reactivated" in text


def test_week_summary_bounds_ro_exceptions_and_calibration_labels():
    repair_orders = [
        {
            "ro_number": str(index),
            "vehicle": f"Vehicle {index}",
            "ready": False,
            "adas_map": {"status": "verified"},
            "missing_si": [
                {"calibration": f"Calibration {index}-{calibration}"}
                for calibration in range(1, 6)
            ],
        }
        for index in range(1, 6)
    ]
    text = prep.summarize(
        "week_readiness",
        {
            "verified": True,
            "readiness_complete": False,
            "exception_count": 5,
            "queue_count": 5,
            "ready_count": 0,
            "needs_si_count": 5,
            "adas_map_unavailable_count": 0,
            "reconciliation_failed_count": 0,
            "phase_scope": ["5", "6", "7", "8"],
            "repair_orders": repair_orders,
        },
    )

    assert text.startswith("No — 5 of 5")
    assert "RO 1" in text and "RO 2" in text and "RO 3" in text
    assert "RO 4" not in text and "RO 5" not in text
    assert "Calibration 1-3 (+2 more)" in text
    assert "2 additional RO exception(s)" in text


def test_phase_coverage_summary_answers_yes_directly():
    text = prep.summarize(
        "phase_coverage",
        {
            "verified": True,
            "readiness_complete": True,
            "exception_count": 0,
            "queue_count": 2,
            "ready_count": 2,
            "needs_si_count": 0,
            "adas_map_unavailable_count": 0,
            "reconciliation_failed_count": 0,
            "phase_scope": ["5"],
            "repair_orders": [
                {"ro_number": "100", "vehicle": "Vehicle 1", "ready": True},
                {"ro_number": "101", "vehicle": "Vehicle 2", "ready": True},
            ],
        },
    )
    assert text.startswith(
        "Yes — all 2 active Calibration IQ ROs in Phase 5 are SI-ready."
    )


def test_phase_map_report_question_answers_map_coverage_not_si_readiness():
    text = prep.summarize(
        "phase_coverage",
        {
            "verified": True,
            "coverage_focus": "adas_map",
            "readiness_complete": False,
            "exception_count": 1,
            "queue_count": 2,
            "ready_count": 1,
            "adas_map_verified_count": 2,
            "adas_map_unavailable_count": 0,
            "si_covered_count": 1,
            "si_missing_count": 1,
            "si_unverified_count": 0,
            "phase_scope": ["5"],
            "repair_orders": [
                {
                    "ro_number": "100",
                    "vehicle": "Vehicle 1",
                    "ready": True,
                    "adas_map": {"status": "verified"},
                },
                {
                    "ro_number": "101",
                    "vehicle": "Vehicle 2",
                    "ready": False,
                    "status": "si_missing",
                    "adas_map": {"status": "verified"},
                    "missing_si": [{"calibration": "Front Camera"}],
                },
            ],
        },
    )

    assert text.startswith(
        "Yes — all 2 active Calibration IQ ROs in Phase 5 have verified ADAS Map reports."
    )
    assert "ADAS SI: 1 fully covered; 1 genuinely missing; 0 unverified." in text
    assert "RO 101" not in text


def test_phase_map_report_summary_separates_missing_from_unverified_ros():
    text = prep.summarize(
        "phase_coverage",
        {
            "verified": True,
            "coverage_focus": "adas_map",
            "queue_count": 3,
            "ready_count": 1,
            "adas_map_verified_count": 1,
            "adas_map_missing_count": 1,
            "adas_map_unverified_count": 1,
            "adas_map_unavailable_count": 2,
            "phase_scope": ["5"],
            "repair_orders": [
                {
                    "ro_number": "100",
                    "vehicle": "Vehicle 1",
                    "ready": True,
                    "adas_map": {"status": "verified"},
                },
                {
                    "ro_number": "101",
                    "vehicle": "Vehicle 2",
                    "ready": False,
                    "adas_map": {"status": "not_found"},
                },
                {
                    "ro_number": "102",
                    "vehicle": "Vehicle 3",
                    "ready": False,
                    "adas_map": {"status": "ambiguous"},
                },
            ],
        },
    )

    assert text.startswith(
        "No — 2 of 3 active Calibration IQ ROs in Phase 5 do not have verified ADAS Map reports."
    )
    assert "ADAS Map: 1 verified; 1 genuinely missing; 1 unverified." in text
    assert "RO 101" in text and "genuinely missing" in text
    assert "RO 102" in text and "ambiguous" in text
    assert "RO 100" not in text


def test_work_prep_tool_is_advertised_as_operator_authorized_after_install():
    schema = registry_mod.TOOL_SCHEMAS[prep.TOOL_NAME]
    assert set(schema["parameters"]["properties"]["mode"]["enum"]) == {
        "phase_list",
        "phase_coverage",
        "ro_requirements",
        "week_readiness",
        "queue_list",
        "queue_next",
    }
    assert set(schema["parameters"]["properties"]["statuses"]["items"]["enum"]) == set(
        weekly_queue.LIFECYCLE_STATUSES
    )


def test_work_prep_schema_owns_ciq_field_work_not_calendar_events():
    schema = registry_mod.TOOL_SCHEMAS[prep.TOOL_NAME]
    description = schema["description"].casefold()
    mode_description = schema["parameters"]["properties"]["mode"][
        "description"
    ].casefold()

    assert "authoritative calibration iq source" in description
    assert "upcoming shop field work" in description
    assert "weekly ro readiness" in description
    assert "does not read google calendar appointments or events" in description
    assert "simple ciq board question" in description
    assert "calibration_iq_summary" in description
    assert "authoritative ciq ro workload/readiness operation" in mode_description
    assert "only for a phase explicitly supplied by the user" in mode_description
    phase_description = schema["parameters"]["properties"]["phase"]["description"].casefold()
    assert "current request explicitly names" in phase_description
    assert "never infer" in phase_description


@pytest.mark.asyncio
async def test_queue_list_mode_reports_no_active_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_queue, "_STORE", None)
    settings = SimpleNamespace(root=tmp_path)
    result = await prep._queue_list_mode(  # noqa: SLF001
        settings,
        {prep._CONTEXT_KEY: {"conversation_id": 999, "message_id": 1, "tool_call_id": "call-1"}},  # noqa: SLF001
    )
    assert result["status"] == "no_active_queue"
    assert result["items"] == []


@pytest.mark.asyncio
async def test_queue_list_mode_reports_stale_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_queue, "_STORE", None)
    settings = SimpleNamespace(root=tmp_path)
    store = weekly_queue.get_store(tmp_path)
    item = weekly_queue.WeeklyQueueItem(repair_order_id="ro-1", ro_number="RO1", vehicle_label="2023 Acura TLX")
    old = weekly_queue.WeeklyQueue(conversation_id="7", items=[item])
    old.updated_at = 0.0
    store._write_all({"7": old.to_dict()})  # noqa: SLF001

    result = await prep._queue_list_mode(  # noqa: SLF001
        settings,
        {prep._CONTEXT_KEY: {"conversation_id": 7, "message_id": 1, "tool_call_id": "call-1"}},  # noqa: SLF001
    )
    assert result["status"] == "queue_stale"


@pytest.mark.asyncio
async def test_queue_list_mode_returns_missing_and_unverified_items(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_queue, "_STORE", None)
    settings = SimpleNamespace(root=tmp_path)
    store = weekly_queue.get_store(tmp_path)
    missing_item = weekly_queue.WeeklyQueueItem(
        repair_order_id="ro-1", ro_number="2400612490", vehicle_label="2023 Ford Maverick",
        missing_calibrations=["Passenger Seat Weight Sensor"], category="missing",
    )
    unverified_item = weekly_queue.WeeklyQueueItem(
        repair_order_id="ro-2", ro_number="2400612471", vehicle_label="2026 Chevrolet Equinox",
        unverified_calibrations=["Windshield mono-camera calibration"], category="unverified",
    )
    done_item = weekly_queue.WeeklyQueueItem(
        repair_order_id="ro-3", ro_number="2400612455", vehicle_label="2022 Honda CR-V",
        missing_calibrations=["Seat Belt"], category="missing", status="complete",
    )
    store.save(weekly_queue.WeeklyQueue(
        conversation_id="11", items=[missing_item, unverified_item, done_item],
    ))

    result = await prep._queue_list_mode(  # noqa: SLF001
        settings,
        {prep._CONTEXT_KEY: {"conversation_id": 11, "message_id": 1, "tool_call_id": "call-1"}},  # noqa: SLF001
    )
    assert result["status"] == "success"
    assert result["pending_count"] == 2
    assert result["missing_count"] == 1
    assert result["unverified_count"] == 1
    assert result["done_count"] == 1
    ro_numbers = {item["ro_number"] for item in result["items"]}
    assert ro_numbers == {"2400612490", "2400612471"}
    assert "2400612455" not in ro_numbers  # already complete -- not "pending"

    summary = prep.summarize("queue_list", result)
    assert "2 RO(s) remain unresolved" in summary
    assert "1 confirmed missing" in summary
    assert "1 unverified" in summary


def test_save_weekly_queue_persists_missing_and_unverified_rows_with_category(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_queue, "_STORE", None)
    settings = SimpleNamespace(root=tmp_path)
    results = [
        {
            "repair_order_id": "ro-1", "ro_number": "2400612490", "vehicle": "2023 Ford Maverick",
            "missing_si": [{"calibration": "Passenger Seat Weight Sensor", "state": "MISSING"}],
            "unverified_si": [],
        },
        {
            "repair_order_id": "ro-2", "ro_number": "2400612471", "vehicle": "2026 Chevrolet Equinox",
            "missing_si": [],
            "unverified_si": [{"calibration": "Windshield mono-camera calibration", "state": "UNVERIFIED"}],
        },
        {
            "repair_order_id": "ro-3", "ro_number": "2400612455", "vehicle": "2022 Honda CR-V",
            "missing_si": [], "unverified_si": [],
        },
    ]
    prep._save_weekly_queue(settings, 12, results)  # noqa: SLF001

    store = weekly_queue.get_store(tmp_path)
    queue = store.get("12")
    by_ro = {item.ro_number: item for item in queue.items}
    assert set(by_ro) == {"2400612490", "2400612471"}  # the fully-covered RO is not queued
    assert by_ro["2400612490"].category == "missing"
    assert by_ro["2400612490"].missing_calibrations == ["Passenger Seat Weight Sensor"]
    assert by_ro["2400612471"].category == "unverified"
    assert by_ro["2400612471"].unverified_calibrations == ["Windshield mono-camera calibration"]
def test_row_phase_token_normalizes_numeric_and_string_phases():
    assert prep._row_phase_token({"phase": 5}) == "5"
    assert prep._row_phase_token({"phase": "5.0"}) == "5"
    assert prep._row_phase_token({"phase": "Reassembly"}) == "Reassembly"
    assert prep._row_phase_token({}) is None


@pytest.mark.asyncio
async def test_phase_coverage_mode_runs_the_full_readiness_audit(monkeypatch):
    observed = {}

    async def week_readiness(_settings, _adas, args):
        observed.update(args)
        return {
            "status": "success",
            "mode": "week_readiness",
            "success": True,
            "verified": True,
            "readiness_complete": True,
            "queue_count": 1,
        }

    monkeypatch.setattr(prep, "_week_readiness", week_readiness)
    result = await prep.handle(
        SimpleNamespace(),
        SimpleNamespace(),
        {"mode": "phase_coverage", "phase": "5", "shop": "Macon"},
    )

    assert observed == {"mode": "phase_coverage", "phase": "5", "shop": "Macon"}
    assert result["mode"] == "phase_coverage"
    assert result["verified"] is True


@pytest.mark.asyncio
async def test_phase_coverage_mode_requires_an_explicit_phase():
    result = await prep.handle(
        SimpleNamespace(),
        SimpleNamespace(),
        {"mode": "phase_coverage"},
    )

    assert result["status"] == "invalid_request"
    assert result["success"] is False
    assert result["verified"] is False


@pytest.mark.asyncio
async def test_week_readiness_defaults_to_phase_five_through_eight(monkeypatch):
    rows = [
        {
            "id": f"ro-{phase}",
            "ro_number": f"RO{phase}",
            "phase": str(phase),
            "vehicle": {"year": 2023, "make": "Ford", "model": "F-150"},
        }
        for phase in range(1, 9)
    ]

    async def query_repair_orders(_settings, args):
        assert "phase" not in args
        assert args.get("include_completed") is True
        return {"status": "verified", "items": rows}

    monkeypatch.setattr(prep.calibration_iq, "query_repair_orders", query_repair_orders)

    async def load_snapshot(_settings, identifier):
        return {
            "status": "verified",
            "snapshot": {"calibrations": [], "repair_order": {"id": identifier}},
        }

    monkeypatch.setattr(prep, "_load_ro_snapshot", load_snapshot)

    async def coverage(_adas, _vehicle, _requirements):
        return []

    monkeypatch.setattr(prep, "_adas_coverage", coverage)

    result = await prep._week_readiness(SimpleNamespace(), SimpleNamespace(), {})
    assert result["queue_count"] == 4
    assert {item["ro_number"] for item in result["repair_orders"]} == {
        "RO5",
        "RO6",
        "RO7",
        "RO8",
    }
    assert result["phase_scope"] == ["5", "6", "7", "8"]


@pytest.mark.asyncio
async def test_week_readiness_keeps_completed_rows_still_in_ciq_active_work(
    monkeypatch,
):
    rows = [
        {
            "id": "ro-source-active",
            "ro_number": "RO5",
            "phase": "5",
            "status": "Calibration Complete",
            "source_presence": {"active_on_source": True},
        },
        {
            "id": "ro-source-removed",
            "ro_number": "REMOVED5",
            "phase": "5",
            "status": "Calibration Complete",
            "source_presence": {"active_on_source": False},
        },
    ]

    async def query_repair_orders(_settings, args):
        # Calibration IQ's active collection already applies active_on_source;
        # include_completed prevents X from undoing that authority decision.
        assert args == {"include_completed": True, "phase": "5"}
        return {"status": "verified", "items": rows}

    async def load_snapshot(_settings, identifier):
        return {
            "status": "verified",
            "snapshot": {
                "calibrations": [],
                "repair_order": {"id": identifier, "ro_number": "RO5"},
            },
        }

    monkeypatch.setattr(prep.calibration_iq, "query_repair_orders", query_repair_orders)
    monkeypatch.setattr(prep, "_load_ro_snapshot", load_snapshot)

    result = await prep._week_readiness(
        SimpleNamespace(), SimpleNamespace(), {"phase": "5"}
    )

    assert result["queue_count"] == 1
    assert result["repair_orders"][0]["ro_number"] == "RO5"


@pytest.mark.asyncio
async def test_week_readiness_explicit_phase_overrides_default_scope(monkeypatch):
    row = {"id": "ro-3", "ro_number": "RO3", "phase": "3", "vehicle": {"year": 2023, "make": "Ford", "model": "F-150"}}

    async def query_repair_orders(_settings, args):
        assert args.get("phase") == "3"
        return {"status": "verified", "items": [row]}

    monkeypatch.setattr(prep.calibration_iq, "query_repair_orders", query_repair_orders)

    async def load_snapshot(_settings, identifier):
        return {"status": "verified", "snapshot": {"calibrations": [], "repair_order": {"id": identifier}}}

    monkeypatch.setattr(prep, "_load_ro_snapshot", load_snapshot)

    async def coverage(_adas, _vehicle, _requirements):
        return []

    monkeypatch.setattr(prep, "_adas_coverage", coverage)

    result = await prep._week_readiness(SimpleNamespace(), SimpleNamespace(), {"phase": "3"})
    assert result["queue_count"] == 1
    assert result["phase_scope"] == ["3"]


@pytest.mark.asyncio
async def test_week_readiness_execute_missing_runs_full_active_evidence_flow(monkeypatch):
    row = {
        "id": "ro-active-3",
        "ro_number": "RO-ACTIVE-3",
        "phase": "3",
        "source_presence": {"active_on_source": True},
        "vehicle": {"year": 2024, "make": "Toyota", "model": "Camry"},
    }
    snapshot = {
        "repair_order": {"id": "ro-active-3", "ro_number": "RO-ACTIVE-3"},
        "vehicle": {"year": 2024, "make": "Toyota", "model": "Camry"},
        "calibrations": [],
    }

    async def query_repair_orders(_settings, args):
        assert args == {"include_completed": True}
        return {"status": "verified", "items": [row]}

    async def load_snapshot(_settings, identifier):
        assert identifier == "ro-active-3"
        return {"status": "verified", "snapshot": snapshot}

    discovery_calls = 0

    async def discover_map(_catalog, _snapshot):
        nonlocal discovery_calls
        discovery_calls += 1
        if discovery_calls == 1:
            return {
                "status": "not_found",
                "requirements": [],
                "requirement_count": 0,
            }
        return {
            "status": "verified",
            "requirements": [
                {"label": "Front camera calibration", "method": "STATIC"}
            ],
            "requirement_count": 1,
        }

    async def acquire_map(_settings, current_snapshot):
        # _week_readiness defensively copies the loaded snapshot, so compare
        # the authoritative content rather than object identity.
        assert current_snapshot == snapshot
        return {
            "status": "completed",
            "success": True,
            "verified": True,
            "work_complete": True,
        }

    async def reconcile(_settings, _adas, current, _map_info, _context):
        # The real reconciler leaves CIQ holding exactly the ADAS Map's
        # authoritative requirement set; readiness then proves that set is
        # present, so the returned snapshot must reflect it.
        reconciled = dict(current)
        reconciled["calibrations"] = [
            {
                "calibration_type": "Front camera calibration",
                "determination": "REQUIRED",
                "method": "STATIC",
            }
        ]
        return reconciled, [], None

    coverage_calls = 0

    async def catalog_coverage(_catalog, _snapshot, _map_info):
        nonlocal coverage_calls
        coverage_calls += 1
        state = (
            prep.adas_artifact_catalog.MISSING
            if coverage_calls == 1
            else prep.adas_artifact_catalog.COVERED
        )
        return [{"calibration": "Front camera calibration", "state": state}]

    async def acquire_si(_settings, _adas, current_snapshot, coverage):
        # SI acquisition runs after reconciliation, so it sees the reconciled
        # snapshot for the same authoritative RO rather than the loaded one.
        assert current_snapshot["repair_order"] == snapshot["repair_order"]
        assert coverage[0]["state"] == prep.adas_artifact_catalog.MISSING
        return [
            {
                "topic": "Front camera calibration",
                "verified": True,
                "captured": True,
            }
        ]

    async def link_evidence(_settings, _adas, repair_order_id, context):
        assert repair_order_id == "ro-active-3"
        assert context["conversation_id"] == 77
        return {
            "status": "success",
            "success": True,
            "verified": True,
            "executed": True,
        }

    monkeypatch.setattr(prep.calibration_iq, "query_repair_orders", query_repair_orders)
    monkeypatch.setattr(prep, "_load_ro_snapshot", load_snapshot)
    monkeypatch.setattr(prep, "_discover_adas_map", discover_map)
    monkeypatch.setattr(prep, "_acquire_adas_map_gap", acquire_map)
    monkeypatch.setattr(prep, "_reconcile_one", reconcile)
    monkeypatch.setattr(prep, "_catalog_coverage", catalog_coverage)
    monkeypatch.setattr(prep, "_acquire_si_gaps", acquire_si)
    monkeypatch.setattr(prep, "_link_ro_research_evidence", link_evidence)

    result = await prep._week_readiness(
        SimpleNamespace(root=Path(".")),
        SimpleNamespace(),
        {
            "execute_missing": True,
            prep._CONTEXT_KEY: {  # noqa: SLF001
                "conversation_id": 77,
                "message_id": 88,
                "tool_call_id": "prepare-active",
            },
        },
    )

    assert result["status"] == "success"
    assert result["execute_missing"] is True
    assert result["phase_scope"] == ["active"]
    assert result["queue_count"] == 1
    assert result["ready_count"] == 1
    assert result["readiness_complete"] is True
    assert result["adas_map_acquisition_attempted"] == 1
    assert result["adas_map_acquired_count"] == 1
    assert result["si_acquisition_attempted"] == 1
    assert result["si_acquired_count"] == 1
    assert result["evidence_link_attempted"] == 1
    assert result["evidence_link_verified"] == 1
    assert result["repair_orders"][0]["status"] == "ready"


@pytest.mark.asyncio
async def test_week_readiness_capacity_fails_before_snapshot_or_reconciliation(monkeypatch):
    rows = [
        {
            "id": f"ro-{index}",
            "ro_number": f"RO{index}",
            "phase": "5",
            "source_presence": {"active_on_source": True},
        }
        for index in range(weekly_queue.MAX_QUEUE_ITEMS + 1)
    ]

    async def query_repair_orders(_settings, args):
        assert args == {"include_completed": True}
        return {"status": "verified", "items": rows}

    calls = {"snapshot": 0, "reconcile": 0, "save": 0}

    async def load_snapshot(*_args, **_kwargs):
        calls["snapshot"] += 1
        raise AssertionError("capacity preflight must run before snapshot loading")

    async def reconcile(*_args, **_kwargs):
        calls["reconcile"] += 1
        raise AssertionError("oversized readiness audit must not reconcile")

    def save_queue(*_args, **_kwargs):
        calls["save"] += 1
        raise AssertionError("oversized readiness audit must not persist a queue")

    monkeypatch.setattr(prep.calibration_iq, "query_repair_orders", query_repair_orders)
    monkeypatch.setattr(prep, "_load_ro_snapshot", load_snapshot)
    monkeypatch.setattr(prep, "_reconcile_one", reconcile)
    monkeypatch.setattr(prep, "_save_weekly_queue", save_queue)

    result = await prep._week_readiness(
        SimpleNamespace(),
        SimpleNamespace(),
        {
            prep._CONTEXT_KEY: {  # noqa: SLF001
                "conversation_id": 7,
                "message_id": 11,
                "tool_call_id": "capacity-guard",
            }
        },
    )

    assert result["status"] == "queue_capacity_exceeded"
    assert result["executed"] is False
    assert result["success"] is False
    assert result["verified"] is True
    assert result["readiness_complete"] is False
    assert result["candidate_count"] == weekly_queue.MAX_QUEUE_ITEMS + 1
    assert result["queue_count"] == 0
    assert result["queue_capacity"] == weekly_queue.MAX_QUEUE_ITEMS
    assert result["repair_orders"] == []
    assert calls == {"snapshot": 0, "reconcile": 0, "save": 0}


def test_build_missing_si_actions_maps_missing_and_covered_states():
    snapshot = {
        "repair_order": {"id": "ro-si-actions", "ro_number": "RO-SI-ACTIONS"},
        "calibrations": [
            {
                "id": "cal-missing",
                "calibration_type": "Front long-range radar",
                "determination": "REQUIRED",
            },
            {
                "id": "cal-covered",
                "calibration_type": "Rear corner radar - Left",
                "determination": "REQUIRED",
            },
            {
                "id": "cal-unverified",
                "calibration_type": "Blind spot / corner radar - Right",
                "determination": "REQUIRED",
            },
        ],
    }
    coverage = [
        {
            "calibration": "Front long-range radar",
            "state": prep.adas_artifact_catalog.MISSING,
            "reason": "No supporting document found.",
        },
        {"calibration": "Rear corner radar - Left", "state": prep.adas_artifact_catalog.COVERED},
        {
            "calibration": "Blind spot / corner radar - Right",
            "state": prep.adas_artifact_catalog.UNVERIFIED,
        },
    ]

    actions = prep.build_missing_si_actions(snapshot, coverage, "ro-si-actions")

    assert len(actions) == 2
    create_action = next(a for a in actions if a["operation"] == "create_missing_si_record")
    assert create_action["repair_order_id"] == "ro-si-actions"
    assert create_action["arguments"]["calibration_item_id"] == "cal-missing"
    assert create_action["arguments"]["search_details"] == {
        "reason": "No supporting document found."
    }
    resolve_action = next(a for a in actions if a["operation"] == "resolve_missing_si_record")
    assert resolve_action["arguments"]["calibration_item_id"] == "cal-covered"


@pytest.mark.asyncio
async def test_week_readiness_dispatches_missing_si_actions_when_context_is_present(
    monkeypatch,
):
    row = {
        "id": "ro-si-dispatch",
        "ro_number": "RO-SI-DISPATCH",
        "phase": "5",
        "source_presence": {"active_on_source": True},
    }
    snapshot = {
        "repair_order": {"id": "ro-si-dispatch", "ro_number": "RO-SI-DISPATCH"},
        "calibrations": [
            {
                "id": "cal-si-dispatch",
                "calibration_type": "Front long-range radar",
                "determination": "REQUIRED",
            }
        ],
    }

    async def query_repair_orders(_settings, _args):
        return {"status": "verified", "items": [row]}

    async def load_snapshot(_settings, _identifier):
        return {"status": "verified", "snapshot": snapshot}

    async def discover_map(_catalog, _snapshot):
        return {
            "status": "verified",
            "requirements": [{"label": "Front long-range radar", "method": "STATIC"}],
        }

    async def reconcile(_settings, _adas, current, _map_info, _context):
        return current, [], None

    async def catalog_coverage(_catalog, _snapshot, _map_info):
        return [
            {"calibration": "Front long-range radar", "state": prep.adas_artifact_catalog.MISSING}
        ]

    dispatched_batches: list[dict] = []

    async def operator_execute(_settings, _adas, payload):
        dispatched_batches.append(payload)
        return {
            "success": True,
            "receipts": [{"success": True} for _ in payload["actions"]],
        }

    monkeypatch.setattr(prep.calibration_iq, "query_repair_orders", query_repair_orders)
    monkeypatch.setattr(prep, "_load_ro_snapshot", load_snapshot)
    monkeypatch.setattr(prep, "_discover_adas_map", discover_map)
    monkeypatch.setattr(prep, "_reconcile_one", reconcile)
    monkeypatch.setattr(prep, "_catalog_coverage", catalog_coverage)
    monkeypatch.setattr(prep.calibration_iq, "operator_execute", operator_execute)

    result = await prep._week_readiness(
        SimpleNamespace(),
        SimpleNamespace(),
        {
            "phase": "5",
            prep._CONTEXT_KEY: {  # noqa: SLF001
                "conversation_id": 1,
                "message_id": 1,
                "tool_call_id": "test-missing-si-dispatch",
            },
        },
    )

    assert len(dispatched_batches) == 1
    dispatched_actions = dispatched_batches[0]["actions"]
    assert len(dispatched_actions) == 1
    assert dispatched_actions[0]["operation"] == "create_missing_si_record"
    assert dispatched_actions[0]["arguments"]["calibration_item_id"] == "cal-si-dispatch"
    assert result["missing_si_records_dispatched"] == 1
    assert result["missing_si_records_dispatch_errors"] == 0


@pytest.mark.asyncio
async def test_week_readiness_preserves_requested_indeterminate_mutation_truth(
    monkeypatch,
):
    row = {
        "id": "ro-indeterminate",
        "ro_number": "RO-INDETERMINATE",
        "phase": "5",
        "source_presence": {"active_on_source": True},
    }
    snapshot = {
        "repair_order": {"id": "ro-indeterminate", "ro_number": "RO-INDETERMINATE"},
        "calibrations": [],
    }

    async def query_repair_orders(_settings, _args):
        return {"status": "verified", "items": [row]}

    async def load_snapshot(_settings, _identifier):
        return {"status": "verified", "snapshot": snapshot}

    async def discover_map(_catalog, _snapshot):
        return {
            "status": "verified",
            "requirements": [
                {"label": "Front camera calibration", "method": "STATIC"}
            ],
        }

    async def reconcile(_settings, _adas, current, _map_info, _context):
        return current, [{"operation": "add_calibration"}], {
            "status": "invalid_response",
            "executed": True,
            "success": False,
            "verified": False,
            "partial": False,
            "requested_count": 1,
            "processed_count": 0,
            "receipts": [],
            "indeterminate": True,
            "may_have_executed": True,
            "message": "The CIQ transport ended before a receipt was returned.",
        }

    async def catalog_coverage(_catalog, _snapshot, _map_info):
        return []

    monkeypatch.setattr(prep.calibration_iq, "query_repair_orders", query_repair_orders)
    monkeypatch.setattr(prep, "_load_ro_snapshot", load_snapshot)
    monkeypatch.setattr(prep, "_discover_adas_map", discover_map)
    monkeypatch.setattr(prep, "_reconcile_one", reconcile)
    monkeypatch.setattr(prep, "_catalog_coverage", catalog_coverage)

    result = await prep._week_readiness(
        SimpleNamespace(), SimpleNamespace(), {"phase": "5"}
    )

    assert result["executed"] is True
    assert result["ciq_mutations_requested_count"] == 1
    assert result["ciq_mutations_processed_count"] == 0
    assert result["ciq_receipt_count"] == 0
    assert result["ciq_verified_receipt_count"] == 0
    assert result["ciq_indeterminate_reconciliation_count"] == 1
    assert result["ciq_may_have_executed_reconciliation_count"] == 1
    assert result["reconciliation_failed_count"] == 1


@pytest.mark.asyncio
async def test_week_readiness_queue_write_failure_retains_all_ciq_receipt_truth(
    monkeypatch,
):
    row = {
        "id": "ro-persistence-failure",
        "ro_number": "RO-PERSISTENCE-FAILURE",
        "phase": "5",
        "source_presence": {"active_on_source": True},
    }
    snapshot = {
        "repair_order": {
            "id": "ro-persistence-failure",
            "ro_number": "RO-PERSISTENCE-FAILURE",
        },
        "calibrations": [],
    }

    async def query_repair_orders(_settings, _args):
        return {"status": "verified", "items": [row]}

    async def load_snapshot(_settings, _identifier):
        return {"status": "verified", "snapshot": snapshot}

    async def discover_map(_catalog, _snapshot):
        return {
            "status": "verified",
            "requirements": [
                {"label": "Front camera calibration", "method": "STATIC"}
            ],
        }

    receipts = [
        {
            "status": "completed",
            "success": True,
            "verification": {"verified": True},
            "operation": "add_calibration",
            "mutation_id": "mutation-verified",
        },
        {
            "status": "failed",
            "success": False,
            "verification": {"verified": False},
            "operation": "update_calibration",
            "mutation_id": "mutation-indeterminate",
            "indeterminate": True,
            "may_have_executed": True,
        },
    ]

    async def reconcile(_settings, _adas, current, _map_info, _context):
        return current, [{"operation": "add_calibration"}], {
            "status": "partial_success",
            "executed": True,
            "success": False,
            "verified": False,
            "requested_count": 2,
            "processed_count": 2,
            "receipts": receipts,
            "indeterminate": True,
            "may_have_executed": True,
        }

    async def catalog_coverage(_catalog, _snapshot, _map_info):
        return [
            {
                "calibration": "Front camera calibration",
                "state": "MISSING",
            }
        ]

    def fail_queue_write(*_args, **_kwargs):
        raise OSError("injected atomic replace failure")

    monkeypatch.setattr(prep.calibration_iq, "query_repair_orders", query_repair_orders)
    monkeypatch.setattr(prep, "_load_ro_snapshot", load_snapshot)
    monkeypatch.setattr(prep, "_discover_adas_map", discover_map)
    monkeypatch.setattr(prep, "_reconcile_one", reconcile)
    monkeypatch.setattr(prep, "_catalog_coverage", catalog_coverage)
    monkeypatch.setattr(prep, "_save_weekly_queue", fail_queue_write)

    result = await prep._week_readiness(
        SimpleNamespace(),
        SimpleNamespace(),
        {
            "phase": "5",
            prep._CONTEXT_KEY: {  # noqa: SLF001
                "conversation_id": 7,
                "message_id": 12,
                "tool_call_id": "persistence-failure",
            },
        },
    )

    assert result["status"] == "partial_success"
    assert result["success"] is False
    assert result["verified"] is True
    assert result["executed"] is True
    assert result["queue_persistence_status"] == "queue_persistence_error"
    assert result["queue_persistence_verified"] is False
    assert result["queue_persistence_error"] == {
        "code": "queue_persistence_error",
        "exception_type": "OSError",
        "message": (
            "The readiness audit completed, but the derived weekly queue "
            "could not be persisted locally."
        ),
    }
    assert result["acquisition_status"] == "queue_persistence_error"
    assert result["alldata_queued_count"] == 0
    assert "CIQ may already have changed" in result["message"]
    assert result["ciq_mutations_requested_count"] == 2
    assert result["ciq_mutations_processed_count"] == 2
    assert result["ciq_receipt_count"] == 2
    assert result["ciq_verified_receipt_count"] == 1
    assert result["ciq_indeterminate_reconciliation_count"] == 1
    assert result["ciq_may_have_executed_reconciliation_count"] == 1


def test_weekly_queue_round_trips_through_dict():
    item = weekly_queue.WeeklyQueueItem(
        repair_order_id="ro-1", ro_number="RO1", vehicle_label="2023 Acura TLX",
        vehicle_year="2023", vehicle_make="Acura", vehicle_model_trim="TLX",
        missing_calibrations=["BSM calibration"],
    )
    original = weekly_queue.WeeklyQueue(conversation_id="42", items=[item])
    restored = weekly_queue.WeeklyQueue.from_dict(original.to_dict())
    assert restored.conversation_id == "42"
    assert restored.items[0].repair_order_id == "ro-1"
    assert restored.items[0].missing_calibrations == ["BSM calibration"]
    assert restored.pending() == restored.items


def test_weekly_queue_staleness_is_time_based():
    stale = weekly_queue.WeeklyQueue(conversation_id="1", updated_at=0.0)
    assert stale.is_stale(now=weekly_queue.STALE_AFTER_SECONDS + 1) is True
    fresh = weekly_queue.WeeklyQueue(conversation_id="1", updated_at=1000.0)
    assert fresh.is_stale(now=1500.0) is False


@pytest.mark.asyncio
async def test_resolve_queue_next_without_an_active_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_queue, "_STORE", None)
    settings = SimpleNamespace(root=tmp_path)
    result = await prep.resolve_queue_next(settings, SimpleNamespace(), 999)
    assert result["status"] == "no_active_queue"
    assert result["success"] is False


@pytest.mark.asyncio
async def test_resolve_queue_next_reports_stale_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_queue, "_STORE", None)
    settings = SimpleNamespace(root=tmp_path)
    store = weekly_queue.get_store(tmp_path)
    item = weekly_queue.WeeklyQueueItem(repair_order_id="ro-1", ro_number="RO1", vehicle_label="2023 Acura TLX")
    old = weekly_queue.WeeklyQueue(conversation_id="7", items=[item])
    old.updated_at = 0.0
    # Bypass save()'s auto-touch so the persisted record is genuinely old.
    store._write_all({"7": old.to_dict()})  # noqa: SLF001

    result = await prep.resolve_queue_next(settings, SimpleNamespace(), 7)
    assert result["status"] == "queue_stale"


@pytest.mark.asyncio
async def test_resolve_selected_alldata_to_ciq_reads_through_navigator(
    tmp_path, monkeypatch
):
    settings = SimpleNamespace(root=tmp_path)

    async def navigator_current_page_signals(_settings, provider):
        assert provider == "alldata"
        return {
            "success": True,
            "data": {
                "authenticated": True,
                "signals": ["Vehicle Information - 2023 Acura TLX Type S - ALLDATA Collision"],
            },
        }

    monkeypatch.setattr(
        prep.scrapex_svc, "navigator_current_page_signals", navigator_current_page_signals
    )

    async def query_repair_orders(_settings, _args):
        return {
            "status": "verified",
            "items": [
                {"id": "ro-1", "ro_number": "RO1", "vehicle": {"year": 2023, "make": "Acura", "model": "TLX"}},
            ],
        }

    monkeypatch.setattr(prep.calibration_iq, "query_repair_orders", query_repair_orders)

    result = await prep.resolve_selected_alldata_to_ciq(settings, SimpleNamespace())
    assert result["status"] == "verified"
    assert result["verified"] is True
    assert result["ro_number"] == "RO1"


@pytest.mark.asyncio
async def test_resolve_selected_alldata_to_ciq_requires_navigator_authentication(
    tmp_path, monkeypatch
):
    settings = SimpleNamespace(root=tmp_path)

    async def navigator_current_page_signals(_settings, _provider):
        return {"success": True, "data": {"authenticated": False, "signals": []}}

    monkeypatch.setattr(
        prep.scrapex_svc, "navigator_current_page_signals", navigator_current_page_signals
    )

    result = await prep.resolve_selected_alldata_to_ciq(settings, SimpleNamespace())
    assert result["status"] == "human_action_required"
    assert result["verified"] is False


@pytest.mark.asyncio
async def test_resolve_queue_next_selected_vehicle_not_in_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_queue, "_STORE", None)
    settings = SimpleNamespace(root=tmp_path)
    store = weekly_queue.get_store(tmp_path)
    item = weekly_queue.WeeklyQueueItem(
        repair_order_id="ro-1", ro_number="RO1", vehicle_label="2023 Acura TLX",
        vehicle_year="2023", vehicle_make="Acura", vehicle_model_trim="TLX",
    )
    store.save(weekly_queue.WeeklyQueue(conversation_id="8", items=[item]))

    async def navigator_current_page_signals(_settings, provider):
        assert provider == "alldata"
        return {
            "success": True,
            "data": {
                "authenticated": True,
                "signals": [
                    "Vehicle Information - 2019 Toyota Camry LE - ALLDATA Collision"
                ],
            },
        }

    monkeypatch.setattr(
        prep.scrapex_svc,
        "navigator_current_page_signals",
        navigator_current_page_signals,
    )

    result = await prep.resolve_queue_next(settings, SimpleNamespace(), 8)
    assert result["status"] == "not_in_queue"


@pytest.mark.asyncio
async def test_resolve_queue_next_reads_through_navigator(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_queue, "_STORE", None)
    settings = SimpleNamespace(root=tmp_path)
    store = weekly_queue.get_store(tmp_path)
    item = weekly_queue.WeeklyQueueItem(
        repair_order_id="ro-1", ro_number="RO1", vehicle_label="2023 Acura TLX",
        vehicle_year="2023", vehicle_make="Acura", vehicle_model_trim="TLX",
    )
    store.save(weekly_queue.WeeklyQueue(conversation_id="11", items=[item]))

    async def navigator_current_page_signals(_settings, provider):
        assert provider == "alldata"
        return {
            "success": True,
            "data": {
                "authenticated": True,
                "signals": ["Vehicle Information - 2023 Acura TLX Type S - ALLDATA Collision"],
            },
        }

    monkeypatch.setattr(
        prep.scrapex_svc, "navigator_current_page_signals", navigator_current_page_signals
    )

    async def collect(_settings, _adas, args):
        assert args["repair_order_id"] == "ro-1"
        return {"status": "success", "success": True, "verified": True, "vehicle": "2023 Acura TLX"}

    monkeypatch.setattr(prep.quick, "collect_for_calibration_iq_ro", collect)

    result = await prep.resolve_queue_next(settings, SimpleNamespace(), 11)
    assert result["status"] == "success"
    assert result["repair_order_id"] == "ro-1"


@pytest.mark.asyncio
async def test_resolve_queue_next_fails_closed_on_ambiguous_match(tmp_path, monkeypatch):
    # Two identical vehicle models both queued this week -- never guess.
    monkeypatch.setattr(weekly_queue, "_STORE", None)
    settings = SimpleNamespace(root=tmp_path)
    store = weekly_queue.get_store(tmp_path)
    first = weekly_queue.WeeklyQueueItem(
        repair_order_id="ro-1", ro_number="RO1", vehicle_label="2023 Ford F-150",
        vehicle_year="2023", vehicle_make="Ford", vehicle_model_trim="F-150",
    )
    second = weekly_queue.WeeklyQueueItem(
        repair_order_id="ro-2", ro_number="RO2", vehicle_label="2023 Ford F-150",
        vehicle_year="2023", vehicle_make="Ford", vehicle_model_trim="F-150",
    )
    store.save(weekly_queue.WeeklyQueue(conversation_id="10", items=[first, second]))

    async def navigator_current_page_signals(_settings, provider):
        assert provider == "alldata"
        return {
            "success": True,
            "data": {
                "authenticated": True,
                "signals": [
                    "Vehicle Information - 2023 Ford F-150 4WD - ALLDATA Collision"
                ],
            },
        }

    monkeypatch.setattr(
        prep.scrapex_svc,
        "navigator_current_page_signals",
        navigator_current_page_signals,
    )

    collected = False

    async def collect(*_args, **_kwargs):
        nonlocal collected
        collected = True
        return {"status": "success", "success": True, "verified": True}

    monkeypatch.setattr(prep.quick, "collect_for_calibration_iq_ro", collect)

    result = await prep.resolve_queue_next(settings, SimpleNamespace(), 10)
    assert result["status"] == "ambiguous_match"
    assert collected is False
    reloaded = store.get("10")
    assert all(item.status == weekly_queue.STATUS_QUEUED for item in reloaded.items)


@pytest.mark.asyncio
async def test_resolve_queue_next_collects_marks_complete_and_names_the_next_vehicle(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_queue, "_STORE", None)
    settings = SimpleNamespace(root=tmp_path)
    store = weekly_queue.get_store(tmp_path)
    first = weekly_queue.WeeklyQueueItem(
        repair_order_id="ro-1", ro_number="RO1", vehicle_label="2023 Acura TLX",
        vehicle_year="2023", vehicle_make="Acura", vehicle_model_trim="TLX",
        missing_calibrations=["Forward camera calibration"],
    )
    second = weekly_queue.WeeklyQueueItem(
        repair_order_id="ro-2", ro_number="RO2", vehicle_label="2021 Jeep Cherokee",
        vehicle_year="2021", vehicle_make="Jeep", vehicle_model_trim="Cherokee",
        missing_calibrations=["BSM calibration"],
    )
    store.save(weekly_queue.WeeklyQueue(conversation_id="9", items=[first, second]))

    async def navigator_current_page_signals(_settings, provider):
        assert provider == "alldata"
        return {
            "success": True,
            "data": {
                "authenticated": True,
                "signals": [
                    "Vehicle Information - 2023 Acura TLX Type S - ALLDATA Collision"
                ],
            },
        }

    monkeypatch.setattr(
        prep.scrapex_svc,
        "navigator_current_page_signals",
        navigator_current_page_signals,
    )

    async def collect(_settings, _adas, args):
        assert args["repair_order_id"] == "ro-1"
        return {"status": "success", "success": True, "verified": True, "vehicle": "2023 Acura TLX"}

    monkeypatch.setattr(prep.quick, "collect_for_calibration_iq_ro", collect)

    result = await prep.resolve_queue_next(settings, SimpleNamespace(), 9)
    assert result["status"] == "success"
    assert result["repair_order_id"] == "ro-1"
    assert result["done_count"] == 1
    assert result["total_count"] == 2
    assert "RO2" in result["message"]
    assert "Jeep Cherokee" in result["message"]

    reloaded = store.get("9")
    assert reloaded.items[0].status == weekly_queue.STATUS_COMPLETED
    assert reloaded.items[1].status == weekly_queue.STATUS_QUEUED


@pytest.mark.asyncio
async def test_queue_list_structured_status_filter_reports_which_rows_could_not_finish(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(weekly_queue, "_STORE", None)
    settings = SimpleNamespace(root=tmp_path)
    store = weekly_queue.get_store(tmp_path)
    store.save(
        weekly_queue.WeeklyQueue(
            conversation_id="91",
            items=[
                weekly_queue.WeeklyQueueItem(repair_order_id="queued", ro_number="Q", status="queued"),
                weekly_queue.WeeklyQueueItem(
                    repair_order_id="auth", ro_number="A", status="authentication_required", last_error="Sign in"
                ),
                weekly_queue.WeeklyQueueItem(
                    repair_order_id="retry", ro_number="R", status="retryable", last_error="Timed out"
                ),
                weekly_queue.WeeklyQueueItem(
                    repair_order_id="blocked", ro_number="B", status="blocked", last_error="No exact match"
                ),
                weekly_queue.WeeklyQueueItem(repair_order_id="done", ro_number="D", status="completed"),
            ],
        )
    )

    result = await prep._queue_list_mode(  # noqa: SLF001
        settings,
        {
            prep._CONTEXT_KEY: {"conversation_id": 91, "message_id": 1, "tool_call_id": "call-1"},  # noqa: SLF001
            "statuses": ["authentication_required", "retryable", "blocked"],
        },
    )

    assert result["verified"] is True
    assert result["failure_count"] == 3
    assert result["unresolved_count"] == 4
    assert {item["repair_order_id"] for item in result["items"]} == {"auth", "retry", "blocked"}
    assert all(item["last_error"] for item in result["items"])
    assert result["status_counts"]["completed"] == 1
    assert "3 RO(s) could not finish" in prep.summarize("queue_list", result)


@pytest.mark.asyncio
async def test_stale_queue_list_retains_items_for_failure_reporting(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_queue, "_STORE", None)
    settings = SimpleNamespace(root=tmp_path)
    store = weekly_queue.get_store(tmp_path)
    old = weekly_queue.WeeklyQueue(
        conversation_id="92",
        items=[weekly_queue.WeeklyQueueItem(repair_order_id="blocked", status="blocked")],
        updated_at=0.0,
    )
    store._write_all({"92": old.to_dict()})  # noqa: SLF001

    result = await prep._queue_list_mode(  # noqa: SLF001
        settings,
        {
            prep._CONTEXT_KEY: {"conversation_id": 92, "message_id": 1, "tool_call_id": "call-1"},  # noqa: SLF001
            "statuses": ["blocked"],
        },
    )

    assert result["status"] == "queue_stale"
    assert result["stale"] is True
    assert [item["repair_order_id"] for item in result["items"]] == ["blocked"]


@pytest.mark.asyncio
async def test_queue_next_persists_running_attempt_then_retryable_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_queue, "_STORE", None)
    settings = SimpleNamespace(root=tmp_path)
    store = weekly_queue.get_store(tmp_path)
    item = weekly_queue.WeeklyQueueItem(
        repair_order_id="ro-1",
        ro_number="RO1",
        vehicle_label="2023 Acura TLX",
        vehicle_year="2023",
        vehicle_make="Acura",
        vehicle_model_trim="TLX",
    )
    store.save(weekly_queue.WeeklyQueue(conversation_id="93", items=[item]))

    async def navigator_current_page_signals(_settings, provider):
        assert provider == "alldata"
        return {
            "success": True,
            "data": {
                "authenticated": True,
                "signals": [
                    "Vehicle Information - 2023 Acura TLX Type S - ALLDATA Collision"
                ],
            },
        }

    monkeypatch.setattr(
        prep.scrapex_svc,
        "navigator_current_page_signals",
        navigator_current_page_signals,
    )

    async def collect(_settings, _adas, _args):
        in_flight = store.get("93").items[0]
        assert in_flight.status == weekly_queue.STATUS_RUNNING
        assert in_flight.attempts == 1
        return {
            "status": "partial_success",
            "success": False,
            "verified": False,
            "message": "One document timed out.",
        }

    monkeypatch.setattr(prep.quick, "collect_for_calibration_iq_ro", collect)

    result = await prep.resolve_queue_next(settings, SimpleNamespace(), 93)
    assert result["status"] == weekly_queue.STATUS_RETRYABLE
    assert result["item_status"] == weekly_queue.STATUS_RETRYABLE
    assert result["attempts"] == 1
    assert result["failure_count"] == 1
    reloaded = store.get("93").items[0]
    assert reloaded.status == weekly_queue.STATUS_RETRYABLE
    assert reloaded.attempts == 1
    assert reloaded.last_error == "One document timed out."


@pytest.mark.asyncio
async def test_queue_next_never_calls_blocked_only_queue_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_queue, "_STORE", None)
    settings = SimpleNamespace(root=tmp_path)
    store = weekly_queue.get_store(tmp_path)
    store.save(
        weekly_queue.WeeklyQueue(
            conversation_id="94",
            items=[weekly_queue.WeeklyQueueItem(repair_order_id="ro-1", status="blocked")],
        )
    )

    result = await prep.resolve_queue_next(settings, SimpleNamespace(), 94)

    assert result["status"] == "queue_blocked"
    assert result["verified"] is True
    assert result["unresolved_count"] == 1
    assert result["items"][0]["status"] == weekly_queue.STATUS_BLOCKED


def test_repeated_weekly_audit_preserves_same_failure_but_requeues_changed_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_queue, "_STORE", None)
    settings = SimpleNamespace(root=tmp_path)
    store = weekly_queue.get_store(tmp_path)
    prior = weekly_queue.WeeklyQueueItem(
        repair_order_id="ro-1",
        ro_number="RO1",
        vehicle_label="2023 Acura TLX",
        missing_calibrations=["Forward camera"],
        status="blocked",
        attempts=2,
        last_error="No exact procedure",
    )
    store.save(weekly_queue.WeeklyQueue(conversation_id="95", items=[prior]))

    base = {
        "repair_order_id": "ro-1",
        "ro_number": "RO1",
        "vehicle": "2023 Acura TLX",
        "missing_si": [{"calibration": "Forward camera"}],
        "unverified_si": [],
    }
    prep._save_weekly_queue(settings, 95, [base])  # noqa: SLF001
    retained = store.get("95").items[0]
    assert retained.status == weekly_queue.STATUS_BLOCKED
    assert retained.attempts == 2
    assert retained.last_error == "No exact procedure"

    changed = {**base, "missing_si": [{"calibration": "Blind spot monitor"}]}
    prep._save_weekly_queue(settings, 95, [changed])  # noqa: SLF001
    requeued = store.get("95").items[0]
    assert requeued.status == weekly_queue.STATUS_QUEUED
    assert requeued.attempts == 0
    assert requeued.last_error == ""


@pytest.mark.asyncio
async def test_week_readiness_reacquires_a_map_that_cannot_verify(monkeypatch):
    """An unverified ADAS Map is a gap, not a settled state.

    The artifact catalog only accepts ScrapeX provenance at the current
    contract version, so a map captured before it -- or imported into
    Calibration IQ without ScrapeX at all -- can never verify by itself.
    Acquiring only on "not_found" left ten of fourteen Repair Plan ROs
    reporting adas_map_unverified indefinitely, and since SI acquisition
    runs only for a verified map, their service information was never
    gathered either.
    """
    row = {
        "id": "ro-stale",
        "ro_number": "RO-STALE",
        "phase": "1",
        "vehicle": {"year": 2016, "make": "Lexus", "model": "ES 350"},
    }

    async def query_repair_orders(_settings, _args):
        return {"status": "verified", "items": [row]}

    async def load_snapshot(_settings, identifier):
        return {
            "status": "verified",
            "snapshot": {"calibrations": [], "repair_order": {"id": identifier}},
        }

    discoveries = ["unverified", "verified"]

    async def discover_map(_catalog, _snapshot):
        status = discoveries.pop(0) if discoveries else "verified"
        return {"status": status, "requirements": [], "requirement_count": 0}

    acquired: list[str] = []

    async def acquire_map(_settings, _snapshot):
        acquired.append("called")
        return {
            "status": "completed",
            "success": True,
            "verified": True,
            "work_complete": True,
        }

    async def reconcile(_settings, _adas, current, _map_info, _context):
        return current, [], None

    async def coverage(_catalog, _snapshot, _map_info):
        return []

    monkeypatch.setattr(prep.calibration_iq, "query_repair_orders", query_repair_orders)
    monkeypatch.setattr(prep, "_load_ro_snapshot", load_snapshot)
    monkeypatch.setattr(prep, "_discover_adas_map", discover_map)
    monkeypatch.setattr(prep, "_acquire_adas_map_gap", acquire_map)
    monkeypatch.setattr(prep, "_reconcile_one", reconcile)
    monkeypatch.setattr(prep, "_catalog_coverage", coverage)

    result = await prep._week_readiness(
        SimpleNamespace(), SimpleNamespace(), {"phase": "1", "execute_missing": True}
    )

    assert acquired == ["called"]
    assert result["adas_map_acquisition_attempted"] == 1
    assert result["adas_map_acquired_count"] == 1


@pytest.mark.asyncio
async def test_week_readiness_leaves_an_ambiguous_map_for_an_operator(monkeypatch):
    """Provenance that contradicts CIQ identity is never re-pulled blind."""
    row = {
        "id": "ro-ambiguous",
        "ro_number": "RO-AMBIG",
        "phase": "1",
        "vehicle": {"year": 2016, "make": "Lexus", "model": "ES 350"},
    }

    async def query_repair_orders(_settings, _args):
        return {"status": "verified", "items": [row]}

    async def load_snapshot(_settings, identifier):
        return {
            "status": "verified",
            "snapshot": {"calibrations": [], "repair_order": {"id": identifier}},
        }

    async def discover_map(_catalog, _snapshot):
        return {"status": "ambiguous", "requirements": [], "requirement_count": 0}

    async def must_not_acquire(*_args, **_kwargs):
        raise AssertionError("an ambiguous artifact must not be re-acquired blind")

    async def reconcile(_settings, _adas, current, _map_info, _context):
        return current, [], None

    async def coverage(_catalog, _snapshot, _map_info):
        return []

    monkeypatch.setattr(prep.calibration_iq, "query_repair_orders", query_repair_orders)
    monkeypatch.setattr(prep, "_load_ro_snapshot", load_snapshot)
    monkeypatch.setattr(prep, "_discover_adas_map", discover_map)
    monkeypatch.setattr(prep, "_acquire_adas_map_gap", must_not_acquire)
    monkeypatch.setattr(prep, "_reconcile_one", reconcile)
    monkeypatch.setattr(prep, "_catalog_coverage", coverage)

    result = await prep._week_readiness(
        SimpleNamespace(), SimpleNamespace(), {"phase": "1", "execute_missing": True}
    )
    assert result["adas_map_acquisition_attempted"] == 0


def test_truncated_ciq_model_does_not_contradict_the_full_adas_map_model():
    """Schedule imports store the model cut short; that is not a conflict.

    Calibration IQ held "Explorer Activ..." while ADAS Map carried the full
    "Explorer Active RWD". Comparing them verbatim reported an identity
    contradiction for vehicles that plainly match, parking the artifact as
    ambiguous so readiness never acquired SI for those repair orders.
    """
    def conflicts(ciq_model: str, adas_model: str) -> list[str]:
        return prep._artifact_identity_conflicts(  # noqa: SLF001
            {
                "repair_order": {"id": "ro-1"},
                "vehicle": {"year": 2025, "make": "Ford", "model": ciq_model},
            },
            {
                "ciq_ro_id": "ro-1",
                "vehicle": {"year": 2025, "make": "Ford", "model": adas_model},
            },
        )

    assert conflicts("Explorer Activ...", "Explorer Active RWD") == []
    assert conflicts("Santa Fe Limit…", "Santa Fe Limited AWD w/6-Passenger") == []
    # A complete value still has to match exactly, and a truncated prefix that
    # genuinely disagrees is still a conflict.
    assert conflicts("Explorer Active RWD", "Explorer Active AWD") == ["model"]
    assert conflicts("Explorer Activ...", "Escape Titanium") == ["model"]


def test_seat_belt_is_a_calibration_requirement():
    """The label gate must admit everything the family vocabulary knows.

    _looks_like_calibration_label runs before _calibration_key, so anything it
    rejects never reaches the family logic. "Seat Belt" already had a seatbelt
    family yet was rejected here, so it vanished from every requirement set and
    an RO whose only requirement was a seat belt reported zero requirements and
    could never verify.
    """
    for label in (
        "Seat Belt",
        "Seat Belt Inspection",
        "Passenger Seat Weight Sensor",
        "Occupant Classification System",
        "Front Camera",
        "Blind Spot Monitor",
        "Steering Angle Sensor",
        "Surround View Camera",
        "Millimeter Wave Radar Sensor",
    ):
        assert prep._looks_like_calibration_label(label), label  # noqa: SLF001
        assert prep._calibration_key(label), label  # noqa: SLF001

    for junk in ("hello there", "n/a", ""):
        assert not prep._looks_like_calibration_label(junk)  # noqa: SLF001


def test_configuration_compares_the_bare_value_not_the_combined_one():
    """CIQ stores both the bare and the model-prefixed configuration.

    Reading the combined "Mustang Premium Fastback w/EcoBoost" and comparing it
    against ADAS Map's bare "Premium Fastback w/EcoBoost" reported a
    contradiction for a vehicle that matches exactly, parking a freshly
    acquired map as ambiguous.
    """
    def conflicts(expected_config, observed_config):
        return prep._artifact_identity_conflicts(  # noqa: SLF001
            {
                "repair_order": {"id": "ro-1"},
                "vehicle": {
                    "year": 2016,
                    "make": "Ford",
                    "model": "Mustang",
                    "configuration": expected_config,
                },
            },
            {
                "ciq_ro_id": "ro-1",
                "vehicle": {
                    "year": 2016,
                    "make": "Ford",
                    "model": "Mustang",
                    "configuration": observed_config,
                },
            },
        )

    combined = {
        "adas_map_configuration": "Premium Fastback w/EcoBoost",
        "adas_map_model_configuration": "Mustang Premium Fastback w/EcoBoost",
    }
    assert conflicts(combined, "Premium Fastback w/EcoBoost") == []
    # A configuration that genuinely differs is still a conflict.
    assert conflicts(combined, "GT Convertible") == ["configuration"]


@pytest.mark.asyncio
async def test_readiness_is_the_map_handoff_and_does_not_wait_on_si(monkeypatch):
    """Outstanding SI is reported, not a blocker on completed map work.

    Much of it cannot be obtained at all -- seat belt and occupant
    classification are inspection steps with no OEM calibration procedure to
    find -- so gating readiness on coverage held finished ADAS Map work open
    indefinitely against evidence that does not exist.
    """
    row = {
        "id": "ro-si",
        "ro_number": "RO-SI",
        "phase": "1",
        "vehicle": {"year": 2025, "make": "Ford", "model": "Explorer"},
    }

    async def query_repair_orders(_settings, _args):
        return {"status": "verified", "items": [row]}

    async def load_snapshot(_settings, identifier):
        # CIQ already carries the calibration, so only the SI is outstanding.
        return {
            "status": "verified",
            "snapshot": {
                "calibrations": [
                    {
                        "calibration_type": "360 Degree View Cameras",
                        "determination": "REQUIRED",
                        "method": "STATIC",
                    }
                ],
                "repair_order": {"id": identifier},
            },
        }

    async def discover_map(_catalog, _snapshot):
        return {
            "status": "verified",
            "requirements": [
                {"label": "360 Degree View Cameras", "method": "STATIC"}
            ],
            "requirement_count": 1,
        }

    async def reconcile(_settings, _adas, current, _map_info, _context):
        return current, [], None

    async def coverage(_catalog, _snapshot, _map_info):
        return [
            {
                "calibration": "360 Degree View Cameras",
                "state": prep.adas_artifact_catalog.MISSING,
                "available": False,
                "documents": [],
            }
        ]

    monkeypatch.setattr(prep.calibration_iq, "query_repair_orders", query_repair_orders)
    monkeypatch.setattr(prep, "_load_ro_snapshot", load_snapshot)
    monkeypatch.setattr(prep, "_discover_adas_map", discover_map)
    monkeypatch.setattr(prep, "_catalog_coverage", coverage)
    monkeypatch.setattr(prep, "_reconcile_one", reconcile)

    result = await prep._week_readiness(SimpleNamespace(), SimpleNamespace(), {"phase": "1"})

    assert result["ready_count"] == 1
    assert result["repair_orders"][0]["status"] == "ready"
    assert result["repair_orders"][0]["ready"] is True
    # The outstanding SI is still visible for whoever chases it.
    assert result["si_missing_count"] == 1
    assert result["repair_orders"][0]["missing_si"]
