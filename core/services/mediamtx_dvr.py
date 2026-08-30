"""MediaMTX-backed drop-in for the retired CameraDVR playback/analysis interface.

camera_security.py's DVR-facing functions (camera_event_history,
camera_footage_analyze, camera_motion_clip) were written against
CameraDVR's status()/list_segments()/range_clip()/event_clip()/
footage_analysis_samples() methods. This class satisfies that exact
duck-typed surface so those call sites did not need to change shape, while
the actual playback/analysis work happens against MediaMTX's Playback API
instead of X Omni's own segment index and FFmpeg-based clip stitcher.
"""

from __future__ import annotations

import logging
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import footage_frames
from .mediamtx_client import (
    MediaMTXClient,
    MediaMTXError,
    MediaMTXInvalidRequest,
    MediaMTXNotFound,
    MediaMTXUnavailable,
    PATH_MAIN,
)

log = logging.getLogger("xomni.mediamtx_dvr")

# MediaMTX's Playback API stitches server-side, so there is no per-segment
# count limit to enforce here; kept only so camera_security.py's existing
# reference to it keeps working unchanged.
MAX_PLAYBACK_SEGMENTS = 8
MAX_TOOL_PLAYBACK_DURATION_SECONDS = 300
MAX_FOOTAGE_ANALYSIS_DURATION_SECONDS = 180
MAX_PLAYBACK_DURATION_SECONDS = 30 * 60


class PlaybackPreparationError(RuntimeError):
    """A clip or analysis frame set could not be prepared promptly."""


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_clip_name(cache_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", cache_name).strip("-.")[:130] or "clip"


_SAFE_SAVED_CLIP_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,140}\.mp4$")


