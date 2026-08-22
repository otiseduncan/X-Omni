from core.services import research_alldata_navigation as alldata_nav
from core.services import research_followup


def test_check_alldata_for_it_is_a_contextual_provider_followup():
    assert research_followup.provider_followup("check all data for it") is True
    assert research_followup.provider_followup("check ALLDATA for it") is True


def test_followup_keeps_prior_jeep_bsm_subject():
    history = [
        {
            "id": 10,
            "role": "user",
            "content": "I need the BSM calibration procedure for a 21 Jeep Cherokee Latitude Luxe",
        },
        {
            "id": 11,
            "role": "assistant",
            "content": "No local document matched.",
        },
        {"id": 12, "role": "user", "content": "check all data for it"},
    ]

    resolved = research_followup.resolve_followup("check all data for it", history)

    assert "21 Jeep Cherokee Latitude Luxe" in resolved
    assert "BSM calibration procedure" in resolved
    assert "ALLDATA" in resolved
    assert "official OEM/manufacturer sources" in resolved
    assert resolved != "check all data for it"


def test_alldata_vehicle_parser_understands_two_digit_year_and_trim():
    vehicle = alldata_nav.vehicle_from_query(
        "I need the BSM calibration procedure for a 21 Jeep Cherokee Latitude Luxe"
    )

    assert vehicle["year"] == "2021"
    assert vehicle["make"] == "Jeep"
    assert vehicle["model_trim"] == "Cherokee Latitude Luxe"
    assert vehicle["label"] == "2021 Jeep Cherokee Latitude Luxe"


def test_alldata_topic_parser_expands_bsm_to_provider_search_language():
    query = "I need the BSM calibration procedure for a 21 Jeep Cherokee Latitude Luxe"
    vehicle = alldata_nav.vehicle_from_query(query)
    topic = alldata_nav.topic_from_query(query, vehicle)
    variants = alldata_nav.topic_variants(topic)

    assert any("Blind Spot Monitor" in item for item in variants)
    assert any("calibration" in item.casefold() for item in variants)
    assert all("Lexus" not in item for item in variants)
