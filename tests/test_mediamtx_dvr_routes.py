from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.services import mediamtx_dvr_routes


class FakeClient:
    def __init__(self):
        self.path_status_result: Optional[dict] = {"ready": True}

    def whep_url(self, path: str) -> str:
        return f"http://127.0.0.1:8889/{path}/whep"

    def hls_playlist_url(self, path: str) -> str:
        return f"http://127.0.0.1:8888/{path}/index.m3u8"

    async def path_status(self, path: str):
        return self.path_status_result


class FakeDVR:
    def __init__(self, tmp_path: Path):
        self.clips_dir = tmp_path / "clips" / "_cache"
        self.saved_clips_dir = tmp_path / "clips" / "saved"
        self.clips_dir.mkdir(parents=True)
        self.saved_clips_dir.mkdir(parents=True)
        self.status_result = {"ok": True, "recording": True, "drive": {}, "last_error": None}
        self.segments: list[dict] = []
        self.export_calls: list[tuple] = []

    async def status(self):
        return self.status_result

    async def list_segments(self, *, since, until, limit):
        return self.segments

    async def range_clip(self, since, until, *, cache_name):
        path = self.clips_dir / f"{cache_name}.mp4"
        path.write_bytes(b"fake-clip")
        return path

    async def event_clip(self, store, burst_id):
        path = self.clips_dir / f"event-{burst_id}.mp4"
        path.write_bytes(b"fake-event-clip")
        return path

    async def export_clip(self, since, until, *, name):
        self.export_calls.append((since, until, name))
        path = self.saved_clips_dir / f"{name}.mp4"
        path.write_bytes(b"fake-export")
        return path

    def list_saved_clips(self):
        return [
            {"filename": p.name, "bytes": p.stat().st_size, "created_at": "2026-08-30T00:00:00Z"}
            for p in sorted(self.saved_clips_dir.glob("*.mp4"))
        ]

    def saved_clip_path(self, filename: str):
        candidate = self.saved_clips_dir / filename
        return candidate if candidate.is_file() else None

    def delete_saved_clip(self, filename: str) -> bool:
        candidate = self.saved_clip_path(filename)
        if candidate is None:
            return False
        candidate.unlink()
        return True


class FakeStore:
    def __init__(self):
        self.events: list[dict] = []

    def get_last_camera_event(self, *, trigger: str):
        return None

    def list_camera_events(self, **kwargs):
        return self.events

    def camera_snapshot_is_tracked(self, filename: str) -> bool:
        return filename == "123456789-motion.jpg"


def _owner_session() -> dict:
    return {"role": "owner"}


def _app(tmp_path: Path, *, role: str = "owner"):
    async def require_session():
        return {"role": role}

    dvr = FakeDVR(tmp_path)
    client = FakeClient()
    store = FakeStore()
    ui_root = tmp_path / "ui"
    ui_root.mkdir()
    (ui_root / "index.html").write_text("<html></html>")
    (ui_root / "app.js").write_text("// app")
    (ui_root / "style.css").write_text("body{}")
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()

    app = FastAPI()
    app.include_router(
        mediamtx_dvr_routes.create_router(
            require_session=require_session,
            client=client,
            dvr=dvr,
            store=store,
            ui_root=ui_root,
            camera_snapshot_dir=snapshot_dir,
        )
    )
    return app, dvr, store, snapshot_dir


@pytest.mark.asyncio
async def test_non_owner_session_is_rejected_on_every_route(tmp_path: Path):
    app, _dvr, _store, _snap = _app(tmp_path, role="guest")
    client = TestClient(app)

    assert client.get("/dvr/api/status").status_code == 403
    assert client.get("/dvr/api/recordings?since=2026-08-30T00:00:00Z&until=2026-08-31T00:00:00Z").status_code == 403