class MediaMTXDVR:
    """Drop-in DVR-facing adapter, backed by MediaMTX instead of a local recorder."""

    def __init__(
        self,
        client: MediaMTXClient,
        *,
        path: str = PATH_MAIN,
        ffmpeg_path: Path,
        recordings_root: Path,
        clips_dir: Path,
        saved_clips_dir: Optional[Path] = None,
    ):
        self.client = client
        self.path = path
        self.ffmpeg_path = Path(ffmpeg_path)
        self.recordings_root = Path(recordings_root)
        # Playback/analysis-driven fetches land here -- an ephemeral cache
        # keyed by time range, reused across repeat scrubs of the same span.
        # A human's deliberate "save this clip" instead calls export_clip(),
        # which writes into saved_clips_dir so it is never mistaken for
        # scratch cache content and never competes with it for cleanup.
        self.clips_dir = Path(clips_dir)
        self.saved_clips_dir = Path(saved_clips_dir) if saved_clips_dir is not None else self.clips_dir / "saved"

    async def status(self) -> dict[str, Any]:
        try:
            info = await self.client.path_status(self.path)
        except MediaMTXError as exc:
            return {"ok": False, "recording": False, "last_error": str(exc)}
        ready = bool(info and info.get("ready"))
        return {
            "ok": ready,
            "recording": ready,
            "root": str(self.recordings_root),
            "last_error": None if ready else "Camera source is not currently connected.",
            "drive": self._drive_usage(),
        }

    def _drive_usage(self) -> dict[str, Any]:
        try:
            usage = shutil.disk_usage(self.recordings_root)
            return {
                "path": str(self.recordings_root),
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
            }
        except OSError:
            return {
                "path": str(self.recordings_root),
                "total_bytes": None,
                "used_bytes": None,
                "free_bytes": None,
            }

    async def list_segments(
        self,
        *,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 40,
        complete_only: bool = False,
    ) -> list[dict[str, Any]]:
        since_dt = _parse_iso(since) if since else _utc_now() - timedelta(days=30)
        until_dt = _parse_iso(until) if until else _utc_now()
        if since_dt is None or until_dt is None or until_dt <= since_dt:
            return []
        try:
            spans = await self.client.list_recordings(self.path, since_dt, until_dt)
        except MediaMTXError as exc:
            log.info("MediaMTX recording list unavailable: %s", exc)
            return []
        rows = [
            {
                "started_at": _iso(span.started_at),
                "ended_at": _iso(span.ended_at),
                "duration_seconds": span.duration_seconds,
                # MediaMTX's /list only reports finalized recording segments.
                "complete": True,
            }
            for span in spans
        ]
        return rows[-limit:] if limit else rows

    async def _fetch_into(self, directory: Path, since: datetime, until: datetime, *, name: str) -> Path:
        since = _utc(since)
        until = _utc(until)
        if until <= since:
            raise ValueError("Clip end must be after clip start.")
        duration = (until - since).total_seconds()
        if duration > MAX_PLAYBACK_DURATION_SECONDS:
            raise ValueError("Continuous playback clips are limited to 30 minutes.")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{_safe_clip_name(name)}.mp4"
        if target.is_file() and target.stat().st_size > 0:
            return target
        try:
            clip_bytes = await self.client.fetch_clip_bytes(self.path, since, duration)
        except MediaMTXNotFound as exc:
            raise FileNotFoundError("No continuous recording covers that time range.") from exc
        except MediaMTXUnavailable as exc:
            raise PlaybackPreparationError(str(exc)) from exc
        except MediaMTXInvalidRequest as exc:
            raise ValueError(str(exc)) from exc
        temp_target = directory / f".{target.stem}-{uuid.uuid4().hex[:8]}.tmp"
        temp_target.write_bytes(clip_bytes)
        temp_target.replace(target)
        return target

    async def range_clip(self, since: datetime, until: datetime, *, cache_name: str) -> Path:
        """Ephemeral, scrub-cache-backed clip for playback/analysis -- not a saved export."""
        return await self._fetch_into(self.clips_dir, since, until, name=cache_name)

    async def export_clip(self, since: datetime, until: datetime, *, name: str) -> Path:
        """A human's deliberate "save this clip" -- lands in saved_clips_dir, kept indefinitely."""
        return await self._fetch_into(self.saved_clips_dir, since, until, name=name)

    def list_saved_clips(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not self.saved_clips_dir.is_dir():
            return items
        for entry in self.saved_clips_dir.glob("*.mp4"):
            try:
                stat = entry.stat()
            except OSError:
                continue
            items.append({
                "filename": entry.name,
                "bytes": stat.st_size,
                "created_at": _iso(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)),
            })
        items.sort(key=lambda row: row["created_at"], reverse=True)
        return items

    def saved_clip_path(self, filename: str) -> Optional[Path]:
        if not _SAFE_SAVED_CLIP_NAME_RE.fullmatch(filename):
            return None
        candidate = self.saved_clips_dir / filename
        try:
            if candidate.resolve().parent != self.saved_clips_dir.resolve():
                return None
        except OSError:
            return None
        return candidate if candidate.is_file() else None

    def delete_saved_clip(self, filename: str) -> bool:
        path = self.saved_clip_path(filename)
        if path is None:
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    async def event_clip(self, store, burst_id: int) -> Path:
        events = list(store.list_camera_events_by_burst(int(burst_id)))
        if not events:
            raise FileNotFoundError("No recorded frames for that motion event.")
        captured = sorted(
            value
            for value in (_parse_iso(row.get("captured_at")) for row in events)
            if value is not None
        )
        if not captured:
            raise FileNotFoundError("Motion event timestamps are invalid.")
        since = captured[0] - timedelta(seconds=30)
        until = captured[-1] + timedelta(seconds=75)
        return await self.range_clip(since, until, cache_name=f"event-{int(burst_id)}")

    async def footage_analysis_samples(
        self,
        since: datetime,
        until: datetime,
        *,
        sample_count: Optional[int] = None,
    ) -> dict[str, Any]:
        since = _utc(since)
        until = _utc(until)
        if until <= since:
            raise ValueError("Analysis end time must be after start time.")
        duration = (until - since).total_seconds()
        if duration > MAX_FOOTAGE_ANALYSIS_DURATION_SECONDS:
            raise ValueError("The selected window is too long for bounded DVR analysis.")
        try:
            clip_bytes = await self.client.fetch_clip_bytes(self.path, since, duration)
        except MediaMTXNotFound as exc:
            raise FileNotFoundError(str(exc)) from exc
        except MediaMTXUnavailable as exc:
            raise PlaybackPreparationError(str(exc)) from exc
        except MediaMTXInvalidRequest as exc:
            raise ValueError(str(exc)) from exc
        try:
            built = await footage_frames.build_contact_sheet(
                clip_bytes, since, since, until,
                ffmpeg_path=self.ffmpeg_path, sample_count=sample_count,
            )
        except footage_frames.FrameExtractionError as exc:
            raise PlaybackPreparationError(str(exc)) from exc
        return {
            "analyzed_started_at": _iso(since),
            "analyzed_ended_at": _iso(until),
            "sample_count": built["sample_count"],
            "sampled_at": [_iso(value) for value in built["sampled_at"]],
            "contact_sheet": built["contact_sheet"],
            "source_segments": [self.path],
        }
