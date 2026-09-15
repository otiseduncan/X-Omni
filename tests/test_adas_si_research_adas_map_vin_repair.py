from types import SimpleNamespace

import pytest

from core.services import adas_si_research as research
from core.services import calibration_iq, scrapex


RO = "2400911761"
VIN = "3TMCZ5AN0PM123456"


def _read(*, vin=None, adas_map=True):
    documents = []
    if adas_map:
        documents.append(
            {
                "id": "doc-map",
                "title": f"{RO} ADAS Map",
                "document_type": "adas_map_report",
                "semantic_type": "ADAS_MAP_REPORT",
            }
        )
    return {
        "status": "verified",
        "repair_order": {
            "id": "ro-1",
            "RO": RO,
            "vin": vin,
            "year": 2023,
            "make": "Toyota",
            "model": "Tacoma",
        },
        "vehicle": {
            "vin": vin,
            "year": 2023,
            "make": "Toyota",
            "model": "Tacoma",
        },
        "raw": {
            "repair_order": {
                "id": "ro-1",
                "ro_number": RO,
                "vin": vin,
                "year": 2023,
                "make": "Toyota",
                "model": "Tacoma",
            },
            "vehicle": {
                "vin": vin,
                "year": 2023,
                "make": "Toyota",
                "model": "Tacoma",
            },
            "research": {"documents": documents},
            "calibrations": [
                {
                    "id": "cal-camera",
                    "calibration_type": "Forward Recognition Camera",
                    "determination": "REQUIRED",
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_missing_ciq_vin_is_repaired_from_proven_attached_adas_map(monkeypatch):
    reads = [_read(vin=None), _read(vin=VIN)]
    calls = []

    async def get_ro(_settings, _args):
        return reads.pop(0)

    async def start_native(_settings):
        return {"success": True, "verified": True}

    async def request(_settings, method, path, **kwargs):
        calls.append((method, path, kwargs.get("body")))
        return {
            "requested_count": 1,
            "repaired_count": 1,
            "results": [{"ro_number": RO, "status": "repaired", "vin": VIN}],
        }

    monkeypatch.setattr(calibration_iq, "get_repair_order", get_ro)
    monkeypatch.setattr(scrapex, "start_native", start_native)
    monkeypatch.setattr(scrapex, "_request", request)

    reader = research.default_ro_reader(SimpleNamespace())
    result = await reader({"repair_order_id": RO})

    assert research.vin_from_read(result) == VIN
    assert calls == [
        (
            "POST",
            "/api/adas-map/repair-proven-vins",
            {"ro_numbers": [RO]},
        )
    ]


@pytest.mark.asyncio
async def test_missing_vin_without_attached_adas_map_does_not_attempt_repair(monkeypatch):
    async def get_ro(_settings, _args):
        return _read(vin=None, adas_map=False)

    async def should_not_start(_settings):
        raise AssertionError("ScrapeX repair must not run without an attached ADAS Map")

    monkeypatch.setattr(calibration_iq, "get_repair_order", get_ro)
    monkeypatch.setattr(scrapex, "start_native", should_not_start)

    reader = research.default_ro_reader(SimpleNamespace())
    result = await reader({"repair_order_id": RO})
    assert research.vin_from_read(result) == ""


@pytest.mark.asyncio
async def test_existing_valid_vin_never_calls_backfill(monkeypatch):
    async def get_ro(_settings, _args):
        return _read(vin=VIN)

    async def should_not_start(_settings):
        raise AssertionError("ScrapeX repair must not run when CIQ already has a VIN")

    monkeypatch.setattr(calibration_iq, "get_repair_order", get_ro)
    monkeypatch.setattr(scrapex, "start_native", should_not_start)

    reader = research.default_ro_reader(SimpleNamespace())
    result = await reader({"repair_order_id": RO})
    assert research.vin_from_read(result) == VIN


@pytest.mark.asyncio
async def test_unverified_scrapex_backfill_leaves_vin_guard_closed(monkeypatch):
    async def get_ro(_settings, _args):
        return _read(vin=None)

    async def start_native(_settings):
        return {"success": True, "verified": True}

    async def request(_settings, method, path, **kwargs):
        return {
            "requested_count": 1,
            "repaired_count": 0,
            "results": [{"ro_number": RO, "status": "unverified"}],
        }

    monkeypatch.setattr(calibration_iq, "get_repair_order", get_ro)
    monkeypatch.setattr(scrapex, "start_native", start_native)
    monkeypatch.setattr(scrapex, "_request", request)

    reader = research.default_ro_reader(SimpleNamespace())
    result = await reader({"repair_order_id": RO})
    assert research.vin_from_read(result) == ""
