from types import SimpleNamespace

import pytest

from core.services import calibration_iq
from core.services import calibration_iq_endpoint_discovery as discovery


def test_candidate_bases_include_native_public_and_backend_endpoints():
    candidates = discovery.candidate_bases(
        "http://127.0.0.1:9999/old/path"
    )

    assert candidates[0] == "http://127.0.0.1:9999/old/path"
    assert "http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq" in candidates
    assert "http://localhost:8084/api/v1/tools/v1/calibration-iq" in candidates
    assert "http://127.0.0.1:18000/api/v1/tools/v1/calibration-iq" in candidates


def test_non_loopback_configuration_is_not_rewritten():
    configured = "https://ciq.example.test/api/v1/tools/v1/calibration-iq"
    assert discovery.candidate_bases(configured) == [configured]


@pytest.mark.asyncio
async def test_resolve_base_recovers_to_native_public_endpoint(monkeypatch):
    attempted = []

    async def fake_answers(base: str, timeout: float = 3.0) -> bool:
        attempted.append(base)
        return base == "http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq"

    monkeypatch.setattr(discovery, "_answers_health", fake_answers)
    calibration_iq._RESOLVED_BASE.clear()
    settings = SimpleNamespace(
        calibration_iq_base_url="http://127.0.0.1:9999/old/path"
    )

    resolved = await calibration_iq.resolve_base(settings)

    assert resolved == "http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq"
    assert attempted[0] == "http://127.0.0.1:9999/old/path"
    assert calibration_iq._RESOLVED_BASE[settings.calibration_iq_base_url] == resolved


@pytest.mark.asyncio
async def test_resolve_base_revalidates_and_replaces_stale_cached_endpoint(monkeypatch):
    configured = "http://127.0.0.1:9999/old/path"
    stale = "http://localhost:8080/api/v1/tools/v1/calibration-iq"
    live = "http://127.0.0.1:18000/api/v1/tools/v1/calibration-iq"
    calibration_iq._RESOLVED_BASE.clear()
    calibration_iq._RESOLVED_BASE[configured] = stale

    async def fake_answers(base: str, timeout: float = 3.0) -> bool:
        return base == live

    monkeypatch.setattr(discovery, "_answers_health", fake_answers)
    settings = SimpleNamespace(calibration_iq_base_url=configured)

    resolved = await calibration_iq.resolve_base(settings)

    assert resolved == live
    assert calibration_iq._RESOLVED_BASE[configured] == live
