from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.services import adas_si_research as research


def _verified_ro(*, vin: str = "") -> dict[str, Any]:
    return {
        "status": "verified",
        "repair_order": {
            "RO": "2400711902",
            "id": "ro-id-1902",
            "Phase": 1,
            "Shop": "Warner Robins",
            "vin": vin or None,
        },
        "raw": {
            "id": "ro-id-1902",
            "ro_number": "2400711902",
            "vin": vin or None,
            "vehicle": {"year": 2025, "make": "Kia", "model": "K4"},
            "calibrations": [
                {
                    "id": "cal-radar",
                    "title": "Front Radar Sensor - SCC / AEB / FCW",
                    "determination": "REQUIRED",
                }
            ],
        },
    }


def test_target_from_read_prefers_exact_vin_when_available() -> None:
    target = research.target_from_read(
        _verified_ro(vin="3KPFT4DE4SE215941")
    )
    assert target is not None
    assert target["vin"] == "3KPFT4DE4SE215941"
    assert target["identity_mode"] == "vin"
    assert target["vehicle"] == {"year": 2025, "make": "Kia", "model": "K4"}


def test_target_from_read_falls_back_to_verified_year_make_model_without_vin() -> None:
    target = research.target_from_read(_verified_ro(vin=""))
    assert target is not None
    assert target["vin"] == ""
    assert target["identity_mode"] == "year_make_model"
    assert target["vehicle"] == {"year": 2025, "make": "Kia", "model": "K4"}
    assert target["calibrations"][0]["title"] == "Front Radar Sensor - SCC / AEB / FCW"


@pytest.mark.asyncio
async def test_target_resolution_does_not_drop_a_ro_only_because_vin_is_missing() -> None:
    async def ro_reader(_args: dict[str, Any]) -> dict[str, Any]:
        return _verified_ro(vin="")

    service = research.AdasSiResearchService(
        SimpleNamespace(),
        SimpleNamespace(),
        client=object(),
        ro_reader=ro_reader,
    )

    targets, problems, label = await service._resolve_targets(
        {"repair_order_id": "2400711902"}
    )

    assert label == "RO 2400711902"
    assert len(targets) == 1
    assert targets[0]["identity_mode"] == "year_make_model"
    assert targets[0]["vehicle"]["model"] == "K4"
    assert not any("valid 17-character VIN" in problem for problem in problems)
