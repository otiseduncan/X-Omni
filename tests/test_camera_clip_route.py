"""
Tests for GET /api/camera-clips/{filename} -- serves only a motion clip this
app actually encoded and logged, to the Owner only.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.routes import create_router
from core.services import camera_monitoring
from core.state.db import Store


class _Router:
    def supports_vision(self):
        return True


class _Registry:
    policy = {}
    roots = []
    _handlers = {}

    @staticmethod
    def tier(_name):
        return "blocked"

    @staticmethod
    def public_approval(record, receipt=None):
        return record


def _app(store: Store, settings: SimpleNamespace, *, role: str = "owner") -> FastAPI:
    async def session():
        return {"google_sub": "owner", "user_id": "local-dev", "role": role}

    app = FastAPI()
    app.include_router(create_router(settings, store, _Router(), _Registry(), session))
    return app


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        local_origin="http://127.0.0.1:8100",
        public_origin="",
        vapid_public_key="k",
        camera_snapshot_dir=tmp_path / "snapshots",
    )


def _track_clip(store: Store, tmp_path: Path, settings, filename: str, content: bytes) -> None:
    clip_dir = settings.camera_snapshot_dir / camera_monitoring.CLIP_SUBDIR
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / filename).write_bytes(content)
    event_id = store.add_camera_event(
        trigger="motion", snapshot_filename="1787999495-motion.jpg", burst_id=1,
    )
    store.add_camera_motion_clip(
        burst_id=1, filename=filename, frame_count=1,
        first_event_id=event_id, last_event_id=event_id,
    )


def test_serves_a_tracked_clip(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(tmp_path / "clip-route.sqlite")
    _track_clip(store, tmp_path, settings, "motion-1.mp4", b"\x00\x00\x00 fake mp4")

    client = TestClient(_app(store, settings))
    response = client.get("/api/camera-clips/motion-1.mp4")
    assert response.status_code == 200
    assert response.content == b"\x00\x00\x00 fake mp4"
    assert response.headers["content-type"] == "video/mp4"


def test_refuses_a_clip_filename_never_logged_in_the_database(tmp_path: Path):
    settings = _settings(tmp_path)
    clip_dir = settings.camera_snapshot_dir / camera_monitoring.CLIP_SUBDIR
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / "motion-1.mp4").write_bytes(b"untracked")
    store = Store(tmp_path / "clip-route-untracked.sqlite")

    client = TestClient(_app(store, settings))
    response = client.get("/api/camera-clips/motion-1.mp4")
    assert response.status_code == 404


def test_refuses_path_traversal_and_unsafe_clip_filenames(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(tmp_path / "clip-route-traversal.sqlite")
    client = TestClient(_app(store, settings))
    for bad in ("../../etc/passwd", "motion-1.jpg", "not-a-clip.mp4", "motion-abc.mp4"):
        response = client.get(f"/api/camera-clips/{bad}")
        assert response.status_code in (404, 400), bad


def test_refuses_a_non_owner_session_for_clips(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(tmp_path / "clip-route-nonowner.sqlite")
    _track_clip(store, tmp_path, settings, "motion-1.mp4", b"\x00\x00\x00 fake mp4")

    client = TestClient(_app(store, settings, role="test_user"))
    response = client.get("/api/camera-clips/motion-1.mp4")
    assert response.status_code == 403
