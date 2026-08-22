import pytest

from core.services import research_alldata_contract, research_alldata_navigation, research_alldata_terms


def test_vehicle_parser_understands_24_ford_transit():
    vehicle = research_alldata_navigation.vehicle_from_query(
        "I need the forward facing calibration procedure for a 24 Ford Transit"
    )
    assert vehicle["year"] == "2024"
    assert vehicle["make"] == "Ford"
    assert vehicle["model_trim"] == "Transit"


def test_vehicle_parser_understands_21_jeep_cherokee_latitude_luxe():
    vehicle = research_alldata_navigation.vehicle_from_query(
        "I need the BSM calibration procedure for a 21 Jeep Cherokee Latitude Luxe"
    )
    assert vehicle["year"] == "2021"
    assert vehicle["make"] == "Jeep"
    assert vehicle["model_trim"] == "Cherokee Latitude Luxe"


def _terms(topic: str) -> set[str]:
    return {
        value.casefold()
        for value in research_alldata_terms.expanded_topic_variants(topic)
    }


def test_forward_facing_terms_are_generic_and_include_common_camera_names():
    folded = _terms("forward facing calibration")
    assert "forward facing camera calibration" in folded
    assert "front camera calibration" in folded
    assert "forward recognition camera calibration" in folded
    assert "camera alignment" in folded
    assert "adas calibration" in folded
    assert "adas initialization" in folded
    # OEM-specific labels may be additive, but generic terms must exist first.
    assert "image processing module a camera alignment" in folded


def test_blind_spot_terms_cover_monitor_and_rear_radar_naming():
    folded = _terms("blind spot calibration")
    assert "blind spot monitor calibration" in folded
    assert "blind spot detection calibration" in folded
    assert "rear corner radar calibration" in folded
    assert "rear side radar calibration" in folded
    assert "blind spot module initialization" in folded


def test_front_radar_terms_cover_distance_sensor_and_millimeter_wave_names():
    folded = _terms("front radar calibration")
    assert "forward radar calibration" in folded
    assert "millimeter wave radar calibration" in folded
    assert "distance sensor calibration" in folded
    assert "radar alignment" in folded


def test_rear_camera_terms_cover_initialization_and_position_setting():
    folded = _terms("rear camera calibration")
    assert "rear camera calibration" in folded
    assert "rear camera initialization" in folded
    assert "back camera position setting" in folded
    assert "camera optical axis adjustment" in folded


def test_steering_and_occupant_calibration_families_include_reset_initialization_terms():
    steering = _terms("steering angle calibration")
    assert "steering angle sensor calibration" in steering
    assert "steering angle sensor initialization" in steering
    assert "steering angle reset" in steering

    occupant = _terms("occupant classification calibration")
    assert "occupant classification system calibration" in occupant
    assert "occupant classification system initialization" in occupant
    assert "occupant classification zero point calibration" in occupant


class _Page:
    def __init__(self):
        self.url = "https://my.alldata.com/#/home"
        self.gotos = []

    async def goto(self, url, **_kwargs):
        self.gotos.append(url)
        self.url = "https://my.alldata.com/repair/#/"


@pytest.mark.asyncio
async def test_direct_repair_fallback_uses_authenticated_repair_product_url():
    page = _Page()
    assert await research_alldata_contract._direct_repair(page) is True
    assert page.gotos == ["https://my.alldata.com/repair/#/"]


@pytest.mark.asyncio
async def test_hardened_enter_product_falls_back_to_direct_repair(monkeypatch):
    page = _Page()

    async def previous(_page):
        return False

    async def click_none(_page, _names):
        return None

    monkeypatch.setattr(research_alldata_contract, "_PREVIOUS_ENTER", previous)
    monkeypatch.setattr(research_alldata_contract, "_click_any", click_none)

    assert await research_alldata_contract.hardened_enter_product(page) is True
    assert "/repair/" in page.url
