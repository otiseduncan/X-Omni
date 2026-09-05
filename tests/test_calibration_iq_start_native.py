"""
Tests for calibration_iq.start_native -- the self-healing recovery tool
that lets X Omni bring Calibration IQ's native stack back up itself instead
of the operator having to run the launcher by hand.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from core.services import calibration_iq as ciq
from core.tools.registry import TOOL_SCHEMAS, Registry


@dataclass
class FakeSettings:
    calibration_iq_base_url: str
    calibration_iq_project_path: Path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "calibration iq"
    root.mkdir()
    (root / "Start-CalibrationIQ-Native.ps1").write_text("# stub\n", encoding="utf-8")
    return root


@pytest.fixture
def settings(project: Path) -> FakeSettings:
    return FakeSettings(
        calibration_iq_base_url="http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq",
        calibration_iq_project_path=project,
    )


def _health(status: str, base_url: str = "http://127.0.0.1:8084") -> dict:
    return {"status": status, "configured": True, "base_url": base_url}


@pytest.mark.asyncio
async def test_already_healthy_still_delegates_to_revision_aware_launcher(
    settings, monkeypatch
):
    calls = {"run": 0}

    async def fake_health(_settings):
        return _health("available")

    def fake_run(_project_path):
        calls["run"] += 1
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Calibration IQ native stack is already healthy and matches the current source revision.\n",
            stderr="",
        )

    monkeypatch.setattr(ciq, "health", fake_health)
    monkeypatch.setattr(ciq, "_run_start_native_script", fake_run)

    result = await ciq.start_native(settings)

    assert result["status"] == "healthy"
    assert result["executed"] is True
    assert result["verified"] is True
    assert calls["run"] == 1


@pytest.mark.asyncio
async def test_successful_start_verifies_via_fresh_health(settings, monkeypatch):
    health_calls = {"n": 0}

    async def fake_health(_settings):
        health_calls["n"] += 1
        # First call (pre-check): offline. Second call (post-start verify): available.
        return _health("offline") if health_calls["n"] == 1 else _health("available")

    def fake_run(_project_path):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="healthy\n", stderr="")

    monkeypatch.setattr(ciq, "health", fake_health)
    monkeypatch.setattr(ciq, "_run_start_native_script", fake_run)

    result = await ciq.start_native(settings)

    assert result["status"] == "healthy"
    assert result["executed"] is True
    assert result["verified"] is True
    assert result["exit_code"] == 0
    assert health_calls["n"] == 2


@pytest.mark.asyncio
async def test_nonzero_exit_and_still_unhealthy_is_reported_as_failed(settings, monkeypatch):
    async def fake_health(_settings):
        return _health("offline")

    def fake_run(_project_path):
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Native startup failed: port in use.\n"
        )

    monkeypatch.setattr(ciq, "health", fake_health)
    monkeypatch.setattr(ciq, "_run_start_native_script", fake_run)

    result = await ciq.start_native(settings)

    assert result["status"] == "failed"
    assert result["verified"] is False
    assert result["exit_code"] == 1
    assert "port in use" in result["detail"]


@pytest.mark.asyncio
async def test_a_nonzero_exit_that_actually_recovers_is_not_reported_as_failed(settings, monkeypatch):
    """The script's exit code is never trusted alone -- only the fresh health
    recheck decides success, in either direction."""
    health_calls = {"n": 0}

    async def fake_health(_settings):
        health_calls["n"] += 1
        return _health("offline") if health_calls["n"] == 1 else _health("available")

    def fake_run(_project_path):
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="warning\n")

    monkeypatch.setattr(ciq, "health", fake_health)
    monkeypatch.setattr(ciq, "_run_start_native_script", fake_run)

    result = await ciq.start_native(settings)

    assert result["status"] == "healthy"
    assert result["verified"] is True


@pytest.mark.asyncio
async def test_timeout_is_reported_as_failed_and_not_executed(settings, monkeypatch):
    async def fake_health(_settings):
        return _health("offline")

    def fake_run(_project_path):
        raise subprocess.TimeoutExpired(cmd="Start-CalibrationIQ-Native.ps1", timeout=300)

    monkeypatch.setattr(ciq, "health", fake_health)
    monkeypatch.setattr(ciq, "_run_start_native_script", fake_run)

    result = await ciq.start_native(settings)

    assert result["status"] == "failed"
    assert result["executed"] is False
    assert result["exit_code"] is None
    assert "timeout" in result["detail"].casefold()


@pytest.mark.asyncio
async def test_missing_launcher_script_fails_without_spawning(tmp_path: Path, monkeypatch):
    empty_project = tmp_path / "no calibration iq here"
    empty_project.mkdir()
    settings = FakeSettings(
        calibration_iq_base_url="http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq",
        calibration_iq_project_path=empty_project,
    )

    async def fake_health(_settings):
        return _health("offline")

    def fake_run(_project_path):
        raise AssertionError("should not spawn a launcher that does not exist")

    monkeypatch.setattr(ciq, "health", fake_health)
    monkeypatch.setattr(ciq, "_run_start_native_script", fake_run)

    result = await ciq.start_native(settings)

    assert result["status"] == "failed"
    assert result["executed"] is False
    assert "Native launcher not found" in result["error"]


def test_tool_is_wired_at_operator_authorized_tier():
    assert "calibration_iq_start_native" in TOOL_SCHEMAS
    registry = Registry("config/tools.yaml")
    assert registry.tier("calibration_iq_start_native") == "operator_authorized"
    names = {item["function"]["name"] for item in registry.profile_catalog("owner")}
    assert "calibration_iq_start_native" in names
