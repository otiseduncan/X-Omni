from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import pytest

from core.services import camera_security


class FakeStore:
    def __init__(self):
        self.added: list[dict] = []
        self.updated: list[tuple[int, dict]] = []
        self.marked: list[int] = []
        self.range_rows: list[dict] = []
        self.burst_rows: dict[int, list[dict]] = {}

    def get_max_camera_burst_id(self) -> int:
        return 0

    def add_camera_event(self, **values) -> int:
        self.added.append(values)
        return len(self.added)

    def update_camera_event_caption(self, event_id: int, **values) -> None:
        self.updated.append((event_id, values))

    def mark_camera_event_notified(self, event_id: int) -> None:
        self.marked.append(event_id)

    def list_camera_events(self, **_kwargs) -> list[dict]:
        return list(self.range_rows)

    def list_camera_events_by_burst(self, burst_id: int) -> list[dict]:
        return list(self.burst_rows.get(int(burst_id), []))


class FakeRouter:
    def supports_vision(self) -> bool:
        return True


class FakeDVR:
    def __init__(self, *, healthy: bool = False):
        self._events_healthy = healthy

    @property
    def events_healthy(self) -> bool:
        return self._events_healthy


def _settings(tmp_path: Path, **overrides):
    values = {
        "camera_snapshot_dir": tmp_path / "snapshots",
        "camera_monitor_interval_seconds": 60,
        "camera_baseline_interval_seconds": 600,
        "camera_snapshot_retention_days": 30,
        "camera_motion_threshold": 18.0,
        "camera_motion_burst_seconds": 10,
        "camera_motion_burst_interval_seconds": 1,
        "vapid_public_key": "public",
        "vapid_private_key": "private",
        "vapid_subject": "mailto:test@example.com",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _monitor(
    tmp_path: Path,
    *,
    store: FakeStore | None = None,
    camera=None,
    dvr=None,
):
    store = store or FakeStore()
    camera = camera or SimpleNamespace(capture_snapshot=AsyncMock(return_value=None))
    dvr = dvr or FakeDVR()
    monitor = camera_security.OnvifCameraMonitor(
        _settings(tmp_path), camera, FakeRouter(), store, dvr=dvr
    )
    return monitor, store


def _notification_body(*notifications: tuple[str, list[tuple[str, str]]]) -> ET.Element:
    body = ET.Element("Body")
    for topic, items in notifications:
        notification = ET.SubElement(body, "NotificationMessage")
        ET.SubElement(notification, "Topic").text = topic
        message = ET.SubElement(notification, "Message")
        for name, value in items:
            ET.SubElement(message, "SimpleItem", {"Name": name, "Value": value})
    return body


def test_xiongmai_parser_preserves_batch_order_and_ignores_channel_values():
    body = _notification_body(
        ("tns1:VideoSource/MotionAlarm", [("Channel", "1"), ("State", "false")]),
        ("tns1:VideoAnalytics/Vehicle", [("Channel", "1"), ("State", "true")]),
        ("tns1:VideoSource/MotionAlarm", [("State", "idle")]),
    )

    assert camera_security.XiongmaiDVR.motion_states_from_body(body) == [False, True, False]
    unrelated = _notification_body(
        ("tns1:Device/Relay", [("Channel", "1"), ("State", "true")]),
        ("tns1:Device/CardReader", [("State", "true")]),
    )
    assert camera_security.XiongmaiDVR.motion_states_from_body(unrelated) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caption", "expected_person", "expected_vehicle", "should_notify"),
    [
        ("PERSON: yes\nVEHICLE: no\nDESCRIPTION: a person", True, False, True),
        ("PERSON: no\nVEHICLE: yes\nDESCRIPTION: a truck", False, True, True),
        ("PERSON: yes\nVEHICLE: yes\nDESCRIPTION: a person by a van", True, True, True),
        ("PERSON: no\nVEHICLE: no\nDESCRIPTION: an empty driveway", False, False, False),
    ],
)
async def test_security_analysis_person_vehicle_matrix(
    tmp_path: Path,
    monkeypatch,
    caption: str,
    expected_person: bool,
    expected_vehicle: bool,
    should_notify: bool,
):
    monitor, store = _monitor(tmp_path)
    monkeypatch.setattr(
        camera_security.camera_svc,
        "caption_frame",
        AsyncMock(return_value=caption),
    )
    monitor._notify_security = AsyncMock(return_value=1)

    result = await monitor._analyze_security_frame(17, SimpleNamespace(raw=b"frame"))

    assert result == (expected_person, expected_vehicle)
    assert store.updated == [
        (
            17,
            {
                "caption": caption.split("DESCRIPTION:", 1)[1].strip(),
                "person_detected": expected_person,
                "vehicle_detected": expected_vehicle,
            },
        )
    ]
    assert monitor._notify_security.await_count == int(should_notify)
    assert store.marked == ([17] if should_notify else [])


