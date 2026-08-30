"""Coverage for the DVR-service-as-independent-process boundary:

- the internal-token credential that lets X Omni Core call the DVR
  service's API with no browser session of its own (`require_owner_or_
  internal` in `camera_dvr.create_router`),
- the new HTTP endpoints Core's `DVRServiceClient` depends on
  (range-clip prep, event-clip prep, analysis samples, saved clips),
- and `DVRServiceClient` itself, exercised against the real router over
  an in-process ASGI transport (no socket, no live DVR process).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core.services import camera_dvr
from core.services import camera_dvr_client

INTERNAL_TOKEN = "test-internal-token-0123456789"


def _app(tmp_path: Path, *, session: dict, internal_token: str = INTERNAL_TOKEN):
    async def require_session(request: Request) -> dict:
        return session

    camera = SimpleNamespace(ffmpeg_path=None)
    dvr = camera_dvr.CameraDVR(camera, root=tmp_path / "dvr", required_drive=None)
    settings = SimpleNamespace(
        root=Path(__file__).resolve().parents[1],
        camera_snapshot_dir=tmp_path / "snapshots",
        local_origin="http://127.0.0.1:8100",
        public_origin="",
    )
    store = SimpleNamespace(audit=lambda *_a, **_k: None)
    app = FastAPI()
    app.include_router(
        camera_dvr.create_router(
            settings, store, require_session, dvr,
            internal_token=internal_token,
            extra_allowed_origins=("http://127.0.0.1:8300",),
        )
    )
    return app, dvr


# --------------------------- require_owner_or_internal ---------------------------


def test_internal_token_grants_access_with_no_session_cookie(tmp_path: Path):
    app, dvr = _app(tmp_path, session={})  # empty session = not signed in
    dvr.status = AsyncMock(return_value={"recording": False})
    client = TestClient(app)

    denied = client.get("/dvr/api/status")
    assert denied.status_code == 403

    granted = client.get(
        "/dvr/api/status", headers={"X-XOmni-Internal-Token": INTERNAL_TOKEN}
    )
    assert granted.status_code == 200
    assert granted.json() == {"recording": False}


def test_wrong_internal_token_falls_back_to_session_and_is_denied(tmp_path: Path):
    app, dvr = _app(tmp_path, session={"role": "test_user"})
    dvr.status = AsyncMock(return_value={"recording": False})
    client = TestClient(app)
    response = client.get(
        "/dvr/api/status", headers={"X-XOmni-Internal-Token": "not-the-real-token"}
    )
    assert response.status_code == 403


def test_no_internal_token_configured_never_accepts_a_header(tmp_path: Path):
    # A DVR service started with an empty/unset token (should never happen in
    # practice -- Settings.load() always persists one) must not treat an
    # empty header as a match.
    app, dvr = _app(tmp_path, session={}, internal_token="")
    dvr.status = AsyncMock(return_value={"recording": False})
    client = TestClient(app)
    response = client.get("/dvr/api/status", headers={"X-XOmni-Internal-Token": ""})
    assert response.status_code in (401, 403)


# --------------------------- new prep endpoints ---------------------------


def _auth_headers():
    return {"X-XOmni-Internal-Token": INTERNAL_TOKEN}


def test_range_clip_endpoint_returns_bare_filename(tmp_path: Path):
    app, dvr = _app(tmp_path, session={})
    dvr.range_clip = AsyncMock(return_value=Path("range-100-200.mp4"))
    client = TestClient(app)
    response = client.post(
        "/dvr/api/clips/range",
        json={"since": "2026-08-30T06:00:00Z", "until": "2026-08-30T06:01:00Z"},
        headers=_auth_headers(),
    )
    assert response.status_code == 200
    assert response.json() == {"filename": "range-100-200.mp4"}


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (camera_dvr.PlaybackPreparationError("timed out"), 503),
        (FileNotFoundError("no coverage"), 404),
        (ValueError("bad range"), 400),
    ],
)
def test_range_clip_endpoint_maps_dvr_errors_to_http_status(
    tmp_path: Path, error: Exception, status: int
):
    app, dvr = _app(tmp_path, session={})
    dvr.range_clip = AsyncMock(side_effect=error)
    client = TestClient(app)
    response = client.post(
        "/dvr/api/clips/range",
        json={"since": "2026-08-30T06:00:00Z", "until": "2026-08-30T06:01:00Z"},
        headers=_auth_headers(),
    )
    assert response.status_code == status


def test_event_clip_endpoint_returns_bare_filename(tmp_path: Path):
    app, dvr = _app(tmp_path, session={})
    dvr.event_clip = AsyncMock(return_value=Path("motion-9.mp4"))
    client = TestClient(app)
    response = client.post("/dvr/api/events/9/clip", headers=_auth_headers())
    assert response.status_code == 200
    assert response.json() == {"filename": "motion-9.mp4"}


def test_analysis_samples_endpoint_base64_encodes_the_contact_sheet(tmp_path: Path):
    app, dvr = _app(tmp_path, session={})
    dvr.footage_analysis_samples = AsyncMock(return_value={
        "analyzed_started_at": "2026-08-30T06:43:00Z",
        "analyzed_ended_at": "2026-08-30T06:45:00Z",
        "sample_count": 2,
        "sampled_at": ["2026-08-30T06:43:00Z", "2026-08-30T06:45:00Z"],
        "contact_sheet": b"\xff\xd8\xff-not-a-real-jpeg",
        "source_segments": [],
    })
    client = TestClient(app)
    response = client.post(
        "/dvr/api/analysis/samples",
        json={"since": "2026-08-30T06:43:00Z", "until": "2026-08-30T06:45:00Z"},
        headers=_auth_headers(),
    )
    assert response.status_code == 200
    payload = response.json()
    assert "contact_sheet" not in payload
    assert payload["sample_count"] == 2
    import base64
    assert base64.b64decode(payload["contact_sheet_base64"]) == b"\xff\xd8\xff-not-a-real-jpeg"


# --------------------------- saved clips (Owner-only, no internal token) ---------------------------


def test_saved_clip_export_requires_exact_origin(tmp_path: Path):
    app, dvr = _app(tmp_path, session={"role": "owner"})
    dvr.export_clip = AsyncMock(return_value={
        "id": 1, "filename": "clip-1-2-abcdef012345.mp4", "title": None,
        "started_at": "2026-08-30T06:43:00Z", "ended_at": "2026-08-30T06:44:00Z", "bytes": 512,
    })
    client = TestClient(app)
    no_origin = client.post(
        "/dvr/api/clips/export",
        json={"since": "2026-08-30T06:43:00Z", "until": "2026-08-30T06:44:00Z"},
    )
    assert no_origin.status_code == 403

    with_origin = client.post(
        "/dvr/api/clips/export",
        json={"since": "2026-08-30T06:43:00Z", "until": "2026-08-30T06:44:00Z"},
        headers={"Origin": "http://127.0.0.1:8300"},
    )
    assert with_origin.status_code == 200
    assert with_origin.json()["video_url"] == "/dvr/api/clips-saved/1/video.mp4"


def test_saved_clips_list_and_missing_video_404(tmp_path: Path):
    app, dvr = _app(tmp_path, session={"role": "owner"})
    dvr.list_saved_clips = AsyncMock(return_value=[{
        "id": 1, "title": "Jeep moved", "started_at": "2026-08-30T06:43:00Z",
        "ended_at": "2026-08-30T06:44:00Z", "bytes": 512, "created_at": "2026-08-30T06:50:00Z",
    }])
    dvr.get_saved_clip = AsyncMock(return_value=None)
    client = TestClient(app)
    listed = client.get("/dvr/api/clips-saved")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["video_url"] == "/dvr/api/clips-saved/1/video.mp4"

    missing = client.get("/dvr/api/clips-saved/999/video.mp4")
    assert missing.status_code == 404


def test_saved_clip_delete_requires_exact_origin_and_is_explicit(tmp_path: Path):
    app, dvr = _app(tmp_path, session={"role": "owner"})
    dvr.delete_saved_clip = AsyncMock(return_value=True)
    client = TestClient(app)
    no_origin = client.delete("/dvr/api/clips-saved/1")
    assert no_origin.status_code == 403
    dvr.delete_saved_clip.assert_not_awaited()

    with_origin = client.delete(
        "/dvr/api/clips-saved/1", headers={"Origin": "http://127.0.0.1:8300"}
    )
    assert with_origin.status_code == 200
    dvr.delete_saved_clip.assert_awaited_once_with(1)


# --------------------------- DVRServiceClient, end-to-end over ASGI ---------------------------


def _client_for(app: FastAPI) -> camera_dvr_client.DVRServiceClient:
    return camera_dvr_client.DVRServiceClient(
        "http://dvr.internal",
        INTERNAL_TOKEN,
        transport=httpx.ASGITransport(app=app),
    )


@pytest.mark.asyncio
async def test_service_client_list_segments_strips_filename(tmp_path: Path):
    app, dvr = _app(tmp_path, session={})
    dvr.list_segments = AsyncMock(return_value=[
        {
            "id": 1, "filename": "20260830T060000000000Z-000000.mkv",
            "started_at": "2026-08-30T06:00:00Z", "ended_at": "2026-08-30T06:05:00Z", "complete": 1,
        },
    ])
    client = _client_for(app)
    rows = await client.list_segments(
        since="2026-08-30T00:00:00Z", until="2026-08-31T00:00:00Z", limit=10
    )
    assert rows == [{
        "id": 1, "started_at": "2026-08-30T06:00:00Z", "ended_at": "2026-08-30T06:05:00Z", "complete": 1,
    }]


@pytest.mark.asyncio
async def test_service_client_range_clip_returns_a_name_bearing_path(tmp_path: Path):
    from datetime import datetime, timezone
    app, dvr = _app(tmp_path, session={})
    dvr.range_clip = AsyncMock(return_value=Path("range-1-2.mp4"))
    client = _client_for(app)
    since = datetime(2026, 8, 30, 6, 43, tzinfo=timezone.utc)
    until = datetime(2026, 8, 30, 6, 44, tzinfo=timezone.utc)
    result = await client.range_clip(since, until, cache_name="ignored-by-server")
    assert result.name == "range-1-2.mp4"


@pytest.mark.asyncio
async def test_service_client_maps_http_status_to_the_same_exception_types(tmp_path: Path):
    from datetime import datetime, timezone
    app, dvr = _app(tmp_path, session={})
    dvr.range_clip = AsyncMock(side_effect=FileNotFoundError("no coverage"))
    client = _client_for(app)
    since = datetime(2026, 8, 30, 6, 43, tzinfo=timezone.utc)
    until = datetime(2026, 8, 30, 6, 44, tzinfo=timezone.utc)
    with pytest.raises(FileNotFoundError):
        await client.range_clip(since, until, cache_name="x")

    dvr.range_clip = AsyncMock(side_effect=camera_dvr.PlaybackPreparationError("timed out"))
    with pytest.raises(camera_dvr.PlaybackPreparationError):
        await client.range_clip(since, until, cache_name="x")


@pytest.mark.asyncio
async def test_service_client_footage_analysis_samples_round_trips_bytes(tmp_path: Path):
    from datetime import datetime, timezone
    app, dvr = _app(tmp_path, session={})
    dvr.footage_analysis_samples = AsyncMock(return_value={
        "analyzed_started_at": "2026-08-30T06:43:00Z",
        "analyzed_ended_at": "2026-08-30T06:45:00Z",
        "sample_count": 1,
        "sampled_at": ["2026-08-30T06:43:00Z"],
        "contact_sheet": b"\xff\xd8\xff-fake",
        "source_segments": [],
    })
    client = _client_for(app)
    since = datetime(2026, 8, 30, 6, 43, tzinfo=timezone.utc)
    until = datetime(2026, 8, 30, 6, 45, tzinfo=timezone.utc)
    result = await client.footage_analysis_samples(since, until)
    assert result["contact_sheet"] == b"\xff\xd8\xff-fake"
    assert result["sample_count"] == 1
