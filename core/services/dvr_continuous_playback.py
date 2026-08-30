"""Continuous browser playback over the DVR's five-minute archive segments.

The recorder intentionally keeps small native MKV files for crash resilience and
retention. Human playback must not expose those file boundaries. This module
selects the contiguous completed run that covers an absolute timestamp, trims
normal recorder overlap at each boundary, and feeds FFmpeg one concat manifest.
FFmpeg emits one fragmented H.264 MP4 stream to the browser, so a single <video>
source keeps playing until a genuine recording gap or the end of retained data.

The archive is never modified and the recorder remains native stream-copy. The
CPU transcode exists only while a human is actively watching the standalone DVR.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from . import camera_dvr as camera_dvr_svc


STREAM_CHUNK_BYTES = 256 * 1024
MAX_STREAM_SEGMENTS = camera_dvr_svc.MAX_UI_SEGMENTS
MAX_CONTIGUOUS_GAP_SECONDS = camera_dvr_svc.MAX_PLAYBACK_GAP_SECONDS


@dataclass(frozen=True)
class PlaybackPart:
    row: dict[str, Any]
    path: Path
    inpoint_seconds: float


def _parse_utc(value: object) -> datetime:
    parsed = camera_dvr_svc._parse_time(value)
    if parsed is None:
        raise FileNotFoundError("DVR segment timestamp is invalid.")
    return parsed.astimezone(timezone.utc)


def _select_contiguous_rows(
    rows: list[dict[str, Any]], start: datetime
) -> list[tuple[dict[str, Any], float]]:
    """Return rows from ``start`` through the first real archive gap.

    The first tuple's float is the offset into the first source. Later values
    trim any overlap with the preceding source, so normal 1-2 second recorder
    overlap never appears to the operator as a rewind at a five-minute boundary.
    """
    start = start.astimezone(timezone.utc)
    ordered = sorted(
        rows,
        key=lambda row: (
            _parse_utc(row.get("started_at")), int(row.get("id") or 0)
        ),
    )
    first_index = None
    for index, row in enumerate(ordered):
        row_start = _parse_utc(row.get("started_at"))
        row_end = _parse_utc(row.get("ended_at"))
        if row_start <= start < row_end:
            first_index = index
            break
    if first_index is None:
        return []

    selected: list[tuple[dict[str, Any], float]] = []
    first = ordered[first_index]
    first_start = _parse_utc(first.get("started_at"))
    first_end = _parse_utc(first.get("ended_at"))
    selected.append((first, max(0.0, (start - first_start).total_seconds())))
    coverage_end = first_end

    for row in ordered[first_index + 1 :]:
        row_start = _parse_utc(row.get("started_at"))
        row_end = _parse_utc(row.get("ended_at"))
        gap = (row_start - coverage_end).total_seconds()
        if gap > MAX_CONTIGUOUS_GAP_SECONDS:
            break
        overlap = max(0.0, (coverage_end - row_start).total_seconds())
        row_duration = max(0.0, (row_end - row_start).total_seconds())
        # A pathological/duplicate row fully covered by the previous file adds
        # no new footage and should not be handed to concat at all.
        if overlap < row_duration - 0.001:
            selected.append((row, overlap))
        coverage_end = max(coverage_end, row_end)
    return selected


def _ffconcat_text(parts: list[PlaybackPart]) -> str:
    """Build a concat manifest with wall-clock overlap removed from its clock.

    `inpoint` alone skips duplicate packets but FFmpeg may still use the source's
    full duration when calculating the next file timestamp. Supplying the
    effective duration as well keeps the concatenated timeline gapless.
    """
    lines = ["ffconcat version 1.0"]
    for part in parts:
        started = _parse_utc(part.row.get("started_at"))
        ended = _parse_utc(part.row.get("ended_at"))
        effective_duration = max(
            0.001, (ended - started).total_seconds() - part.inpoint_seconds
        )
        lines.append(f"file '{camera_dvr_svc._ffconcat_path(part.path)}'")
        if part.inpoint_seconds > 0.001:
            lines.append(f"inpoint {part.inpoint_seconds:.3f}")
        lines.append(f"duration {effective_duration:.3f}")
    return "\n".join(lines) + "\n"


async def _continuous_parts(dvr, start: datetime) -> list[PlaybackPart]:
    # One throttled refresh is enough; the recorder's maintenance loop owns
    # authoritative full-index refreshes. Only immutable completed files are
    # eligible for this playback stream.
    await dvr._index_segments()
    rows = await dvr.list_segments(
        since=camera_dvr_svc._utc_iso(
            start - timedelta(seconds=dvr.segment_seconds)
        ),
        until=None,
        limit=MAX_STREAM_SEGMENTS,
        complete_only=True,
    )
    selected = _select_contiguous_rows(rows, start)
    parts: list[PlaybackPart] = []
    for row, inpoint in selected:
        path = dvr._segment_path(row.get("filename"), require_file=True)
        stat = path.stat()
        if (
            int(row.get("bytes") or -1) != stat.st_size
            or int(row.get("source_mtime_ns") or -1) != stat.st_mtime_ns
        ):
            raise FileNotFoundError(
                "Continuous recording changed; retry playback."
            )
        parts.append(
            PlaybackPart(row=row, path=path, inpoint_seconds=inpoint)
        )
    return parts


async def continuous_stream(
    dvr, start: datetime
) -> tuple[AsyncIterator[bytes], dict[str, str]]:
    """Create one browser stream spanning every contiguous completed segment."""
    parts = await _continuous_parts(dvr, start)
    if not parts:
        raise FileNotFoundError("No completed recording covers that time.")

    dvr.playback_dir.mkdir(parents=True, exist_ok=True)
    fd, manifest_name = tempfile.mkstemp(
        prefix=".continuous-", suffix=".ffconcat", dir=dvr.playback_dir
    )
    os.close(fd)
    manifest = Path(manifest_name)
    manifest.write_text(_ffconcat_text(parts), encoding="utf-8")

    ffmpeg = dvr.camera._require_ffmpeg()
    creationflags = 0x08000000 if os.name == "nt" else 0
    proc = await dvr._process_factory(
        str(ffmpeg),
        "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
        "-fflags", "+genpts",
        "-f", "concat", "-safe", "0", "-i", str(manifest),
        "-map", "0:v:0", "-an",
        # Normalize all archive codecs to one browser-safe stream. This is
        # deliberately review quality; evidentiary HEVC/H.264 MKVs stay
        # untouched on E:. Existing measurements show this preset comfortably
        # outruns the UI's maximum playback speed on Omega.
        "-vf", "scale=1280:-2",
        "-c:v", "libx264", "-preset", "superfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
    )
    if proc.stdout is None or proc.stderr is None:
        manifest.unlink(missing_ok=True)
        await dvr._terminate_process(proc)
        raise RuntimeError(
            "DVR playback worker did not expose its media pipes."
        )

    key = id(proc)
    dvr._playback_processes[key] = proc
    stderr_task = asyncio.create_task(dvr._drain_stderr(proc.stderr))

    async def iterator() -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await proc.stdout.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk
            await proc.wait()
        except asyncio.CancelledError:
            await dvr._terminate_process(proc)
            raise
        finally:
            if getattr(proc, "returncode", None) is None:
                await dvr._terminate_process(proc)
            dvr._playback_processes.pop(key, None)
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            manifest.unlink(missing_ok=True)

    first_start = _parse_utc(
        parts[0].row.get("started_at")
    ) + timedelta(seconds=parts[0].inpoint_seconds)
    last_end = _parse_utc(parts[-1].row.get("ended_at"))
    headers = {
        "Cache-Control": "private, no-store",
        "X-DVR-Playback-Start": camera_dvr_svc._utc_iso(first_start),
        "X-DVR-Playback-End": camera_dvr_svc._utc_iso(last_end),
        "X-DVR-Source-Segments": str(len(parts)),
    }
    return iterator(), headers


def create_router(require_session, dvr) -> APIRouter:
    router = APIRouter(prefix="/dvr", tags=["dvr-continuous-playback"])

    async def require_owner(
        session: dict = Depends(require_session),
    ) -> dict:
        if session.get("role") != "owner":
            raise HTTPException(403, "Owner authorization is required.")
        return session

    @router.get("/api/playback/continuous.mp4")
    async def playback_continuous(
        request: Request,
        start: str = Query(..., min_length=10, max_length=64),
        _session: dict = Depends(require_owner),
    ):
        parsed = camera_dvr_svc._parse_time(start)
        if parsed is None:
            raise HTTPException(400, "start must be a valid ISO timestamp.")
        try:
            iterator, headers = await continuous_stream(dvr, parsed)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                503, "Continuous DVR playback could not be started."
            ) from exc
        if await request.is_disconnected():
            await iterator.aclose()
            raise HTTPException(499, "Playback request was cancelled.")
        return StreamingResponse(
            iterator,
            media_type="video/mp4",
            headers=headers,
        )

    return router
