from core.services import research_workflow


def test_calibration_iq_composite_research_stays_on_existing_operator_lane():
    text = (
        "For Calibration IQ RO XOP-20260821211550-c28d41ae, re-run the complete OEM "
        "research for the existing blind spot detection calibration using ADAS SI, "
        "verify the persisted evidence and page citations."
    )
    assert research_workflow.full_research_request(text) is False


def test_external_collision_question_enters_verified_multi_source_lane():
    text = (
        "Research whether Toyota permits a recycled blind spot monitor module. "
        "Check ADAS SI, ALLDATA, then official Toyota collision sources."
    )
    assert research_workflow.full_research_request(text) is True
