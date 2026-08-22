"""Regression coverage for a reported routing gap: a plain "I need the 360
camera procedure for <vehicle>" request matched none of the calibration-intent
signals, so it never reached ALLDATA at all -- it silently dead-ended at a
local ADAS SI miss and was reported to the user as simply "not found."
"""

from core.services import adas_calibration_depth as acd
from core.services import research_calibration_route  # noqa: F401 - installs the routing wrapper
from core.services import research_workflow


def test_360_camera_procedure_request_is_recognized_as_calibration_shaped():
    assert acd.calibration_intent(
        "I need the 360 camera procedure for a 2019 Ford F150"
    ) is True
    assert acd.calibration_intent(
        "I need the 360-degree camera procedure for a 2019 Ford F150"
    ) is True
    assert acd.calibration_intent(
        "Does the surround view camera need calibration after bumper replacement?"
    ) is True


def test_360_camera_procedure_request_now_routes_into_research():
    assert research_workflow.full_research_request(
        "I need the 360 camera procedure for a 2019 Ford F150"
    ) is True


def test_unrelated_camera_questions_still_dont_trigger_full_research():
    assert research_workflow.full_research_request(
        "how do I clean my camera lens"
    ) is False
