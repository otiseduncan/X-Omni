from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services import calibration_iq_weekly_queue as weekly_queue
from core.services import calibration_iq_work_prep as prep
from core.tools import registry as registry_mod


def test_request_classifier_routes_field_workflows_without_generic_adas_search():
    assert prep.classify_request("check what cars are in phase five") == "phase_list"
    assert prep.classify_request("make sure we're prepared for the week") == "week_readiness"
    assert prep.classify_request("what calibrations does RO 2400911667 have?") == "ro_requirements"
    assert prep.classify_request("retrieve all ADAS SI information out of ADAS Quick Reference for the Acura") == "quick_reference"
    assert prep.classify_request("log in to ALLDATA") == "alldata_access"
    assert prep.classify_request("what is the weather") is None


def test_phase_parser_accepts_spoken_number():
    assert prep._phase("check phase five") == "5"
    assert prep._phase("show phase 6") == "6"


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


def test_existing_required_requirement_is_not_duplicated():
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
    assert prep.build_reconciliation_actions(snapshot, map_info, "ro") == []


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
    assert "RO 101" in text
    assert "BSM calibration" in text
    assert "RO 102" in text
    assert "ADAS Map" in text
    assert "added/reactivated 1" in text


def test_describing_the_already_open_vehicle_does_not_replay_the_login_card():
    # Field trace: "download the adas si for the 2022 nissan altima that's
    # open in alldata" was re-triggering the login card because "open" fell
    # within 50 characters of "alldata" -- even though the user was
    # describing an already-open vehicle, not asking to log in.
    assert prep.classify_request(
        "down load the adas si for the 2022 nissan altima thats open in alldata"
    ) != "alldata_access"
    # An actual login/open command must still route normally.
    assert prep.classify_request("open alldata") == "alldata_access"
    assert prep.classify_request("open the alldata browser") == "alldata_access"
    assert prep.classify_request("log in to ALLDATA") == "alldata_access"


def test_descriptive_open_state_falls_through_to_continuation_when_stage_active():
    history = [_alldata_login_turn()]
    assert prep.classify_request(
        "down load the adas si for the 2022 nissan altima thats open in alldata",
        history,
    ) == "quick_reference"


def _alldata_login_turn():
    return {
        "role": "assistant",
        "artifacts": [{
            "type": "work_prep_state",
            "data": {"mode": "ciq_si_preparation", "stage": "awaiting_vehicle_selection"},
        }],
    }


def test_bare_followup_without_active_stage_does_not_route_to_quick_reference():
    # Case A precondition: with no active ALLDATA stage recorded, a message
    # that names none of ALLDATA/quick reference/RO stays unclassified so it
    # falls through to ordinary model tool choice, not a guessed collector.
    assert prep.classify_request("retrieve SI information please") is None
    assert prep.classify_request("retrieve SI information please", []) is None


def test_low_specificity_followup_after_alldata_login_resolves_to_quick_reference():
    # Case B: once "log in to ALLDATA" has run, a natural continuation that
    # names no ALLDATA/quick-reference/RO wording of its own must still route
    # to the collector so the already-selected vehicle resolves automatically.
    history = [_alldata_login_turn()]
    for text in (
        "retrieve SI information please",
        "Get the information.",
        "Go ahead.",
        "Pull it.",
        "Do this one.",
        "Okay, selected.",
        "Ready.",
    ):
        assert prep.classify_request(text, history) == "quick_reference", text


def test_unrelated_short_message_after_alldata_login_is_not_swept_in():
    # A bare "yes"/"ok" or an unrelated short message must not be treated as
    # an ALLDATA continuation just because a login happened recently -- it
    # could be answering something else entirely (e.g. a calendar prompt).
    history = [_alldata_login_turn()]
    for text in ("yes", "ok", "what's the weather", "how many are in Macon"):
        assert prep.classify_request(text, history) != "quick_reference", text


def test_stage_falls_outside_lookback_window_stops_being_active():
    history = [_alldata_login_turn()] + [
        {"role": "assistant", "artifacts": []} for _ in range(10)
    ]
    assert prep.classify_request("retrieve SI information please", history) is None


def test_completed_stage_does_not_keep_absorbing_later_short_messages():
    history = [{
        "role": "assistant",
        "artifacts": [{
            "type": "work_prep_state",
            "data": {"mode": "ciq_si_preparation", "stage": "complete"},
        }],
    }]
    assert prep.classify_request("retrieve SI information please", history) is None


