"""
Tests for core.services.camera_monitoring -- the background exterior-camera
tick loop (capture, motion diff, baseline documentation, retention) and the
two read-only history/analyze tools.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from core.services import camera as camera_svc
from core.services import camera_monitoring
from core.services import push_notifications
from core.state.db import Store


def _jpeg_frame(color=(10, 10, 10)) -> camera_svc.CameraFrame:
    buf = io.BytesIO()
    Image.new("RGB", (320, 240), color).save(buf, format="JPEG")
    raw = buf.getvalue()
    return camera_svc.CameraFrame(
        raw=raw, mime="image/jpeg", width=320, height=240,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


class FakeExteriorCamera:
    def __init__(self, frames):
        self._frames = list(frames)
        self.calls = 0

    async def capture_snapshot(self):
        self.calls += 1
        if not self._frames:
            return None
        return self._frames.pop(0)


class FakeRouter:
    def __init__(self, vision=True):
        self.vision = vision
        self.ensure_calls = 0

    def supports_vision(self):
        return self.vision

    async def ensure_capability(self, **kwargs):
        self.ensure_calls += 1
        self.vision = True


class FakeClock:
    def __init__(self, start: float = 1_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _settings(tmp_path: Path, **overrides) -> SimpleNamespace:
    base = dict(
        camera_snapshot_dir=tmp_path / "snapshots",
        camera_monitor_interval_seconds=60,
        camera_baseline_interval_seconds=600,
        camera_snapshot_retention_days=30,
        camera_motion_threshold=18.0,
        camera_motion_burst_seconds=90,
        camera_motion_burst_interval_seconds=5,
        vapid_public_key="pub",
        vapid_private_key="priv",
        vapid_subject="mailto:otiseduncan@gmail.com",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _monitor(tmp_path, store, frames, *, settings=None, router=None):
    settings = settings or _settings(tmp_path)
    camera = FakeExteriorCamera(frames)
    router = router or FakeRouter()
    monitor = camera_monitoring.CameraMonitor(settings, camera, router, store)
    return monitor, camera, router


@pytest.mark.asyncio
async def test_first_tick_stores_one_baseline_row_and_no_motion(tmp_path: Path):
    store = Store(tmp_path / "first.sqlite")
    monitor, camera, _router = _monitor(tmp_path, store, [_jpeg_frame()])
    await monitor._tick()

    events = store.list_camera_events(limit=10)
    assert len(events) == 1
    assert events[0]["trigger"] == "interval"
    assert camera.calls == 1


@pytest.mark.asyncio
async def test_a_very_different_second_frame_fires_a_motion_event(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "motion.sqlite")
    store.bind_owner("owner-sub", "otis@example.com", "Otis")
    clock = FakeClock()
    monkeypatch.setattr(camera_monitoring.time, "monotonic", clock)
    monitor, camera, router = _monitor(
        tmp_path, store, [_jpeg_frame((10, 10, 10)), _jpeg_frame((250, 250, 250))]
    )

    async def fake_caption(_router, _frame, _prompt):
        return "PERSON: yes\nVEHICLE: no\nDESCRIPTION: a person on the porch"

    monkeypatch.setattr(camera_svc, "caption_frame", fake_caption)
    sent = []

    async def fake_send(_store, _settings, user_id, title, body):
        sent.append((user_id, title, body))
        return 1

    monkeypatch.setattr(push_notifications, "send_push_async", fake_send)

    await monitor._tick()  # baseline only, sets _previous_raw
    clock.advance(1)
    await monitor._tick()  # very different frame -> motion

    events = store.list_camera_events(limit=10)
    triggers = sorted(e["trigger"] for e in events)
    assert triggers == ["interval", "motion"]
    motion_row = next(e for e in events if e["trigger"] == "motion")
    assert motion_row["person_detected"] == 1
    assert motion_row["vehicle_detected"] == 0
    assert motion_row["caption"] == "a person on the porch"
    assert motion_row["notified"] == 1
    assert len(sent) == 1
    assert sent[0][2] == "a person on the porch"


@pytest.mark.asyncio
async def test_identical_frames_never_fire_motion_or_captioning(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "nomotion.sqlite")
    clock = FakeClock()
    monkeypatch.setattr(camera_monitoring.time, "monotonic", clock)
    frame = _jpeg_frame()
    monitor, _camera, _router = _monitor(tmp_path, store, [frame, frame, frame])

    caption_calls = []

    async def fake_caption(*args):
        caption_calls.append(args)
        return "PERSON: yes\nVEHICLE: no\nDESCRIPTION: x"

    monkeypatch.setattr(camera_svc, "caption_frame", fake_caption)

    await monitor._tick()
    clock.advance(1)
    await monitor._tick()
    clock.advance(1)
    await monitor._tick()

    events = store.list_camera_events(limit=10)
    assert not any(e["trigger"] == "motion" for e in events)
    assert caption_calls == []


@pytest.mark.asyncio
async def test_sustained_motion_extends_the_burst_and_captions_only_once(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "burst.sqlite")
    clock = FakeClock()
    monkeypatch.setattr(camera_monitoring.time, "monotonic", clock)
    settings = _settings(tmp_path, camera_motion_burst_seconds=90, camera_motion_burst_interval_seconds=5)
    # Alternating extreme colors keep the frame-to-frame diff above
    # threshold on every tick, simulating continued activity.
    frames = [_jpeg_frame((0, 0, 0))] + [
        _jpeg_frame((255, 255, 255) if i % 2 == 0 else (0, 0, 0)) for i in range(6)
    ]
    monitor, _camera, _router = _monitor(tmp_path, store, frames, settings=settings)

    caption_calls = []

    async def fake_caption(*args):
        caption_calls.append(args)
        return "PERSON: yes\nVEHICLE: no\nDESCRIPTION: someone is moving around"

    monkeypatch.setattr(camera_svc, "caption_frame", fake_caption)

    await monitor._tick()  # baseline, no previous frame to diff against
    for _ in range(6):
        clock.advance(5)  # the burst-interval cadence
        await monitor._tick()

    motion_events = [e for e in store.list_camera_events(limit=20) if e["trigger"] == "motion"]
    assert len(motion_events) == 6
    assert len(caption_calls) == 1
    assert sum(e["notified"] for e in motion_events) == 1
    # Insertion order (by id) is authoritative -- captured_at has only
    # one-second SQLite resolution, so these rows can share a timestamp.
    first_by_id = min(motion_events, key=lambda e: e["id"])
    assert first_by_id["notified"] == 1


@pytest.mark.asyncio
async def test_burst_ends_once_activity_stops_and_the_window_elapses(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "burst-end.sqlite")
    clock = FakeClock()
    monkeypatch.setattr(camera_monitoring.time, "monotonic", clock)
    settings = _settings(tmp_path, camera_motion_burst_seconds=90, camera_motion_burst_interval_seconds=5)
    still_frame = _jpeg_frame((10, 10, 10))
    bright_frame = _jpeg_frame((250, 250, 250))
    # A single transition into "bright", then it stays bright (no further
    # frame-to-frame change) -- so once the 90s window elapses with nothing
    # new happening, the burst should actually close.
    monitor, _camera, _router = _monitor(
        tmp_path, store,
        [still_frame, bright_frame, bright_frame],
        settings=settings,
    )

    async def fake_caption(*args):
        return "PERSON: no\nVEHICLE: no\nDESCRIPTION: nothing"

    monkeypatch.setattr(camera_svc, "caption_frame", fake_caption)

    await monitor._tick()  # baseline
    clock.advance(1)
    await monitor._tick()  # still -> bright: motion fires, burst opens (burst_until = 1 + 90 = 91)
    assert monitor._burst_until is not None

    clock.advance(95)  # now = 96, well past 91, and bright -> bright shows no further motion
    await monitor._tick()
    assert monitor._burst_until is None

    motion_events = [e for e in store.list_camera_events(limit=20) if e["trigger"] == "motion"]
    assert len(motion_events) == 1  # only the frame that opened the burst


@pytest.mark.asyncio
async def test_run_forever_uses_the_fast_interval_only_while_a_burst_is_open(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "burst-interval.sqlite")
    settings = _settings(tmp_path, camera_motion_burst_seconds=90, camera_motion_burst_interval_seconds=5,
                          camera_monitor_interval_seconds=60)
    monitor, _camera, _router = _monitor(tmp_path, store, [], settings=settings)

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            monitor.stop()

    tick_states = [False, True]  # not in burst, then in burst

    async def fake_tick():
        monitor._burst_until = 123.0 if tick_states.pop(0) else None

    monkeypatch.setattr(monitor, "_tick", fake_tick)
    monkeypatch.setattr(camera_monitoring.asyncio, "sleep", fake_sleep)
    await monitor.run_forever()

    assert sleeps == [60, 5]


@pytest.mark.asyncio
async def test_baseline_only_fires_once_per_configured_interval(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "baseline.sqlite")
    clock = FakeClock()
    monkeypatch.setattr(camera_monitoring.time, "monotonic", clock)
    settings = _settings(tmp_path, camera_baseline_interval_seconds=600)
    frame = _jpeg_frame()
    monitor, _camera, _router = _monitor(tmp_path, store, [frame, frame, frame], settings=settings)

    await monitor._tick()
    clock.advance(60)
    await monitor._tick()
    clock.advance(600)
    await monitor._tick()

    baseline_events = [e for e in store.list_camera_events(limit=10) if e["trigger"] == "interval"]
    assert len(baseline_events) == 2


@pytest.mark.asyncio
async def test_a_missed_capture_is_skipped_without_crashing(tmp_path: Path):
    store = Store(tmp_path / "missed.sqlite")
    monitor, camera, _router = _monitor(tmp_path, store, [None])
    await monitor._tick()
    assert camera.calls == 1
    assert store.list_camera_events(limit=10) == []


@pytest.mark.asyncio
async def test_run_forever_survives_a_tick_exception_and_can_be_stopped(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "survive.sqlite")
    settings = _settings(tmp_path, camera_monitor_interval_seconds=0)
    monitor, _camera, _router = _monitor(tmp_path, store, [], settings=settings)

    call_count = {"n": 0}

    async def failing_tick():
        call_count["n"] += 1
        if call_count["n"] >= 2:
            monitor.stop()
        raise RuntimeError("boom")

    monkeypatch.setattr(monitor, "_tick", failing_tick)
    await monitor.run_forever()
    assert call_count["n"] >= 2


def test_retention_sweep_deletes_only_expired_rows_and_files(tmp_path: Path):
    store = Store(tmp_path / "retention.sqlite")
    settings = _settings(tmp_path, camera_snapshot_retention_days=30)
    directory = settings.camera_snapshot_dir
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "old.jpg").write_bytes(b"old")
    (directory / "new.jpg").write_bytes(b"new")

    old_id = store.add_camera_event(trigger="interval", snapshot_filename="old.jpg")
    store._exec(
        "UPDATE camera_events SET captured_at = datetime('now', '-40 days') WHERE id = ?",
        (old_id,),
    )
    store.add_camera_event(trigger="interval", snapshot_filename="new.jpg")

    monitor, _camera, _router = _monitor(tmp_path, store, [], settings=settings)
    monitor._sweep_retention()

    remaining = store.list_camera_events(limit=10)
    assert len(remaining) == 1
    assert remaining[0]["snapshot_filename"] == "new.jpg"
    assert not (directory / "old.jpg").exists()
    assert (directory / "new.jpg").exists()


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_camera_event_history_reports_truncation(tmp_path: Path):
    store = Store(tmp_path / "history.sqlite")
    for i in range(5):
        store.add_camera_event(trigger="interval", snapshot_filename=f"{i}.jpg")

    result = await camera_monitoring.camera_event_history(store, {"limit": 2})
    assert result["ok"] is True
    assert result["total_count"] == 5
    assert result["shown_count"] == 2
    assert result["truncated"] is True
    assert all("snapshot_url" in item for item in result["items"])
    assert result["items"][0]["snapshot_url"].startswith("/api/camera-snapshots/")
    assert all("captured_at_local" in item for item in result["items"])


def test_local_time_str_converts_naive_utc_to_local_and_never_raises():
    local = camera_monitoring._local_time_str("2026-08-29 10:31:35")
    # Exact wall-clock text depends on the test machine's timezone, but it
    # must actually be a conversion, not the raw UTC string echoed back.
    assert local != "2026-08-29 10:31:35"
    assert "2026-08-29" in local
    # Malformed input must degrade to the raw string, never raise.
    assert camera_monitoring._local_time_str("not a timestamp") == "not a timestamp"
    assert camera_monitoring._local_time_str(None) is None


@pytest.mark.asyncio
async def test_camera_snapshot_analyze_returns_cached_caption_without_reanalyzing(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "analyze-cached.sqlite")
    event_id = store.add_camera_event(trigger="motion", snapshot_filename="x.jpg", caption="already known")
    store.update_camera_event_caption(event_id, caption="already known", person_detected=True, vehicle_detected=False)

    called = []
    monkeypatch.setattr(camera_svc, "caption_frame", lambda *a: called.append(a))

    result = await camera_monitoring.camera_snapshot_analyze(
        store, FakeRouter(), _settings(tmp_path), {"event_id": event_id}
    )
    assert result["ok"] is True
    assert result["cached"] is True
    assert result["caption"] == "already known"
    assert called == []


@pytest.mark.asyncio
async def test_camera_snapshot_analyze_runs_and_caches_a_fresh_analysis(tmp_path: Path, monkeypatch):
    store = Store(tmp_path / "analyze-fresh.sqlite")
    settings = _settings(tmp_path)
    settings.camera_snapshot_dir.mkdir(parents=True, exist_ok=True)
    frame = _jpeg_frame()
    (settings.camera_snapshot_dir / "shot.jpg").write_bytes(frame.raw)
    event_id = store.add_camera_event(trigger="motion", snapshot_filename="shot.jpg")

    async def fake_caption(_router, _frame, _prompt):
        return "PERSON: no\nVEHICLE: yes\nDESCRIPTION: a van in the driveway"

    monkeypatch.setattr(camera_svc, "caption_frame", fake_caption)

    result = await camera_monitoring.camera_snapshot_analyze(
        store, FakeRouter(), settings, {"event_id": event_id}
    )
    assert result["ok"] is True
    assert result["cached"] is False
    assert result["caption"] == "a van in the driveway"
    assert result["vehicle_detected"] is True
    assert result["snapshot_url"] == "/api/camera-snapshots/shot.jpg"

    stored = store.get_camera_event(event_id)
    assert stored["caption"] == "a van in the driveway"


@pytest.mark.asyncio
async def test_camera_snapshot_analyze_reports_a_missing_file(tmp_path: Path):
    store = Store(tmp_path / "analyze-missing.sqlite")
    event_id = store.add_camera_event(trigger="motion", snapshot_filename="ghost.jpg")
    result = await camera_monitoring.camera_snapshot_analyze(
        store, FakeRouter(), _settings(tmp_path), {"event_id": event_id}
    )
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_camera_snapshot_analyze_rejects_an_unknown_event_id(tmp_path: Path):
    store = Store(tmp_path / "analyze-unknown.sqlite")
    result = await camera_monitoring.camera_snapshot_analyze(
        store, FakeRouter(), _settings(tmp_path), {"event_id": 999}
    )
    assert result["ok"] is False
