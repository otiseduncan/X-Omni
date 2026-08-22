from core.services import research_verification


def _vehicle():
    return {"year": "2018", "make": "Ford", "model_trim": "F-350"}


def test_unselected_vehicle_is_never_verified_regardless_of_url():
    claim = research_verification.evaluate_alldata_claim(
        vehicle=_vehicle(),
        vehicle_state={"selected": False, "reason": "ALLDATA vehicle selection was not confirmed."},
        query_submitted=True,
        matched_terms=["camera", "calibration"],
        relevance_score=6,
        result_page_text="2018 Ford F-350 forward facing camera calibration procedure",
    )
    assert claim["verified"] is False
    assert "not confirmed" in claim["reason"]


def test_selected_but_no_query_submitted_is_not_verified():
    claim = research_verification.evaluate_alldata_claim(
        vehicle=_vehicle(),
        vehicle_state={"selected": True},
        query_submitted=False,
        matched_terms=[],
        relevance_score=0,
        result_page_text="",
    )
    assert claim["verified"] is False
    assert "query" in claim["reason"].casefold()


def test_selected_and_submitted_but_no_matched_terms_is_not_verified():
    claim = research_verification.evaluate_alldata_claim(
        vehicle=_vehicle(),
        vehicle_state={"selected": True},
        query_submitted=True,
        matched_terms=[],
        relevance_score=0,
        result_page_text="2018 Ford F-350 owner's manual index",
    )
    assert claim["verified"] is False


def test_result_page_that_lost_the_vehicle_identity_is_not_verified():
    """Guards against drift/redirect: the result page must still describe the
    requested vehicle, not just contain on-topic terms from some other page."""
    claim = research_verification.evaluate_alldata_claim(
        vehicle=_vehicle(),
        vehicle_state={"selected": True},
        query_submitted=True,
        matched_terms=["camera", "calibration"],
        relevance_score=5,
        result_page_text="2021 Toyota Camry forward facing camera calibration procedure",
    )
    assert claim["verified"] is False
    assert "identity" in claim["reason"].casefold()


def test_fully_supported_claim_is_verified():
    claim = research_verification.evaluate_alldata_claim(
        vehicle=_vehicle(),
        vehicle_state={"selected": True, "confirmed_via": "Change Vehicle 2018 Ford F-350"},
        query_submitted=True,
        matched_terms=["camera", "calibration"],
        relevance_score=6,
        result_page_text="2018 Ford F-350 forward facing camera calibration procedure",
    )
    assert claim["verified"] is True
    assert claim["reason"] is None


def test_unselected_source_claim_never_verifies():
    claim = research_verification.unselected_source_claim("generic keyword search only")
    assert claim["verified"] is False
    assert claim["reason"] == "generic keyword search only"
