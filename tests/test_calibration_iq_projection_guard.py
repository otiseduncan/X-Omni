from types import SimpleNamespace

import pytest

from core.services import calibration_iq_projection_guard as guard


class FakeCIQ(SimpleNamespace):
    pass


def _module(*, phase=None):
    async def health(_settings):
        return {
            "status": "available",
            "data_plane": {
                "status": "available",
                "verified": True,
                "count": 145,
                "sampled_rows": 1,
            },
        }

    async def get_repair_order(_settings, _args):
        return {
            "status": "verified",
            "repair_order": {"RO": "2400911778", "Phase": phase},
            "raw": {
                "repair_order": {
                    "ro_number": "2400911778",
                    "shop_stage_number": 6,
                    "shop_stage_name": "Reassemble",
                }
            },
        }

    def normalize_phase(value):
        text = str(value).strip()
        return text if text in {str(number) for number in range(1, 11)} else None

    def phase_of(_item):
        return phase

    return FakeCIQ(
        health=health,
        get_repair_order=get_repair_order,
        normalize_phase=normalize_phase,
        _phase_of=phase_of,
    )


@pytest.mark.asyncio
async def test_exact_ro_phase_falls_back_to_authoritative_shop_stage_number():
    module = _module(phase=None)
    guard.install(module)

    result = await module.get_repair_order(object(), {"repair_order_id": "2400911778"})

    assert result["repair_order"]["Phase"] == 6
    assert result["raw"]["repair_order"]["shop_stage_number"] == 6


def test_phase_helper_preserves_existing_phase_and_falls_back_when_missing():
    module = _module(phase=5)
    guard.install(module)
    assert module._phase_of({"shop_stage_number": 6}) == 5

    missing = _module(phase=None)
    guard.install(missing)
    assert missing._phase_of({"shop_stage_number": 6}) == 6


@pytest.mark.asyncio
async def test_status_health_probe_does_not_expose_business_looking_count():
    module = _module()
    guard.install(module)

    result = await module.health(object())

    assert result["status"] == "available"
    assert result["data_plane"]["verified"] is True
    assert "count" not in result["data_plane"]
    assert "sampled_rows" not in result["data_plane"]
    assert result["data_plane"]["business_counts_included"] is False
    assert result["data_plane"]["purpose"] == "repair_order_data_plane_health_probe"
