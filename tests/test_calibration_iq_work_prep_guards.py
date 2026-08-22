from __future__ import annotations

from core.services import calibration_iq_work_prep as prep


def test_voice_transcribed_8_oz_quick_reference_routes_to_collector():
    assert prep.classify_request(
        "retrieve all the Adas SI information out of 8 oz quick reference"
    ) == "quick_reference"


def test_parent_adas_map_key_does_not_promote_sibling_calibration_data():
    snapshot = {
        "adas_map": {
            "provider": "ADAS Map",
            "required_calibrations": ["Blind Spot Monitor Calibration"],
        },
        "calibrations": [
            {
                "calibration_type": "Forward Facing Camera Calibration",
                "determination": "REQUIRED",
            }
        ],
        "assessments": [
            {"confirmed_calibrations": ["Steering Angle Sensor Reset"]}
        ],
    }
    result = prep.extract_adas_map(snapshot)
    assert result["status"] == "verified"
    labels = {item["label"] for item in result["requirements"]}
    assert labels == {"Blind Spot Monitor Calibration"}


def test_unmarked_sibling_named_adas_topic_is_not_governing_source():
    snapshot = {
        "vehicle": {
            "repair_information": {
                "adas_map": {
                    "provider": "ADAS Map",
                    "required_calibrations": ["BSM calibration"],
                },
                "technician_notes": "Forward Facing Camera Calibration",
            }
        }
    }
    result = prep.extract_adas_map(snapshot)
    labels = {item["label"] for item in result["requirements"]}
    assert labels == {"BSM calibration"}
