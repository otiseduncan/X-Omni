"""
Tests for GET /api/camera-snapshots/{filename} -- serves only a snapshot
this app actually wrote and logged, to the Owner only.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.routes import create_router
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


def test_serves_a_tracked_snapshot(tmp_path: Path):
    settings = _settings(tmp_path)
    settings.camera_snapshot_dir.mkdir(parents=True, exist_ok=True)
    (settings.camera_snapshot_dir / "1787999495-interval.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")
    store = Store(tmp_path / "route.sqlite")
    store.add_camera_event(trigger="interval", snapshot_filename="1787999495-interval.jpg")

    client = TestClient(_app(store, settings))
    response = client.get("/api/camera-snapshots/1787999495-interval.jpg")
    assert response.status_code == 200
    assert response.content == b"\xff\xd8\xff fake jpeg"
    assert response.headers["content-type"] == "image/jpeg"


def test_refuses_a_filename_never_logged_in_the_database(tmp_path: Path):
    settings = _settings(tmp_path)
    settings.camera_snapshot_dir.mkdir(parents=True, exist_ok=True)
    # File exists on disk but was never recorded as a real captured event --
    # must not be servable just because a name happens to match the pattern.
    (settings.camera_snapshot_dir / "1787999495-interval.jpg").write_bytes(b"untracked")
    store = Store(tmp_path / "route-untracked.sqlite")

    client = TestClient(_app(store, settings))
    response = client.get("/api/camera-snapshots/1787999495-interval.jpg")
    assert response.status_code == 404


def test_refuses_path_traversal_and_unsafe_filenames(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(tmp_path / "route-traversal.sqlite")
    client = TestClient(_app(store, settings))
    for bad in ("../../etc/passwd", "1787999495-interval.png", "not-a-snapshot.jpg", "1787999495.jpg"):
        response = client.get(f"/api/camera-snapshots/{bad}")
        assert response.status_code in (404, 400), bad


def test_refuses_a_non_owner_session(tmp_path: Path):
    settings = _settings(tmp_path)
    settings.camera_snapshot_dir.mkdir(parents=True, exist_ok=True)
    (settings.camera_snapshot_dir / "1787999495-interval.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")
    store = Store(tmp_path / "route-nonowner.sqlite")
    store.add_camera_event(trigger="interval", snapshot_filename="1787999495-interval.jpg")

    client = TestClient(_app(store, settings, role="test_user"))
    response = client.get("/api/camera-snapshots/1787999495-interval.jpg")
    assert response.status_code == 403
