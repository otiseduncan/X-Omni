from __future__ import annotations

import json

import pytest

from core.services import calibration_iq_weekly_queue as queue_mod


def _item(repair_order_id: str, **values):
    return queue_mod.WeeklyQueueItem(repair_order_id=repair_order_id, **values)


def test_legacy_statuses_load_into_explicit_lifecycle_without_losing_failures():
    queue = queue_mod.WeeklyQueue.from_dict(
        {
            "conversation_id": "42",
            "updated_at": 123.0,
            "items": [
                {"repair_order_id": "queued", "status": "pending"},
                {"repair_order_id": "done", "status": "complete"},
                {"repair_order_id": "failed", "status": "failed"},
            ],
        }
    )

    assert [item.status for item in queue.items] == [
        queue_mod.STATUS_QUEUED,
        queue_mod.STATUS_COMPLETED,
        queue_mod.STATUS_BLOCKED,
    ]
    assert [item.repair_order_id for item in queue.unresolved()] == ["queued", "failed"]
    assert [item.repair_order_id for item in queue.failures()] == ["failed"]
    assert queue.items[2].updated_at == 123.0


def test_lifecycle_records_attempt_errors_and_timestamps():
    item = _item("ro-1", created_at=10.0, updated_at=10.0, status_changed_at=10.0)

    item.transition(queue_mod.STATUS_RUNNING, now=20.0, begin_attempt=True)
    assert item.status == queue_mod.STATUS_RUNNING
    assert item.attempts == 1
    assert item.last_attempt_at == 20.0
    assert item.status_changed_at == 20.0

    item.transition(
        queue_mod.STATUS_RETRYABLE,
        now=21.0,
        error={"code": "provider_timeout", "message": "Try again"},
    )
    assert item.status == queue_mod.STATUS_RETRYABLE
    assert "provider_timeout" in item.last_error
    assert item.updated_at == 21.0

    item.transition(queue_mod.STATUS_COMPLETED, now=30.0)
    assert item.completed_at == 30.0
    assert item.last_error == ""


def test_structured_status_queries_keep_every_unfinished_and_failed_row():
    queue = queue_mod.WeeklyQueue(
        conversation_id="7",
        items=[
            _item("queued", status="queued"),
            _item("running", status="running"),
            _item("auth", status="authentication_required", last_error="Sign in"),
            _item("retry", status="retryable", last_error="Timeout"),
            _item("blocked", status="blocked", last_error="No authoritative match"),
            _item("done", status="completed"),
        ],
    )

    assert {item.repair_order_id for item in queue.unresolved()} == {
        "queued",
        "running",
        "auth",
        "retry",
        "blocked",
    }
    assert {item.repair_order_id for item in queue.failures()} == {
        "auth",
        "retry",
        "blocked",
    }
    assert {item.repair_order_id for item in queue.actionable()} == {
        "queued",
        "auth",
        "retry",
    }


def test_store_replaces_atomically_and_round_trips(tmp_path):
    path = tmp_path / "queue.json"
    store = queue_mod.WeeklyQueueStore(path)
    store.save(queue_mod.WeeklyQueue(conversation_id="1", items=[_item("ro-1")]))

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["1"]["items"][0]["status"] == queue_mod.STATUS_QUEUED
    assert store.get("1").items[0].repair_order_id == "ro-1"
    assert not list(tmp_path.glob(".queue.json.*.tmp"))


def test_failed_atomic_replace_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "queue.json"
    store = queue_mod.WeeklyQueueStore(path)
    store.save(queue_mod.WeeklyQueue(conversation_id="1", items=[_item("original")]))
    original = path.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(queue_mod.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        store.save(queue_mod.WeeklyQueue(conversation_id="1", items=[_item("new")]))

    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".queue.json.*.tmp"))


def test_store_bounds_queue_and_conversation_counts(tmp_path, monkeypatch):
    store = queue_mod.WeeklyQueueStore(tmp_path / "queue.json")
    with pytest.raises(ValueError, match="limited"):
        store.save(
            queue_mod.WeeklyQueue(
                conversation_id="oversized",
                items=[_item(str(index)) for index in range(queue_mod.MAX_QUEUE_ITEMS + 1)],
            )
        )

    monkeypatch.setattr(queue_mod, "MAX_STORED_CONVERSATIONS", 2)
    store.save(queue_mod.WeeklyQueue(conversation_id="oldest", items=[_item("1")]))
    store.save(queue_mod.WeeklyQueue(conversation_id="middle", items=[_item("2")]))
    store.save(queue_mod.WeeklyQueue(conversation_id="latest", items=[_item("3")]))

    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert set(saved) == {"middle", "latest"}


def test_store_restart_recovers_stale_running_row_to_retryable(tmp_path, monkeypatch):
    path = tmp_path / "queue.json"
    monkeypatch.setattr(queue_mod.time, "time", lambda: 1_000.0)
    store = queue_mod.WeeklyQueueStore(path)
    item = _item("ro-1")
    item.transition(queue_mod.STATUS_RUNNING, now=1_000.0, begin_attempt=True)
    store.save(queue_mod.WeeklyQueue(conversation_id="crashed", items=[item]))

    # A new store instance models a fresh process reading the state left by
    # the interrupted attempt after the documented recovery window.
    recovered_at = 1_000.0 + queue_mod.RUNNING_STALE_AFTER_SECONDS + 1
    monkeypatch.setattr(queue_mod.time, "time", lambda: recovered_at)
    restarted_store = queue_mod.WeeklyQueueStore(path)
    recovered = restarted_store.get("crashed")

    assert recovered is not None
    assert recovered.items[0].status == queue_mod.STATUS_RETRYABLE
    assert recovered.items[0].attempts == 1
    assert recovered.items[0].last_attempt_at == 1_000.0
    assert "30-minute recovery window" in recovered.items[0].last_error
    assert recovered.items[0].status_changed_at == recovered_at

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["crashed"]["items"][0]["status"] == queue_mod.STATUS_RETRYABLE
    # Automatic recovery must not manufacture a fresh live-audit timestamp.
    assert persisted["crashed"]["updated_at"] == 1_000.0


def test_running_row_is_not_recovered_at_or_before_bounded_window():
    item = _item(
        "ro-1",
        status=queue_mod.STATUS_RUNNING,
        updated_at=100.0,
        status_changed_at=100.0,
        last_attempt_at=100.0,
    )
    queue = queue_mod.WeeklyQueue(
        conversation_id="active",
        items=[item],
        updated_at=100.0,
    )

    assert queue.recover_stale_running(
        now=100.0 + queue_mod.RUNNING_STALE_AFTER_SECONDS
    ) == 0
    assert item.status == queue_mod.STATUS_RUNNING
