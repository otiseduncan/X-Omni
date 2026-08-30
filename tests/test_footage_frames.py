from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from core.services import footage_frames


def _jpeg_bytes(color: str = "black", size: tuple[int, int] = (320, 180)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_sample_times_includes_both_endpoints_and_is_chronological():
    since = datetime(2026, 8, 30, 6, 0, tzinfo=timezone.utc)
    until = since + timedelta(minutes=3)

    times = footage_frames.sample_times(since, until, footage_frames.MIN_SAMPLES)

    assert times[0] == since
    assert times[-1] == until
    assert times == sorted(times)
    assert len(times) == footage_frames.MIN_SAMPLES


def test_sample_times_clamps_below_the_minimum_and_above_the_maximum():
    since = datetime(2026, 8, 30, 6, 0, tzinfo=timezone.utc)
    until = since + timedelta(minutes=1)

    assert len(footage_frames.sample_times(since, until, 1)) == footage_frames.MIN_SAMPLES
    assert len(footage_frames.sample_times(since, until, 999)) == footage_frames.MAX_SAMPLES


def test_sample_times_handles_a_zero_length_window_without_dividing_by_zero():
    instant = datetime(2026, 8, 30, 6, 0, tzinfo=timezone.utc)
    times = footage_frames.sample_times(instant, instant, footage_frames.MIN_SAMPLES)
    assert times == [instant]


def test_contact_sheet_produces_one_valid_jpeg_from_multiple_frames():
    since = datetime(2026, 8, 30, 6, 0, tzinfo=timezone.utc)
    samples = [
        (since + timedelta(seconds=i * 10), _jpeg_bytes("black" if i % 2 else "white"))
        for i in range(footage_frames.MIN_SAMPLES)
    ]

    sheet_bytes = footage_frames.contact_sheet(samples)

    image = Image.open(io.BytesIO(sheet_bytes))
    image.load()
    assert image.format == "JPEG"
    assert image.width > 0 and image.height > 0


def test_contact_sheet_rejects_an_empty_sample_list():
    with pytest.raises(footage_frames.FrameExtractionError):
        footage_frames.contact_sheet([])


@pytest.mark.asyncio
async def test_build_contact_sheet_derives_a_bounded_sample_count_from_duration(tmp_path: Path, monkeypatch):
    since = datetime(2026, 8, 30, 6, 0, tzinfo=timezone.utc)
    until = since + timedelta(minutes=3)
    captured_offsets: list[float] = []

    async def fake_run_ffmpeg(ffmpeg_path, args, *, timeout_seconds):
        # -ss offset is always the argument right after "-ss".
        offset = float(args[args.index("-ss") + 1])
        captured_offsets.append(offset)
        frame_path = Path(args[-1])
        frame_path.write_bytes(_jpeg_bytes())
        return 0, b""

    monkeypatch.setattr(footage_frames, "_run_ffmpeg", fake_run_ffmpeg)

    result = await footage_frames.build_contact_sheet(
        b"fake-clip-bytes", since, since, until, ffmpeg_path=tmp_path / "ffmpeg.exe"
    )

    assert result["sample_count"] >= footage_frames.MIN_SAMPLES
    assert result["sample_count"] <= footage_frames.MAX_SAMPLES
    assert result["sampled_at"][0] == since
    assert result["sampled_at"][-1] == until
    assert captured_offsets[0] == pytest.approx(0.0)
    assert captured_offsets[-1] == pytest.approx(180.0)
    Image.open(io.BytesIO(result["contact_sheet"])).load()


@pytest.mark.asyncio
async def test_extract_frames_raises_when_ffmpeg_fails(tmp_path: Path, monkeypatch):
    since = datetime(2026, 8, 30, 6, 0, tzinfo=timezone.utc)
    times = footage_frames.sample_times(since, since + timedelta(minutes=1), footage_frames.MIN_SAMPLES)
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"not-really-a-clip")

    async def failing_run_ffmpeg(ffmpeg_path, args, *, timeout_seconds):
        return 1, b"decode error"

    monkeypatch.setattr(footage_frames, "_run_ffmpeg", failing_run_ffmpeg)

    with pytest.raises(footage_frames.FrameExtractionError):
        await footage_frames.extract_frames(clip_path, since, times, ffmpeg_path=tmp_path / "ffmpeg.exe")
