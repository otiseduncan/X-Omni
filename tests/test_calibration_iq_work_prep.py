from __future__ import annotations

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
    }