def test_status_reports_whep_and_hls_urls(tmp_path: Path):
    app, _dvr, _store, _snap = _app(tmp_path)
    client = TestClient(app)

    response = client.get("/dvr/api/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["recording"] is True
    assert payload["whep_url"] == "http://127.0.0.1:8889/exterior_sub/whep"
    assert payload["hls_url"] == "http://127.0.0.1:8888/exterior_sub/index.m3u8"


def test_recordings_requires_ordered_since_until(tmp_path: Path):
    app, _dvr, _store, _snap = _app(tmp_path)
    client = TestClient(app)

    response = client.get(
        "/dvr/api/recordings",
        params={"since": "2026-08-31T00:00:00Z", "until": "2026-08-30T00:00:00Z"},
    )
    assert response.status_code == 400


def test_clip_endpoint_streams_the_cached_file(tmp_path: Path):
    app, _dvr, _store, _snap = _app(tmp_path)
    client = TestClient(app)

    response = client.get(
        "/dvr/api/clip",
        params={"since": "2026-08-30T08:00:00Z", "until": "2026-08-30T08:05:00Z"},
    )
    assert response.status_code == 200
    assert response.content == b"fake-clip"
    assert response.headers["content-type"] == "video/mp4"


def test_export_clip_names_the_file_from_since_until_and_title(tmp_path: Path):
    app, dvr, _store, _snap = _app(tmp_path)
    client = TestClient(app)

    response = client.post(
        "/dvr/api/clips/export",
        json={"since": "2026-08-30T08:00:00Z", "until": "2026-08-30T08:05:00Z", "title": "front door"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert "front door" in payload["filename"]
    assert dvr.export_calls


def test_clips_saved_lists_only_saved_clips_not_the_scrub_cache(tmp_path: Path):
    app, dvr, _store, _snap = _app(tmp_path)
    (dvr.clips_dir / "range-1234.mp4").write_bytes(b"scratch")
    (dvr.saved_clips_dir / "export-keep.mp4").write_bytes(b"keep")
    client = TestClient(app)

    response = client.get("/dvr/api/clips-saved")

    assert response.status_code == 200
    filenames = {row["filename"] for row in response.json()["items"]}
    assert filenames == {"export-keep.mp4"}


def test_clips_saved_delete_returns_404_for_an_unknown_file(tmp_path: Path):
    app, _dvr, _store, _snap = _app(tmp_path)
    client = TestClient(app)

    response = client.delete("/dvr/api/clips-saved/does-not-exist.mp4")
    assert response.status_code == 404


def test_clips_saved_path_traversal_is_rejected(tmp_path: Path):
    app, dvr, _store, _snap = _app(tmp_path)
    (dvr.saved_clips_dir.parent / "outside.mp4").write_bytes(b"secret")
    client = TestClient(app)

    response = client.get("/dvr/api/clips-saved/..%2Foutside.mp4")
    assert response.status_code == 404


def test_camera_snapshot_route_enforces_the_tracked_filename_allowlist(tmp_path: Path):
    app, _dvr, _store, snapshot_dir = _app(tmp_path)
    (snapshot_dir / "123456789-motion.jpg").write_bytes(b"jpeg-bytes")
    (snapshot_dir / "not-tracked-motion.jpg").write_bytes(b"jpeg-bytes")
    client = TestClient(app)

    ok = client.get("/dvr/api/camera-snapshots/123456789-motion.jpg")
    assert ok.status_code == 200

    untracked = client.get("/dvr/api/camera-snapshots/999999999-motion.jpg")
    assert untracked.status_code == 404

    malformed = client.get("/dvr/api/camera-snapshots/not-tracked-motion.jpg")
    assert malformed.status_code == 404


def test_events_response_includes_a_snapshot_url_for_the_dvr_origin(tmp_path: Path):
    app, _dvr, store, _snap = _app(tmp_path)
    store.events = [
        {
            "trigger": "motion",
            "burst_id": 9,
            "captured_at": "2026-08-30 06:43:20",
            "snapshot_filename": "123456789-motion.jpg",
            "person_detected": 1,
            "vehicle_detected": 0,
        },
        {"trigger": "interval", "captured_at": "2026-08-30 06:00:00"},
    ]
    client = TestClient(app)

    response = client.get(
        "/dvr/api/events",
        params={"since": "2026-08-30T00:00:00Z", "until": "2026-08-31T00:00:00Z"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["snapshot_url"] == "/dvr/api/camera-snapshots/123456789-motion.jpg"
