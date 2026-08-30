from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.services import exterior_camera as exterior_camera_svc
from core.services import mediamtx_config


def _profile(
    token: str, *, encoding: str, width: int, height: int, ordinal: int = 0
) -> exterior_camera_svc._OnvifProfile:
    return exterior_camera_svc._OnvifProfile(
        token=token, name=token, encoding=encoding, width=width, height=height, ordinal=ordinal
    )


class FakeExteriorCamera:
    """Stands in for ExteriorCameraService's ONVIF-facing private methods.

    discover_camera_path_plans() calls these same methods
    ExteriorCameraService itself uses for live view -- faking them here
    avoids a real SOAP round trip while still exercising the actual
    profile-selection logic under test.
    """

    def __init__(self, profiles: list, stream_urls: dict[str, str]):
        self.onvif_timeout_seconds = 5
        self._onvif_transport = None
        self._profiles = profiles
        self._stream_urls = stream_urls
        self.resolved_tokens: list[str] = []

    def _load_credentials(self):
        return SimpleNamespace(host="192.168.1.10")

    async def _post_onvif(self, client, *, credentials, operation, body_builder=None):
        return SimpleNamespace(operation=operation)

    def _profiles_from_response(self, body):
        return self._profiles

    def _stream_uri_from_response(self, body, *, host):
        # discover_camera_path_plans doesn't expose which profile a
        # GetStreamUri response is "for" directly to us -- track resolution
        # order via a queue so the test can still assert per-profile URLs.
        token = self._pending_tokens.pop(0)
        self.resolved_tokens.append(token)
        return self._stream_urls[token]


async def _discover(camera: FakeExteriorCamera, resolve_order: list[str]):
    camera._pending_tokens = list(resolve_order)
    return await mediamtx_config.discover_camera_path_plans(camera)


@pytest.mark.asyncio
async def test_main_is_the_highest_resolution_profile_and_live_is_smallest_h264():
    profiles = [
        _profile("main", encoding="H264", width=2304, height=1296),
        _profile("sub", encoding="H264", width=704, height=576),
        _profile("snapshot", encoding="JPEG", width=640, height=480),
    ]
    camera = FakeExteriorCamera(
        profiles,
        {
            "main": "rtsp://cam/main-stream",
            "sub": "rtsp://cam/sub-stream",
        },
    )

    plans = await _discover(camera, ["main", "sub"])

    assert [p.path_name for p in plans] == [mediamtx_config.PATH_MAIN, mediamtx_config.PATH_LIVE]
    assert plans[0].width == 2304 and plans[0].rtsp_source_url == "rtsp://cam/main-stream"
    assert plans[1].width == 704 and plans[1].rtsp_source_url == "rtsp://cam/sub-stream"


@pytest.mark.asyncio
async def test_no_live_path_when_no_profile_is_genuinely_h264():
    profiles = [
        _profile("main", encoding="H265", width=2304, height=1296),
        _profile("sub", encoding="H265", width=704, height=576),
    ]
    camera = FakeExteriorCamera(profiles, {"main": "rtsp://cam/main-stream"})

    plans = await _discover(camera, ["main"])

    assert [p.path_name for p in plans] == [mediamtx_config.PATH_MAIN]


@pytest.mark.asyncio
async def test_live_path_omitted_when_the_only_h264_profile_equals_main():
    profiles = [_profile("only", encoding="H264", width=1280, height=720)]
    camera = FakeExteriorCamera(profiles, {"only": "rtsp://cam/only-stream"})

    plans = await _discover(camera, ["only"])

    assert [p.path_name for p in plans] == [mediamtx_config.PATH_MAIN]


@pytest.mark.asyncio
async def test_snapshot_only_profiles_are_never_selected_as_main():
    profiles = [_profile("snap", encoding="JPEG", width=1920, height=1080)]
    camera = FakeExteriorCamera(profiles, {})

    with pytest.raises(mediamtx_config.MediaMTXConfigError):
        await _discover(camera, [])


def test_resolved_stream_url_never_appears_in_the_plan_repr():
    plan = mediamtx_config.CameraPathPlan(
        path_name="exterior",
        rtsp_source_url="rtsp://192.168.1.10:554/user=urwh_password=secretvalue_channel=0",
        profile_name="mainStream",
        encoding="H264",
        width=2304,
        height=1296,
    )
    assert "secretvalue" not in repr(plan)
    assert "password" not in repr(plan).casefold() or "rtsp_source_url" not in repr(plan)


def test_render_paths_block_includes_every_plan_and_a_denying_catch_all():
    plans = [
        mediamtx_config.CameraPathPlan(
            path_name="exterior", rtsp_source_url="rtsp://cam/main",
            profile_name="main", encoding="H264", width=2304, height=1296,
        ),
        mediamtx_config.CameraPathPlan(
            path_name="exterior_sub", rtsp_source_url="rtsp://cam/sub",
            profile_name="sub", encoding="H264", width=704, height=576,
        ),
    ]
    text = mediamtx_config.render_paths_block(plans)
    assert text.startswith("paths:\n")
    assert "  exterior:\n" in text
    assert "  exterior_sub:\n" in text
    assert "source: 'rtsp://cam/main'" in text
    assert "record: yes" in text
    assert "  all_others:\n    record: no\n" in text


def test_render_paths_block_escapes_single_quotes_in_the_source_url():
    plans = [
        mediamtx_config.CameraPathPlan(
            path_name="exterior", rtsp_source_url="rtsp://cam/it's-here",
            profile_name="main", encoding="H264", width=100, height=100,
        )
    ]
    text = mediamtx_config.render_paths_block(plans)
    assert "'rtsp://cam/it''s-here'" in text


def test_update_mediamtx_yaml_preserves_everything_above_the_paths_marker(tmp_path: Path):
    yaml_path = tmp_path / "mediamtx.yml"
    yaml_path.write_text(
        "logLevel: info\napi: yes\n\npaths:\n\n  exterior:\n    source: 'rtsp://old'\n",
        encoding="utf-8",
    )
    plans = [
        mediamtx_config.CameraPathPlan(
            path_name="exterior", rtsp_source_url="rtsp://new",
            profile_name="main", encoding="H264", width=100, height=100,
        )
    ]

    mediamtx_config.update_mediamtx_yaml(yaml_path, plans)

    updated = yaml_path.read_text(encoding="utf-8")
    assert updated.startswith("logLevel: info\napi: yes\n\npaths:")
    assert "rtsp://new" in updated
    assert "rtsp://old" not in updated
    # No stray temp file left behind after a successful atomic replace.
    assert list(tmp_path.glob(".*.tmp")) == []


def test_update_mediamtx_yaml_refuses_to_write_zero_plans(tmp_path: Path):
    yaml_path = tmp_path / "mediamtx.yml"
    yaml_path.write_text("paths:\n", encoding="utf-8")
    with pytest.raises(mediamtx_config.MediaMTXConfigError):
        mediamtx_config.update_mediamtx_yaml(yaml_path, [])