def test_work_prep_tool_is_advertised_as_operator_authorized_after_install():
    schema = registry_mod.TOOL_SCHEMAS[prep.TOOL_NAME]
    assert set(schema["parameters"]["properties"]["mode"]["enum"]) == {
        "phase_list",
        "ro_requirements",
        "week_readiness",
        "queue_next",
    }


def test_bare_next_routes_to_queue_next_mode():
    for text in ("next", "Next.", "next one", "next car", "next vehicle", "who's next?", "okay, next"):
        assert prep.classify_request(text) == "queue_next", text


def test_next_embedded_in_a_longer_sentence_does_not_trigger_queue_walk():
    # _QUEUE_NEXT_RE is deliberately anchored to the whole message -- "next"
    # as a topic word elsewhere must not hijack an unrelated turn.
    assert prep.classify_request("what's next on my calendar today") != "queue_next"
    assert prep.classify_request("I'll do the next RO after lunch") != "queue_next"


def test_row_phase_token_normalizes_numeric_and_string_phases():
    assert prep._row_phase_token({"phase": 5}) == "5"
    assert prep._row_phase_token({"phase": "5.0"}) == "5"
    assert prep._row_phase_token({"phase": "Reassembly"}) == "Reassembly"
    assert prep._row_phase_token({}) is None


@pytest.mark.asyncio
async def test_week_readiness_defaults_to_phase_five_through_eight(monkeypatch):
    rows = [
        {
            "id": f"ro-{phase}", "ro_number": f"RO{phase}", "phase": str(phase),
            "vehicle": {"year": 2023, "make": "Ford", "model": "F-150"},
        }
        for phase in range(1, 9)
    ]

    async def query_repair_orders(_settings, args):
        assert "phase" not in args
        return {"status": "verified", "items": rows}

    monkeypatch.setattr(prep.calibration_iq, "query_repair_orders", query_repair_orders)

    async def load_snapshot(_settings, identifier):
        return {"status": "verified", "snapshot": {"calibrations": [], "repair_order": {"id": identifier}}}

    monkeypatch.setattr(prep, "_load_ro_snapshot", load_snapshot)

    async def coverage(_adas, _vehicle, _requirements):
        return []

    monkeypatch.setattr(prep, "_adas_coverage", coverage)

    result = await prep._week_readiness(SimpleNamespace(), SimpleNamespace(), {})
    assert result["queue_count"] == 4
    assert {item["ro_number"] for item in result["repair_orders"]} == {"RO5", "RO6", "RO7", "RO8"}
    assert result["phase_scope"] == ["5", "6", "7", "8"]


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
async def test_resolve_queue_next_selected_vehicle_not_in_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_queue, "_STORE", None)
    settings = SimpleNamespace(root=tmp_path)
    store = weekly_queue.get_store(tmp_path)
    item = weekly_queue.WeeklyQueueItem(
        repair_order_id="ro-1", ro_number="RO1", vehicle_label="2023 Acura TLX",
        vehicle_year="2023", vehicle_make="Acura", vehicle_model_trim="TLX",
    )
    store.save(weekly_queue.WeeklyQueue(conversation_id="8", items=[item]))

    class Browser:
        _page = object()

        async def start(self, auto_login=False):  # noqa: ARG002
            return {"authenticated": True}

    monkeypatch.setattr(prep.research_operator, "get_browser", lambda *_a, **_k: Browser())

    async def signals(_page):
        return ["Vehicle Information - 2019 Toyota Camry LE - ALLDATA Collision"]

    monkeypatch.setattr(prep, "_bounded_selected_vehicle_signals", signals)

    result = await prep.resolve_queue_next(settings, SimpleNamespace(), 8)
    assert result["status"] == "not_in_queue"


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

    class Browser:
        _page = object()

        async def start(self, auto_login=False):  # noqa: ARG002
            return {"authenticated": True}

    monkeypatch.setattr(prep.research_operator, "get_browser", lambda *_a, **_k: Browser())

    async def signals(_page):
        return ["Vehicle Information - 2023 Ford F-150 4WD - ALLDATA Collision"]

    monkeypatch.setattr(prep, "_bounded_selected_vehicle_signals", signals)

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
    assert all(item.status == "pending" for item in reloaded.items)


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

    class Browser:
        _page = object()

        async def start(self, auto_login=False):  # noqa: ARG002
            return {"authenticated": True}

    monkeypatch.setattr(prep.research_operator, "get_browser", lambda *_a, **_k: Browser())

    async def signals(_page):
        return ["Vehicle Information - 2023 Acura TLX Type S - ALLDATA Collision"]

    monkeypatch.setattr(prep, "_bounded_selected_vehicle_signals", signals)

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
    assert reloaded.items[0].status == "complete"
    assert reloaded.items[1].status == "pending"