@pytest.mark.parametrize(
    "caption",
    [
        "PERSON: yes or no\nVEHICLE: no\nDESCRIPTION: ambiguous",
        "PERSON: yesterday\nVEHICLE: no\nDESCRIPTION: malformed",
        "PERSON: yes\nDESCRIPTION: missing vehicle",
        "PERSON: yes\nVEHICLE: no\nDESCRIPTION:",
    ],
)
def test_security_caption_parser_never_promotes_malformed_yes_substrings(caption: str):
    person, vehicle, _description = camera_security._parse_security_caption(caption)
    assert person is None
    assert vehicle is None


@pytest.mark.asyncio
async def test_security_event_is_marked_notified_only_after_positive_delivery(
    tmp_path: Path, monkeypatch
):
    monitor, store = _monitor(tmp_path)
    monkeypatch.setattr(
        camera_security.camera_svc,
        "caption_frame",
        AsyncMock(return_value="PERSON: yes\nVEHICLE: no\nDESCRIPTION: a visitor"),
    )
    monitor._notify_security = AsyncMock(return_value=0)

    await monitor._analyze_security_frame(3, SimpleNamespace(raw=b"frame"))

    assert monitor._notify_security.await_count == 1
    assert store.marked == []


@pytest.mark.asyncio
async def test_pulse_events_reopen_a_burst_after_the_previous_window_expires(
    tmp_path: Path, monkeypatch
):
    clock = [0.0]
    monkeypatch.setattr(camera_security.time, "monotonic", lambda: clock[0])

    class PulseDVR(FakeDVR):
        async def motion_states(self):
            yield True
            clock[0] = 11.0
            yield True

    monitor, _store = _monitor(tmp_path, dvr=PulseDVR(healthy=True))
    monitor._capture_onvif_opening_frame = AsyncMock()

    await monitor._run_onvif_events()

    assert monitor._capture_onvif_opening_frame.await_count == 2
    assert monitor._current_burst_id == 2
    assert monitor._next_burst_id == 3


@pytest.mark.asyncio
async def test_second_look_retries_when_the_opening_capture_is_unavailable(
    tmp_path: Path, monkeypatch
):
    frame = SimpleNamespace(raw=b"second")
    camera = SimpleNamespace(capture_snapshot=AsyncMock(side_effect=[None, frame]))
    monitor, store = _monitor(tmp_path, camera=camera)
    monitor._current_burst_id = 8
    monitor._write_snapshot = lambda _raw, _trigger: "second-motion.jpg"
    monitor._analyze_security_frame = AsyncMock(return_value=(False, True))
    monkeypatch.setattr(camera_security, "_SECOND_LOOK_DELAY_SECONDS", 0)

    await monitor._capture_onvif_opening_frame()

    assert camera.capture_snapshot.await_count == 2
    assert monitor._analyze_security_frame.await_count == 1
    assert store.added[0]["burst_id"] == 8


