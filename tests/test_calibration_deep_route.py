from core.services import research_workflow


def test_plain_calibration_question_enters_deep_external_research_lane():
    assert research_workflow.full_research_request(
        "What calibrations are required on a 2025 Subaru Outback after collision repair?"
    ) is True


def test_eyesight_collision_question_enters_deep_external_research_lane():
    assert research_workflow.full_research_request(
        "Does Subaru EyeSight need calibration after any collision?"
    ) is True


def test_explicit_local_only_calibration_question_stays_local():
    assert research_workflow.full_research_request(
        "Check ADAS SI only: what calibrations are required on a 2025 Subaru Outback?"
    ) is False


def test_what_does_adas_si_say_stays_scoped_to_adas_si():
    assert research_workflow.full_research_request(
        "What does ADAS SI say about calibrating EyeSight on a 2025 Subaru Outback?"
    ) is False


def test_adas_first_then_alldata_and_oem_is_multi_source():
    assert research_workflow.full_research_request(
        "Research whether Toyota permits the use of a recycled blind spot monitor module. "
        "Check ADAS SI first, then ALLDATA, then official Toyota collision sources if necessary."
    ) is True


def test_check_adas_plus_explicit_alldata_is_not_mistaken_for_local_scope():
    assert research_workflow.full_research_request(
        "Check ADAS SI, ALLDATA, then official Toyota collision sources."
    ) is True


def test_calibration_iq_ro_research_still_uses_existing_operator_lane():
    assert research_workflow.full_research_request(
        "For Calibration IQ RO 2400911667, research the camera calibration and attach OEM evidence."
    ) is False
