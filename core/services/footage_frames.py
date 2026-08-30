"""Bounded, on-demand frame extraction for temporal DVR visual analysis.

This is the one place X Omni still runs FFmpeg for the DVR, and it is
deliberately narrow: given one already-bounded clip MediaMTX's Playback API
already built (see mediamtx_client.fetch_clip_bytes), pull a handful of
still frames out of it and assemble them into one chronological contact
sheet for the existing vision worker. It never decodes continuously, never
runs against a live/growing recording, and is only ever invoked for an
explicit temporal question -- not on every frame, not on a schedule.
"""

from __future__ import annotations

import asyncio
import io
import math
import os
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageOps

MIN_SAMPLES = 8
MAX_SAMPLES = 20
FRAME_TIMEOUT_SECONDS = 12
FRAME_WIDTH = 640


class FrameExtractionError(RuntimeError):
    """A bounded frame-extraction worker could not produce a trustworthy result."""


def sample_times(since: datetime, until: datetime, sample_count: int) -> list[datetime]:
    """Chronological, inclusive strategic sample times across [since, until].

    Including both endpoints is deliberate: a temporal conclusion needs a
    before-and-after view, not a cluster of near-duplicate middle frames.
    """
    count = min(max(int(sample_count), MIN_SAMPLES), MAX_SAMPLES)
    span_seconds = max(0.0, (until - since).total_seconds())
    if count == 1 or span_seconds <= 0:
        return [since]
    return [since + timedelta(seconds=span_seconds * index / (count - 1)) for index in range(count)]


async def _run_ffmpeg(ffmpeg_path: Path, args: list[str], *, timeout_seconds: float) -> tuple[int, bytes]:
    creationflags = 0x08000000 if os.name == "nt" else 0
    proc = await asyncio.create_subprocess_exec(
        str(ffmpeg_path), "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
    )
    try:
        stdout_data, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await asyncio.gather(proc.wait(), return_exceptions=True)
        return -1, b"Frame extraction timed out."
    return int(proc.returncode or 0), stderr_data or b""


_BACKOFF_SECONDS = (0.0, 1.5, 3.0)


async def _extract_one_frame(
    clip_path: Path, frame_path: Path, offset: float, *, ffmpeg_path: Path,
) -> bool:
    rc, _stderr = await _run_ffmpeg(
        ffmpeg_path,
        [
            "-y", "-ss", f"{offset:.3f}", "-i", str(clip_path),
            "-map", "0:v:0", "-frames:v", "1",
            "-vf", f"scale={FRAME_WIDTH}:-2:force_original_aspect_ratio=decrease",
            "-strict", "unofficial", "-q:v", "4", str(frame_path),
        ],
        timeout_seconds=FRAME_TIMEOUT_SECONDS,
    )
    return rc == 0 and frame_path.is_file() and frame_path.stat().st_size > 0


async def extract_frames(
    clip_path: Path,
    clip_started_at: datetime,
    times: list[datetime],
    *,
    ffmpeg_path: Path,
) -> list[tuple[datetime, bytes]]:
    """Extract one still JPEG per requested time from a local clip file.

    A requested offset landing exactly on (or near) a server-side segment
    stitch boundary can occasionally fail to decode even though the
    surrounding footage is fine -- nudging a few seconds earlier before
    giving up on that one sample keeps a transient stitch artifact from
    silently costing the "after" evidence frame the analysis prompt
    depends on.
    """
    extracted: list[tuple[datetime, bytes]] = []
    with tempfile.TemporaryDirectory(prefix=".xomni-footage-frames-") as temp:
        temp_dir = Path(temp)
        for index, captured_at in enumerate(times):
            base_offset = max(0.0, (captured_at - clip_started_at).total_seconds())
            for backoff in _BACKOFF_SECONDS:
                offset = max(0.0, base_offset - backoff)
                frame_path = temp_dir / f"frame-{index:02d}-{uuid.uuid4().hex[:6]}.jpg"
                if await _extract_one_frame(clip_path, frame_path, offset, ffmpeg_path=ffmpeg_path):
                    extracted.append((captured_at, frame_path.read_bytes()))
                    break
    if len(extracted) < MIN_SAMPLES:
        raise FrameExtractionError("Insufficient DVR frames were extracted for temporal analysis.")
    return extracted


def contact_sheet(samples: list[tuple[datetime, bytes]]) -> bytes:
    """Build one bounded chronological JPEG for the existing vision worker."""
    if not samples:
        raise FrameExtractionError("No DVR frames were available for analysis.")
    columns = min(3, len(samples))
    tile_width, tile_height, label_height, gutter = 400, 225, 24, 8
    rows = math.ceil(len(samples) / columns)
    sheet = Image.new(
        "RGB",
        (gutter + columns * (tile_width + gutter), gutter + rows * (tile_height + label_height + gutter)),
        "black",
    )
    draw = ImageDraw.Draw(sheet)
    resampling = getattr(Image, "Resampling", Image).LANCZOS
    for index, (captured_at, raw) in enumerate(samples):
        with Image.open(io.BytesIO(raw)) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            image.thumbnail((tile_width, tile_height), resampling)
            tile = Image.new("RGB", (tile_width, tile_height), "black")
            tile.paste(image, ((tile_width - image.width) // 2, (tile_height - image.height) // 2))
        column, row = index % columns, index // columns
        x = gutter + column * (tile_width + gutter)
        y = gutter + row * (tile_height + label_height + gutter)
        sheet.paste(tile, (x, y))
        timestamp = captured_at.astimezone().strftime("%H:%M:%S %Z")
        draw.text((x + 3, y + tile_height + 3), f"{index + 1}. {timestamp}", fill="white")
    encoded = io.BytesIO()
    sheet.save(encoded, format="JPEG", quality=88, optimize=True)
    return encoded.getvalue()


async def build_contact_sheet(
    clip_bytes: bytes,
    clip_started_at: datetime,
    since: datetime,
    until: datetime,
    *,
    ffmpeg_path: Path,
    sample_count: Optional[int] = None,
) -> dict:
    """End to end: bytes in, a contact sheet and its sample metadata out."""
    duration_seconds = (until - since).total_seconds()
    derived_count = max(MIN_SAMPLES, min(MAX_SAMPLES, math.ceil(duration_seconds / 12.0) + 1))
    count = derived_count if sample_count is None else min(max(int(sample_count), MIN_SAMPLES), MAX_SAMPLES)
    times = sample_times(since, until, count)
    with tempfile.TemporaryDirectory(prefix=".xomni-footage-clip-") as temp:
        clip_path = Path(temp) / "clip.mp4"
        clip_path.write_bytes(clip_bytes)
        samples = await extract_frames(clip_path, clip_started_at, times, ffmpeg_path=ffmpeg_path)
    sheet_bytes = await asyncio.to_thread(contact_sheet, samples)
    return {
        "contact_sheet": sheet_bytes,
        "sample_count": len(samples),
        "sampled_at": [value for value, _raw in samples],
    }
