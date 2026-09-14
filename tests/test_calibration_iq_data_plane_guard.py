from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services import calibration_iq_data_plane_guard as guard


class _Module(SimpleNamespace):
    pass


def _settings():
    return SimpleNamespace(calibration_iq_project_path="X:/Calibration IQ")


def _module(*, health_result=None, ro_result=None):
    async def health(_settings):
        return dict(
            health_result
            or {
                "status": "available",
                "configured": True,
                "token_present": True,
                "base_url": "http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq",
            }
        )

    async def get_repair_order(_settings, _args):
        return dict(
            ro_result
            or {
                "status": "no_result",
                "repair_order": None,
                "query": "2400711902",
                "message": "No repair order matched '2400711902'.",
            }
        )

    return _Module(health=health, get_repair_order=get_repair_order)


@pytest.mark.asyncio
async def test_health_degrades_when_http_shell_is_up_but_data_plane_is_not(monkeypatch):
    module = _module()

    async def probe(_module, _settings):
        return {"status": "unavailable", "verified": False, "count": None, "http_status": 500}

    monkeypatch.setattr(guard, "probe_data_plane", probe)
    guard.install(module)

    result = await module.health(_settings())

    assert result["status"] == "degraded"
    assert result["data_plane"]["verified"] is False
    assert "repair-order data plane" in result["message"]


@pytest.mark.asyncio
async def test_health_stays_available_when_authenticated_collection_is_proven(monkeypatch):
    module = _module()

    async def probe(_module, _settings):
        return {"status": "available", "verified": True, "count": 51, "sampled_rows": 1}

    monkeypatch.setattr(guard, "probe_data_plane", probe)
    guard.install(module)

    result = await module.health(_settings())

    assert result["status"] == "available"
    assert result["data_plane"]["count"] == 51


@pytest.mark.asyncio
async def test_missing_ro_is_not_mislabeled_as_missing_vin_when_data_plane_is_down(monkeypatch):
    module = _module()

    async def probe(_module, _settings):
        return {"status": "offline", "verified": False, "count": None}

    monkeypatch.setattr(guard, "probe_data_plane", probe)
    guard.install(module)

    result = await module.get_repair_order(_settings(), {"repair_order_id": "2400711902"})

    assert result["status"] == "data_plane_unavailable"
    assert "not proven missing" in result["message"]
    assert "missing VIN" in result["message"]


@pytest.mark.asyncio
async def test_zero_row_data_plane_is_reported_as_empty_not_as_a_vin_problem(monkeypatch):
    module = _module()

    async def probe(_module, _settings):
        return {"status": "available", "verified": True, "count": 0, "sampled_rows": 0}

    monkeypatch.setattr(guard, "probe_data_plane", probe)
    guard.install(module)

    result = await module.get_repair_order(_settings(), {"repair_order_id": "2400711902"})

    assert result["status"] == "data_plane_empty"
    assert "zero repair orders" in result["message"]


@pytest.mark.asyncio
async def test_real_verified_ro_with_empty_vin_is_left_untouched(monkeypatch):
    module = _module(
        ro_result={
            "status": "verified",
            "repair_order": {"RO": "2400711902", "vin": None},
            "raw": {"vehicle": {"year": 2025, "make": "Kia", "model": "K4"}},
        }
    )
    calls = 0

    async def probe(_module, _settings):
        nonlocal calls
        calls += 1
        return {"status": "available", "verified": True, "count": 51}

    monkeypatch.setattr(guard, "probe_data_plane", probe)
    guard.install(module)

    result = await module.get_repair_order(_settings(), {"repair_order_id": "2400711902"})

    assert result["status"] == "verified"
    assert result["repair_order"]["vin"] is None
    assert calls == 0
