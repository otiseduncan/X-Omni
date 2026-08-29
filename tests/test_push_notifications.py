"""
Tests for core.services.push_notifications.send_push -- delivery and
pruning of expired subscriptions.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pywebpush import WebPushException

from core.services import push_notifications
from core.state.db import Store


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        vapid_public_key="pub",
        vapid_private_key="priv",
        vapid_subject="mailto:otiseduncan@gmail.com",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _subscribe(store: Store, endpoint: str, *, user_id: str = "local-dev") -> None:
    store.add_push_subscription(
        user_id=user_id, endpoint=endpoint, p256dh_key="p256dh", auth_key="auth",
    )


def test_send_push_does_nothing_without_configured_vapid_keys(tmp_path: Path):
    store = Store(tmp_path / "push-noconfig.sqlite")
    _subscribe(store, "https://push.example.com/1")
    sent = push_notifications.send_push(
        store, _settings(vapid_public_key="", vapid_private_key=""),
        "local-dev", "title", "body",
    )
    assert sent == 0


def test_send_push_delivers_to_every_stored_subscription(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "push-deliver.sqlite")
    _subscribe(store, "https://push.example.com/1")
    _subscribe(store, "https://push.example.com/2")

    calls = []

    def fake_webpush(**kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(push_notifications, "webpush", fake_webpush)
    sent = push_notifications.send_push(store, _settings(), "local-dev", "Hi", "There")
    assert sent == 2
    assert {c["subscription_info"]["endpoint"] for c in calls} == {
        "https://push.example.com/1",
        "https://push.example.com/2",
    }
    assert all(c["vapid_private_key"] == "priv" for c in calls)


def test_send_push_prunes_a_410_gone_subscription(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "push-prune.sqlite")
    _subscribe(store, "https://push.example.com/dead")
    _subscribe(store, "https://push.example.com/alive")

    def fake_webpush(**kwargs):
        if kwargs["subscription_info"]["endpoint"].endswith("dead"):
            raise WebPushException("gone", response=SimpleNamespace(status_code=410))
        return "ok"

    monkeypatch.setattr(push_notifications, "webpush", fake_webpush)
    sent = push_notifications.send_push(store, _settings(), "local-dev", "Hi", "There")

    assert sent == 1
    remaining = {s["endpoint"] for s in store.list_push_subscriptions("local-dev")}
    assert remaining == {"https://push.example.com/alive"}


def test_send_push_keeps_a_subscription_on_a_non_expiry_failure(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "push-keep.sqlite")
    _subscribe(store, "https://push.example.com/flaky")

    def fake_webpush(**kwargs):
        raise WebPushException("server error", response=SimpleNamespace(status_code=500))

    monkeypatch.setattr(push_notifications, "webpush", fake_webpush)
    sent = push_notifications.send_push(store, _settings(), "local-dev", "Hi", "There")

    assert sent == 0
    assert len(store.list_push_subscriptions("local-dev")) == 1


@pytest.mark.asyncio
async def test_send_push_async_offloads_to_a_thread(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "push-async.sqlite")
    _subscribe(store, "https://push.example.com/1")

    monkeypatch.setattr(push_notifications, "webpush", lambda **kwargs: "ok")
    sent = await push_notifications.send_push_async(store, _settings(), "local-dev", "Hi", "There")
    assert sent == 1
