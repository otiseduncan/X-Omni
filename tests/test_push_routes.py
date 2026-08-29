"""
Tests for the Web Push subscription routes: GET /api/push/public-key,
POST /api/push/subscribe, POST /api/push/unsubscribe.
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


def _app(store: Store, *, user_id: str = "local-dev") -> FastAPI:
    async def session():
        return {"google_sub": "owner", "user_id": user_id}

    settings = SimpleNamespace(
        local_origin="http://127.0.0.1:8100",
        public_origin="",
        vapid_public_key="test-public-key",
    )
    app = FastAPI()
    app.include_router(create_router(settings, store, _Router(), _Registry(), session))
    return app


def test_public_key_route_returns_the_configured_vapid_key(tmp_path: Path):
    store = Store(tmp_path / "push-key.sqlite")
    client = TestClient(_app(store))
    response = client.get("/api/push/public-key")
    assert response.status_code == 200
    assert response.json() == {"key": "test-public-key"}


def test_subscribe_then_unsubscribe_round_trips(tmp_path: Path):
    store = Store(tmp_path / "push-sub.sqlite")
    client = TestClient(_app(store))

    response = client.post(
        "/api/push/subscribe",
        json={
            "endpoint": "https://push.example.com/abc",
            "p256dh": "p256dh-value",
            "auth": "auth-value",
        },
    )
    assert response.status_code == 200
    subs = store.list_push_subscriptions("local-dev")
    assert len(subs) == 1
    assert subs[0]["endpoint"] == "https://push.example.com/abc"

    response = client.post(
        "/api/push/unsubscribe", json={"endpoint": "https://push.example.com/abc"},
    )
    assert response.status_code == 200
    assert store.list_push_subscriptions("local-dev") == []


def test_subscribe_rejects_a_non_https_endpoint(tmp_path: Path):
    store = Store(tmp_path / "push-reject.sqlite")
    client = TestClient(_app(store))
    response = client.post(
        "/api/push/subscribe",
        json={"endpoint": "http://push.example.com/abc", "p256dh": "x", "auth": "y"},
    )
    assert response.status_code == 400
    assert store.list_push_subscriptions("local-dev") == []


def test_resubscribing_the_same_endpoint_replaces_keys_instead_of_duplicating(tmp_path: Path):
    store = Store(tmp_path / "push-upsert.sqlite")
    client = TestClient(_app(store))
    endpoint = "https://push.example.com/abc"

    client.post("/api/push/subscribe", json={"endpoint": endpoint, "p256dh": "old", "auth": "old"})
    client.post("/api/push/subscribe", json={"endpoint": endpoint, "p256dh": "new", "auth": "new"})

    subs = store.list_push_subscriptions("local-dev")
    assert len(subs) == 1
    assert subs[0]["p256dh_key"] == "new"
    assert subs[0]["auth_key"] == "new"