@pytest.mark.asyncio
async def test_second_look_retries_a_malformed_analysis_and_keeps_positive_followup(
    tmp_path: Path, monkeypatch
):
    camera = SimpleNamespace(
        capture_snapshot=AsyncMock(
            side_effect=[SimpleNamespace(raw=b"first"), SimpleNamespace(raw=b"second")]
        )
    )
    monitor, store = _monitor(tmp_path, camera=camera)
    monitor._current_burst_id = 4
    monitor._write_snapshot = lambda raw, _trigger: f"{raw.decode()}-motion.jpg"
    monitor._notify_security = AsyncMock(return_value=1)
    monkeypatch.setattr(camera_security, "_SECOND_LOOK_DELAY_SECONDS", 0)
    monkeypatch.setattr(
        camera_security.camera_svc,
        "caption_frame",
        AsyncMock(
            side_effect=[
                "DESCRIPTION: the opening frame is unclear",
                "PERSON: no\nVEHICLE: yes\nDESCRIPTION: a truck entered",
            ]
        ),
    )

    await monitor._capture_onvif_opening_frame()

    assert camera.capture_snapshot.await_count == 2
    assert len(store.added) == 2
    assert store.updated[0][1]["person_detected"] is None
    assert store.updated[0][1]["vehicle_detected"] is None
    assert store.updated[1][1]["vehicle_detected"] is True
    assert store.marked == [2]


@pytest.mark.asyncio
async def test_event_supervisor_clears_stale_authority_and_restarts(
    tmp_path: Path, monkeypatch
):
    dvr = FakeDVR(healthy=True)
    monitor, _store = _monitor(tmp_path, dvr=dvr)
    monitor._onvif_motion_active = True
    calls = 0

    async def run_once_then_stop():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("consumer failed")
        monitor._stopped = True

    monitor._run_onvif_events = run_once_then_stop
    monkeypatch.setattr(camera_security, "_EVENT_CONSUMER_RESTART_SECONDS", 0)

    await monitor._supervise_onvif_events()

    assert calls == 2
    assert dvr.events_healthy is False
    assert monitor._onvif_motion_active is False


@pytest.mark.asyncio
async def test_onvif_tick_refreshes_frame_difference_baseline(tmp_path: Path, monkeypatch):
    frame = SimpleNamespace(raw=b"current-frame")
    camera = SimpleNamespace(capture_snapshot=AsyncMock(return_value=frame))
    monitor, _store = _monitor(tmp_path, camera=camera, dvr=FakeDVR(healthy=True))
    monitor._last_baseline_at = 0.0
    monitor._sweep_retention = lambda: None
    monkeypatch.setattr(camera_security.time, "monotonic", lambda: 1.0)

    await monitor._onvif_tick()

    assert monitor._previous_raw == b"current-frame"


@pytest.mark.asyncio
async def test_camera_history_normalizes_offset_bounds_for_dvr_sql(
    tmp_path: Path, monkeypatch
):
    store = FakeStore()
    legacy_history = AsyncMock(return_value={"ok": True, "items": []})
    monkeypatch.setattr(camera_security.legacy, "camera_event_history", legacy_history)
    dvr = SimpleNamespace(
        status=AsyncMock(return_value={"ok": True}),
        list_segments=AsyncMock(return_value=[]),
    )

    result = await camera_security.camera_event_history(
        store,
        {
            "since": "2026-08-29T18:00:00-04:00",
            "until": "2026-08-29T18:05:00-04:00",
            "include_recordings": True,
        },
        dvr=dvr,
    )

    assert legacy_history.await_args.args[1]["since"] == "2026-08-29 22:00:00"
    assert dvr.list_segments.await_args.kwargs == {
        "since": "2026-08-29T22:00:00Z",
        "until": "2026-08-29T22:05:00Z",
        "limit": 40,
    }
    assert result["dvr_url"] == "/dvr"


