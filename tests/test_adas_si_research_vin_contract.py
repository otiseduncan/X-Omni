from core.services import adas_si_research as research


def _base_read() -> dict:
    return {
        "status": "verified",
        "repair_order": {
            "RO": "2400711902",
            "id": "ro-uuid-1",
            "vin": "1HGCV1F30PA000001",
            "Phase": 4,
            "Shop": "Warner Robins",
        },
        "raw": {
            "vehicle": {
                "year": 2023,
                "make": "Honda",
                "model": "Accord",
                "trim": "EX",
            },
            "calibrations": [
                {
                    "id": "cal-radar",
                    "title": "Front Millimeter Wave Radar",
                    "determination": "REQUIRED",
                }
            ],
        },
    }


def test_vin_prefers_normalized_repair_order_summary():
    read = _base_read()
    assert "vin" not in read["raw"]
    assert "vin" not in read["raw"]["vehicle"]
    assert research.vin_from_read(read) == "1HGCV1F30PA000001"


def test_target_accepts_real_get_repair_order_shape_with_summary_vin():
    target = research.target_from_read(_base_read())
    assert target is not None
    assert target["vin"] == "1HGCV1F30PA000001"
    assert target["vehicle"] == {
        "year": 2023,
        "make": "Honda",
        "model": "Accord",
        "trim": "EX",
    }
    assert target["ro_number"] == "2400711902"


def test_vin_raw_snapshot_fallback_remains_supported():
    read = _base_read()
    read["repair_order"].pop("vin")
    read["raw"]["vehicle"]["vin"] = "1HGCV1F31PA000002"
    assert research.vin_from_read(read) == "1HGCV1F31PA000002"


def test_invalid_vin_still_fails_closed():
    read = _base_read()
    read["repair_order"]["vin"] = "VIN-1"
    assert research.vin_from_read(read) == ""
