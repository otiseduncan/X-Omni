from types import SimpleNamespace

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


def test_forward_camera_terms_include_ford_ipma_language():
    values = research_alldata_terms.expanded_topic_variants(
        "forward facing calibration"
    )
    folded = {value.casefold() for value in values}
    assert "forward facing camera calibration" in folded
    assert "image processing module a camera alignment" in folded
    assert "ipma camera alignment" in folded


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
