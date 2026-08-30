"""Continuous browser playback over the DVR's five-minute archive segments.

The recorder intentionally keeps small native MKV files for crash resilience and
retention. Human playback must not expose those file boundaries. This module
selects the contiguous completed run that covers an absolute timestamp, trims
normal recorder overlap at each boundary, and feeds FFmpeg one concat manifest.
FFmpeg emits one fragmented H.264 MP4 stream to the browser, so a single <video>
source keeps playing until a genuine recording break or the end of retained data.

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
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from . import camera_dvr as camera_dvr_svc


STREAM_CHUNK_BYTES = 256 * 1024
MAX_STREAM_SEGMENTS = camera_dvr_svc.MAX_UI_SEGMENTS
# Legacy filenames have no explicit session/ordinal relationship, so tolerate
# only a small amount of close-time/index jitter before treating them as a real
# recording break. New filenames are handled by their recorder sequence below.
LEGACY_INDEX_JITTER_SECONDS = 10.0
CROSS_SESSION_GAP_SECONDS = 2.0


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


def _segment_sequence(filename: object) -> Optional[tuple[str, int]]:
    """Return (recording-session, ordinal) for the recorder's new filenames."""
    match = camera_dvr_svc._SEGMENT_RE.fullmatch(str(filename or ""))
    if not match:
        return None
    return match.group(1), int(match.group(2))


def _is_expected_successor(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """Decide continuity from recorder identity before noisy close-time metadata.

    Completed-segment ``ended_at`` may be based on filesystem close time. That
    can be a couple seconds earlier/later than the next nominal 300-second
    boundary even though the recorder produced consecutive files. New recorder
    filenames encode the session and ordinal explicitly; consecutive ordinals in
    the same session are authoritative continuity and must never be split merely
    because mtime-derived ``ended_at`` drifted by a second or two.
    """
    prev_seq = _segment_sequence(previous.get("filename"))
    cur_seq = _segment_sequence(current.get("filename"))
    if prev_seq and cur_seq:
        prev_session, prev_ordinal = prev_seq
        cur_session, cur_ordinal = cur_seq
        if prev_session == cur_session:
            return cur_ordinal == prev_ordinal + 1
        # A recorder restart creates a new session prefix. Preserve a genuinely
        # continuous restart only when wall-clock coverage is effectively
        # adjacent; otherwise the UI should stop at the real outage.
        prev_end = _parse_utc(previous.get("ended_at"))
        cur_start = _parse_utc(current.get("started_at"))
        return (cur_start - prev_end).total_seconds() <= CROSS_SESSION_GAP_SECONDS

    # Legacy local-wall-clock names predate session ordinals. Their five-minute
    # starts are reliable enough to distinguish normal close-time jitter from a
    # true missing recording without trusting mtime to sub-second precision.
    prev_start = _parse_utc(previous.get("started_at"))
    cur_start = _parse_utc(current.get("started_at"))
    start_delta = (cur_start - prev_start).total_seconds()
    nominal = 300.0
    return 0.0 < start_delta <= nominal + LEGACY_INDEX_JITTER_SECONDS


def _select_contiguous_rows(
    rows: list[dict[str, Any]], start: datetime
) -> list[tuple[dict[str, Any], float]]:
    """Return rows from ``start`` through the first genuine recording break."""
    start = start.astimezone(timezone.utc)
    ordered = sorted(
        rows,
        key=lambda row: (
            _parse_utc(row.get("started_at")), int(row.get("id") or 0)
        ),
    )

    # Boundary overlaps are normal. If two files both cover the requested
    # instant, choose the later-started one so playback never begins in the last
    # second of an outgoing segment.
    covering = [
        (index, row)
        for index, row in enumerate(ordered)
        if _parse_utc(row.get("started_at")) <= start < _parse_utc(row.get("ended_at"))
    ]
    if not covering:
        return []
    first_index, first = max(
        covering,
        key=lambda item: (_parse_utc(item[1].get("started_at")), int(item[1].get("id") or 0)),
    )

    first_start = _parse_utc(first.get("started_at"))
    first_end = _parse_utc(first.get("ended_at"))
    selected: list[tuple[dict[str, Any], float]] = [
        (first, max(0.0, (start - first_start).total_seconds()))
    ]
    previous = first
    coverage_end = first_end

    for row in ordered[first_index + 1 :]:
        if not _is_expected_successor(previous, row):
            break
        row_start = _parse_utc(row.get("started_at"))
        row_end = _parse_utc(row.get("ended_at"))
        overlap = max(0.0, (coverage_end - row_start).total_seconds())
        row_duration = max(0.0, (row_end - row_start).total_seconds())
        if overlap < row_duration - 0.001:
            selected.append((row, overlap))
            previous = row
        coverage_end = max(coverage_end, row_end)
    return selected


def _ffconcat_text(parts: list[PlaybackPart]) -> str:
    """Build a concat manifest with duplicate boundary time removed.

    The source files themselves carry their real packet timing. ``inpoint``
    trims an overlap from the incoming file. Deliberately do not inject a
    synthetic ``duration`` from the SQLite ``ended_at`` value: that value may be
    filesystem-close time, not media duration, and was the source of false
    1-3 second discontinuities in historical playback.
    """
    lines = ["ffconcat version 1.0"]
    for part in parts:
        lines.append(f"file '{camera_dvr_svc._ffconcat_path(part.path)}'")
        if part.inpoint_seconds > 0.001:
            lines.append(f"inpoint {part.inpoint_seconds:.3f}")
    return "\n".join(lines) + "\n"


async def _continuous_parts(dvr, start: datetime) -> list[PlaybackPart]:
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
