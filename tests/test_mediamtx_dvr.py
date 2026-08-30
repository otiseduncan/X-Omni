from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock

import pytest

from core.services import mediamtx_dvr
from core.services.mediamtx_client import (
    MediaMTXInvalidRequest,
    MediaMTXNotFound,
    MediaMTXUnavailable,
    RecordingSpan,
)


class FakeClient:
    def __init__(self):
        self.path_status_result: Optional[dict] = {"ready": True}
        self.list_recordings_result: list[RecordingSpan] = []
        self.fetch_clip_bytes_result: bytes = b"fake-mp4"
        self.fetch_clip_bytes_error: Optional[Exception] = None
        self.fetch_calls: list[tuple] = []

    async def path_status(self, path: str):
        return self.path_status_result

    async def list_recordings(self, path, since, until):
        return self.list_recordings_result

    async def fetch_clip_bytes(self, path, since, duration_seconds, *, container="mp4"):
        self.fetch_calls.append((path, since, duration_seconds))
        if self.fetch_clip_bytes_error is not None:
            raise self.fetch_clip_bytes_error
        return self.fetch_clip_bytes_result


def _dvr(tmp_path: Path, client: FakeClient) -> mediamtx_dvr.MediaMTXDVR:
    return mediamtx_dvr.MediaMTXDVR(
        client,
        ffmpeg_path=tmp_path / "ffmpeg.exe",
        recordings_root=tmp_path / "recordings",
        clips_dir=tmp_path / "clips" / "_cache",
        saved_clips_dir=tmp_path / "clips" / "saved",
    )


class FakeStore:
    def __init__(self, burst_rows: dict[int, list[dict]]):
        self._burst_rows = burst_rows

    def list_camera_events_by_burst(self, burst_id: int):
        return list(self._burst_rows.get(int(burst_id), []))


@pytest.mark.asyncio
async def test_status_reports_recording_true_when_the_path_is_ready(tmp_path: Path):
    client = FakeClient()
    dvr = _dvr(tmp_path, client)
    (tmp_path / "recordings").mkdir()

    status = await dvr.status()

    assert status["ok"] is True
    assert status["recording"] is True
    assert status["last_error"] is None
    assert status["drive"]["path"] == str(tmp_path / "recordings")


@pytest.mark.asyncio
async def test_status_reports_not_recording_when_the_path_has_no_source(tmp_path: Path):
    client = FakeClient()
    client.path_status_result = None
    dvr = _dvr(tmp_path, client)

    status = await dvr.status()

    assert status["ok"] is False
    assert status["recording"] is False
    assert "not currently connected" in status["last_error"]


@pytest.mark.asyncio
async def test_list_segments_shapes_recording_spans_into_dict_rows(tmp_path: Path):
    client = FakeClient()
    client.list_recordings_result = [
        RecordingSpan(started_at=datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc), duration_seconds=120.0)
    ]
    dvr = _dvr(tmp_path, client)

    rows = await dvr.list_segments(since="2026-08-30T00:00:00Z", until="2026-08-31T00:00:00Z")

    assert rows == [
        {
            "started_at": "2026-08-30T08:00:00Z",
            "ended_at": "2026-08-30T08:02:00Z",
            "duration_seconds": 120.0,
            "complete": True,
        }
    ]


@pytest.mark.asyncio
async def test_range_clip_writes_and_caches_the_fetched_bytes(tmp_path: Path):
    client = FakeClient()
    dvr = _dvr(tmp_path, client)
    since = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    until = datetime(2026, 8, 30, 8, 5, tzinfo=timezone.utc)

    path = await dvr.range_clip(since, until, cache_name="range-test")

    assert path.is_file()
    assert path.read_bytes() == b"fake-mp4"
    assert len(client.fetch_calls) == 1

    # A second request for the same window must reuse the cached file --
    # no second network round trip to MediaMTX's Playback API.
    path_again = await dvr.range_clip(since, until, cache_name="range-test")
    assert path_again == path
    assert len(client.fetch_calls) == 1


