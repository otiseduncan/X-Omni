from __future__ import annotations

from core.services import calibration_iq_work_prep as prep


def test_voice_transcribed_8_oz_quick_reference_routes_to_collector():
    assert prep.classify_request(
        "retrieve all the Adas SI information out of 8 oz quick reference"
    ) == "quick_reference"


def test_voice_transcribed_8_ass_quick_reference_routes_to_collector():
    # Field trace: "collect 8 ass quick reference" -- another mishearing of
    # "ADAS" alongside the existing "8 oz" case.
    assert prep.classify_request("collect 8 ass quick reference") == "quick_reference"


def test_generic_adas_info_request_for_this_car_routes_to_collector():
    # Field trace: "collect the a dash information for this car" hallucinated
    # a fake vehicle-mismatch refusal because neither the literal "quick
    # reference" phrase nor a recent alldata_access turn was present. An
    # acquisition verb + an ADAS marker (including this mishearing) + an
    # explicit reference to the vehicle in front of the tech is enough on
    # its own.
    assert prep.classify_request(
        "collect the a dash information for this car"
    ) == "quick_reference"
    assert prep.classify_request(
        "download the adas information for that vehicle"
    ) == "quick_reference"


def test_generic_adas_info_request_without_vehicle_reference_stays_unrouted():
    # No "this car"/"that vehicle" anchor -- this reads as a knowledge
    # question for ADAS SI, not an ALLDATA acquisition request, so it must
    # not be swept into the licensed research lane.
    assert prep.classify_request("get the adas calibration steps") is None


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