@pytest.mark.asyncio
async def test_time_range_uses_historical_timelapse_and_positive_later_caption(
    tmp_path: Path, monkeypatch
):
    store = FakeStore()
    store.range_rows = [
        {
            "id": 10,
            "trigger": "motion",
            "burst_id": 7,
            "captured_at": "2026-08-29 22:01:00",
        }
    ]
    store.burst_rows[7] = [
        {
            "id": 10,
            "captured_at": "2026-08-29 22:01:00",
            "caption": "an empty driveway",
            "person_detected": 0,
            "vehicle_detected": 0,
        },
        {
            "id": 11,
            "captured_at": "2026-08-29 22:01:02",
            "caption": "a truck entered",
            "person_detected": 0,
            "vehicle_detected": 1,
        },
    ]
    legacy_clip = AsyncMock(
        return_value={"ok": True, "burst_id": 7, "caption": "an empty driveway"}
    )
    monkeypatch.setattr(camera_security.legacy, "camera_motion_clip", legacy_clip)
    dvr = SimpleNamespace(
        range_clip=AsyncMock(side_effect=FileNotFoundError("no DVR segment"))
    )

    result = await camera_security.camera_motion_clip(
        store,
        _settings(tmp_path),
        tmp_path / "ffmpeg.exe",
        {
            "since": "2026-08-29T22:00:00Z",
            "until": "2026-08-29T22:05:00Z",
        },
        dvr=dvr,
    )

    assert legacy_clip.await_args.args[3] == {"event_id": 10}
    assert result["ok"] is True
    assert result["source"] == "stored_frame_timelapse"
    assert result["caption"] == "a truck entered"


@pytest.mark.asyncio
async def test_time_range_returns_truthful_completed_overlap_at_archive_boundary(
    tmp_path: Path,
):
    store = FakeStore()
    dvr = SimpleNamespace(
        range_clip=AsyncMock(
            side_effect=[
                FileNotFoundError("requested start predates archive"),
                Path("range-overlap.mp4"),
            ]
        ),
        list_segments=AsyncMock(
            return_value=[
                {
                    "started_at": "2026-08-29T21:31:58Z",
                    "ended_at": "2026-08-29T21:37:01Z",
                }
            ]
        ),
    )

    result = await camera_security.camera_motion_clip(
        store,
        _settings(tmp_path),
        tmp_path / "ffmpeg.exe",
        {
            "since": "2026-08-29T17:30:00-04:00",
            "until": "2026-08-29T17:35:00-04:00",
        },
        dvr=dvr,
    )

    second_call = dvr.range_clip.await_args_list[1]
    assert second_call.args[0].isoformat() == "2026-08-29T21:31:58+00:00"
    assert second_call.args[1].isoformat() == "2026-08-29T21:35:00+00:00"
    assert result["ok"] is True
    assert result["source"] == "continuous_dvr"
    assert result["partial"] is True
    assert result["clip_url"] == "/dvr/api/clips/range-overlap.mp4"
    assert "05:30:00 PM" in result["requested_started_at_local"]
    assert "Daylight" in result["requested_started_at_local"]


@pytest.mark.asyncio
async def test_continuous_event_clip_prefers_positive_later_caption(tmp_path: Path):
    store = FakeStore()
    store.burst_rows[7] = [
        {
            "captured_at": "2026-08-29 22:01:00",
            "caption": "an empty driveway",
            "person_detected": 0,
            "vehicle_detected": 0,
        },
        {
            "captured_at": "2026-08-29 22:01:02",
            "caption": "a van arrived",
            "person_detected": 0,
            "vehicle_detected": 1,
        },
    ]
    store.get_camera_event = lambda _event_id: {"burst_id": 7}
    dvr = SimpleNamespace(event_clip=AsyncMock(return_value=Path("motion-7.mp4")))

    result = await camera_security.camera_motion_clip(
        store,
        _settings(tmp_path),
        tmp_path / "ffmpeg.exe",
        {"event_id": 1},
        dvr=dvr,
    )

    assert result["source"] == "continuous_dvr"
    assert result["caption"] == "a van arrived"
