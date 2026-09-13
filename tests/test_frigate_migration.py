"""Guards for the boundary this migration established.

X Omni is not an NVR. It does not record, it does not run a media server, it
does not supervise one, and it does not reach the camera directly. Frigate
owns all of that on its own machine and X is a client of its API.

These are static and structural checks rather than behavioural ones,
because the failure they protect against is re-growth: someone adding a
recorder back one convenient helper at a time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import Settings

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"

# Modules whose whole purpose was the retired local recorder stack.
RETIRED_MODULES = (
    "core/services/mediamtx_client.py",
    "core/services/mediamtx_config.py",
    "core/services/mediamtx_dvr.py",
    "core/services/mediamtx_dvr_routes.py",
    "core/services/exterior_camera.py",
    "core/services/onvif_motion.py",
    "core/services/camera_monitoring.py",
    "core/dvr_service.py",
)


def _python_sources() -> list[Path]:
    return [
        path
        for path in CORE.rglob("*.py")
        if "__pycache__" not in path.parts
    ]


def test_the_retired_recorder_modules_are_gone():
    for relative in RETIRED_MODULES:
        assert not (ROOT / relative).exists(), f"{relative} should have been removed"


def test_the_standalone_dvr_interface_is_gone():
    assert not (ROOT / "ui" / "dvr").exists(), "the standalone DVR GUI should be removed"


def test_no_runtime_module_imports_the_retired_stack():
    banned = (
        "mediamtx_client",
        "mediamtx_config",
        "mediamtx_dvr",
        "exterior_camera",
        "onvif_motion",
        "camera_monitoring",
        "dvr_service",
    )
    offenders: list[str] = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8")
        for name in banned:
            if f"import {name}" in text or f"from .{name}" in text:
                offenders.append(f"{path.relative_to(ROOT)} imports {name}")
    assert not offenders, offenders


def test_core_runs_no_recorder_and_supervises_no_media_server():
    # The words themselves are the guard: none of these belong anywhere in
    # the runtime once recording lives on another machine.
    banned = ("MediaMTX", "mediamtx", "rtsp://", "PATH_MAIN", "PATH_LIVE")
    offenders: list[str] = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8")
        for token in banned:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)} mentions {token!r}")
    assert not offenders, offenders


def test_settings_describe_frigate_and_no_local_recorder():
    settings = Settings.load()
    for attribute in (
        "frigate_base_url",
        "frigate_camera",
        "frigate_verify_tls",
        "frigate_timeout_seconds",
        "frigate_max_clip_seconds",
        "frigate_credential_path",
    ):
        assert hasattr(settings, attribute), attribute
    for retired in (
        "mediamtx_control_base_url",
        "mediamtx_recordings_root",
        "mediamtx_clips_root",
        "dvr_port",
        "internal_dvr_token",
        "camera_snapshot_dir",
        "camera_motion_threshold",
    ):
        assert not hasattr(settings, retired), f"{retired} should no longer exist"


def test_the_frigate_address_is_configuration_not_a_constant():
    # No machine address may be compiled into the code: the Ubuntu laptop's
    # LAN address, an mDNS name, or a Tailscale name must all be reachable by
    # changing one setting.
    offenders: list[str] = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8")
        for token in ("192.168.1.201", "naomi-hplaptop", "100.116.238"):
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)} hardcodes {token!r}")
    assert not offenders, offenders


def test_the_unauthenticated_frigate_port_is_never_the_default():
    from core.services import frigate_client

    assert frigate_client.DEFAULT_FRIGATE_PORT == 8971
    assert frigate_client.UNAUTHENTICATED_FRIGATE_PORT == 5000
    settings = Settings.load()
    # Whatever the operator configured, it must not be the unauthenticated API.
    if settings.frigate_base_url:
        assert ":5000" not in settings.frigate_base_url


def test_the_camera_tool_names_the_model_knows_are_unchanged():
    from core.services.camera_security import SECURITY_TOOL_SCHEMAS
    from core.tools.registry import TOOL_SCHEMAS

    assert set(SECURITY_TOOL_SCHEMAS) == {
        "camera_footage",
        "camera_snapshot_analyze",
        "camera_event_history",
    }
    # The exterior look-at-the-camera capability keeps its existing name so
    # prompting and tool routing did not have to change.
    assert "exterior_camera_request" in TOOL_SCHEMAS


def test_x_omni_starts_normally_while_frigate_is_unreachable(tmp_path, monkeypatch):
    """Frigate's machine can be asleep. X must boot anyway.

    Building the app performs no camera I/O at all, so an unreachable or
    entirely unconfigured recorder cannot delay or fail startup.
    """
    from core.main import build_app

    monkeypatch.setenv("FRIGATE_BASE_URL", "https://192.0.2.1:8971")  # TEST-NET-1
    settings = Settings.load()
    app = build_app(settings)
    assert app.state.frigate_client is not None
    assert app.state.surveillance is not None
    # Unreachable, and yet constructed and ready to report that.
    assert app.state.frigate_client.configured() in (True, False)


@pytest.mark.asyncio
async def test_a_camera_question_during_an_outage_answers_with_a_state(monkeypatch):
    """The whole point of the structured states: no invented observations."""
    from core.services import camera_security
    from core.services.frigate_client import FrigateState, FrigateUnavailable

    class DeadSurveillance:
        client = type("C", (), {"base_url": None, "camera": "exterior"})()

        async def status(self):
            return {
                "ok": False,
                "state": FrigateState.UNAVAILABLE,
                "recording": False,
                "source": "frigate",
                "detail": "unreachable",
            }

        async def events(self, **kwargs):
            raise FrigateUnavailable("the recorder is not reachable")

    result = await camera_security.camera_event_history({}, surveillance=DeadSurveillance())
    assert result["ok"] is False
    assert result["state"] == FrigateState.UNAVAILABLE
    assert result["items"] == []