@pytest.mark.asyncio
async def test_range_clip_rejects_end_before_start_without_a_network_call(tmp_path: Path):
    client = FakeClient()
    dvr = _dvr(tmp_path, client)
    since = datetime(2026, 8, 30, 8, 5, tzinfo=timezone.utc)
    until = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError):
        await dvr.range_clip(since, until, cache_name="bad-range")
    assert client.fetch_calls == []


@pytest.mark.asyncio
async def test_range_clip_maps_mediamtx_errors_to_the_dvr_facing_exceptions(tmp_path: Path):
    since = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    until = datetime(2026, 8, 30, 8, 5, tzinfo=timezone.utc)

    client = FakeClient()
    client.fetch_clip_bytes_error = MediaMTXNotFound("no recording")
    with pytest.raises(FileNotFoundError):
        await _dvr(tmp_path, client).range_clip(since, until, cache_name="a")

    client = FakeClient()
    client.fetch_clip_bytes_error = MediaMTXUnavailable("down")
    with pytest.raises(mediamtx_dvr.PlaybackPreparationError):
        await _dvr(tmp_path, client).range_clip(since, until, cache_name="b")

    client = FakeClient()
    client.fetch_clip_bytes_error = MediaMTXInvalidRequest("bad")
    with pytest.raises(ValueError):
        await _dvr(tmp_path, client).range_clip(since, until, cache_name="c")


@pytest.mark.asyncio
async def test_event_clip_derives_a_padded_window_from_the_burst_frame_timestamps(tmp_path: Path):
    client = FakeClient()
    dvr = _dvr(tmp_path, client)
    store = FakeStore({9: [
        {"captured_at": "2026-08-30 06:43:20"},
        {"captured_at": "2026-08-30 06:44:01"},
    ]})

    await dvr.event_clip(store, 9)

    assert len(client.fetch_calls) == 1
    _path, since, duration = client.fetch_calls[0]
    assert since.isoformat() == "2026-08-30T06:42:50+00:00"
    assert duration == pytest.approx(30 + 41 + 75)


@pytest.mark.asyncio
async def test_event_clip_raises_not_found_for_an_unknown_burst(tmp_path: Path):
    client = FakeClient()
    dvr = _dvr(tmp_path, client)
    store = FakeStore({})

    with pytest.raises(FileNotFoundError):
        await dvr.event_clip(store, 404)


@pytest.mark.asyncio
async def test_export_clip_lands_in_saved_clips_dir_not_the_scrub_cache(tmp_path: Path):
    client = FakeClient()
    dvr = _dvr(tmp_path, client)
    since = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    until = datetime(2026, 8, 30, 8, 5, tzinfo=timezone.utc)

    path = await dvr.export_clip(since, until, name="my-export")

    assert path.parent == dvr.saved_clips_dir
    assert path.parent != dvr.clips_dir
    saved = dvr.list_saved_clips()
    assert [row["filename"] for row in saved] == [path.name]


@pytest.mark.asyncio
async def test_saved_clip_path_rejects_traversal_and_unknown_files(tmp_path: Path):
    client = FakeClient()
    dvr = _dvr(tmp_path, client)
    dvr.saved_clips_dir.mkdir(parents=True)

    assert dvr.saved_clip_path("../../etc/passwd") is None
    assert dvr.saved_clip_path("does-not-exist.mp4") is None
    assert dvr.delete_saved_clip("does-not-exist.mp4") is False


@pytest.mark.asyncio
async def test_footage_analysis_samples_maps_not_found_without_calling_ffmpeg(tmp_path: Path, monkeypatch):
    client = FakeClient()
    client.fetch_clip_bytes_error = MediaMTXNotFound("no coverage")
    dvr = _dvr(tmp_path, client)
    since = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    until = datetime(2026, 8, 30, 8, 3, tzinfo=timezone.utc)

    with pytest.raises(FileNotFoundError):
        await dvr.footage_analysis_samples(since, until)


@pytest.mark.asyncio
async def test_footage_analysis_samples_rejects_a_window_over_the_bounded_limit(tmp_path: Path):
    client = FakeClient()
    dvr = _dvr(tmp_path, client)
    since = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    until = since.replace(hour=9)

    with pytest.raises(ValueError):
        await dvr.footage_analysis_samples(since, until)
    assert client.fetch_calls == []
