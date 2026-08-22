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


def test_topic_parser_expands_bsm_to_provider_search_language():
    query = "I need the BSM calibration procedure for a 21 Jeep Cherokee Latitude Luxe"
    vehicle = research_alldata_navigation.vehicle_from_query(query)
    topic = research_alldata_navigation.topic_from_query(query, vehicle)
    variants = research_alldata_navigation.topic_variants(topic)

    assert any("Blind Spot Monitor" in item for item in variants)
    assert any("calibration" in item.casefold() for item in variants)
    assert all("Lexus" not in item for item in variants)


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


class _EmptyLocator:
    """Represents "nothing found" for any locator lookup on a fake page."""

    def __init__(self):
        self.first = self

    async def is_visible(self, timeout=None):  # noqa: ARG002
        return False

    async def inner_text(self, timeout=None):  # noqa: ARG002
        raise RuntimeError("not visible")

    def locator(self, *_args, **_kwargs):
        return self


class _TextLocator:
    """A single visible element exposing fixed bounded text (e.g. body)."""

    def __init__(self, text: str):
        self._text = text
        self.first = self

    async def is_visible(self, timeout=None):  # noqa: ARG002
        return True

    async def inner_text(self, timeout=None):  # noqa: ARG002
        return self._text


class _StuckOnSelectVehiclePage:
    """Reproduces the exact reported evidence for a 2018 Ford F-350 request:
    still sitting on ALLDATA's raw "Select Vehicle" screen, whose year dropdown
    lists every year (including 2018) and whose Recent Vehicles panel happens
    to contain a prior Ford -- the exact ingredients that let a whole-body
    substring match falsely report the vehicle as already selected, and that
    let ALLDATA get graded "searched * verified" while never leaving the
    picker.
    """

    def __init__(self):
        self.url = "https://my.alldata.com/repair/#/vehicle-select"
        self.frames = []
        self._body = (
            "Select Vehicle YMME/VIN Plate Year 2027 2026 2025 2024 2023 2022 "
            "2021 2020 2019 2018 2017 Make Model Recent Vehicles "
            "2019 Ford Explorer 2016 Ford F-150"
        )

    def locator(self, selector):
        return _TextLocator(self._body) if selector == "body" else _EmptyLocator()

    def get_by_text(self, *_args, **_kwargs):
        return _EmptyLocator()

    def get_by_role(self, *_args, **_kwargs):
        return _EmptyLocator()

    async def title(self):
        return "ALLDATA Collision - Home"


@pytest.mark.asyncio
async def test_current_vehicle_label_ignores_the_raw_select_vehicle_picker():
    page = _StuckOnSelectVehiclePage()
    vehicle = {"year": "2018", "make": "Ford", "model_trim": "F-350"}

    label = await research_alldata_navigation._current_vehicle_label(page)

    # The picker's own year list and Recent Vehicles panel contain both "2018"
    # and "Ford" -- exactly what let the old whole-body substring check
    # falsely confirm a vehicle that was never actually selected. The bounded
    # signal this function returns (here, just the generic page title, since
    # no Change Vehicle control exists) must not satisfy identity confirmation.
    assert not await research_alldata_navigation._confirms_identity(label, vehicle)


@pytest.mark.asyncio
async def test_select_vehicle_fails_closed_on_the_raw_picker_screen():
    page = _StuckOnSelectVehiclePage()
    vehicle = research_alldata_navigation.vehicle_from_query(
        "camera calibration for a 2018 Ford F350"
    )

    result = await research_alldata_navigation._select_vehicle(page, vehicle)

    # This is the exact reported defect: X sat on Select Vehicle and the
    # workflow still reported the vehicle as selected, with 0 matched terms,
    # graded "searched * verified". It must fail closed instead.
    assert result["selected"] is False
