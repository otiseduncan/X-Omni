"""Continuous exterior-camera DVR and standalone recording browser.

The DVR deliberately stays cheap: the camera's already-compressed H.264/H.265
RTSP video is copied into five-minute Matroska segments without decoding or
re-encoding.  E:\\XOmni-DVR is treated as a dedicated rolling-recording root;
only files created beneath that root are ever pruned.

ONVIF PullPoint motion events are consumed independently of recording.  The
CameraMonitor uses those events as its primary trigger for screenshots and
person/vehicle analysis, while the continuous recorder preserves everything.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image, ImageDraw, ImageOps
from pydantic import BaseModel
from starlette.requests import ClientDisconnect

from . import exterior_camera as exterior_camera_svc
from . import camera_monitoring as camera_monitoring_svc

log = logging.getLogger("xomni.camera_dvr")

DEFAULT_DVR_ROOT = Path("E:/XOmni-DVR")
SEGMENT_SECONDS = 300
OPERATING_RESERVE_BYTES = 2 * 1024 * 1024 * 1024
MAINTENANCE_SECONDS = 30
RESTART_DELAY_SECONDS = 5
MAX_RESTART_DELAY_SECONDS = 60
RECORDER_STALL_SECONDS = 120
INDEX_REFRESH_SECONDS = 5
SUBSCRIPTION_RECREATE_SECONDS = 4 * 60
ONVIF_MIN_PULL_CYCLE_SECONDS = 1.0
# Playback must never keep a chat request pending for minutes.  This applies
# only to on-demand browser artifacts; the archive recorder remains an
# independent stream-copy process.
PLAYBACK_TIMEOUT_SECONDS = 45
CONCAT_TIMEOUT_SECONDS = 20
SEGMENT_PROBE_TIMEOUT_SECONDS = 10
SEGMENT_PROBE_RETRY_SECONDS = 60
MAX_SEGMENT_PROBES_PER_INDEX = 4
MAX_PLAYBACK_GAP_SECONDS = 1.0
MAX_PLAYBACK_DURATION_SECONDS = 30 * 60
MAX_PLAYBACK_SEGMENTS = 8
# Five-minute clips are the recorder's native segment duration and finish
# quickly on this host.  Longer chat requests are narrowed to a motion event
# before FFmpeg is invoked so a broad history question cannot block a reply.
MAX_TOOL_PLAYBACK_DURATION_SECONDS = 300
MAX_FOOTAGE_ANALYSIS_DURATION_SECONDS = 180
MIN_FOOTAGE_ANALYSIS_SAMPLES = 8
MAX_FOOTAGE_ANALYSIS_SAMPLES = 20
FOOTAGE_ANALYSIS_FRAME_TIMEOUT_SECONDS = 12
FOOTAGE_ANALYSIS_FRAME_WIDTH = 640
PROCESS_STOP_TIMEOUT_SECONDS = 5.0
PLAYBACK_CACHE_MAX_AGE = timedelta(hours=24)
MAX_TOOL_SEGMENTS = 40
MAX_UI_SEGMENTS = 500
MAX_UI_EVENTS = 500

_EVENTS_NS = "http://www.onvif.org/ver10/events/wsdl"
_WSN_NS = "http://docs.oasis-open.org/wsn/b-2"
_EVENT_ACTIONS = {
    "CreatePullPointSubscription": (
        "http://www.onvif.org/ver10/events/wsdl/"
        "EventPortType/CreatePullPointSubscriptionRequest"
    ),
    "PullMessages": (
        "http://www.onvif.org/ver10/events/wsdl/"
        "PullPointSubscription/PullMessagesRequest"
    ),
    "Renew": (
        "http://docs.oasis-open.org/wsn/bw-2/"
        "SubscriptionManager/RenewRequest"
    ),
    "Unsubscribe": (
        "http://docs.oasis-open.org/wsn/bw-2/"
        "SubscriptionManager/UnsubscribeRequest"
    ),
}
_BOOL_TRUE = {"true", "1", "on", "active", "yes"}
_BOOL_FALSE = {"false", "0", "off", "inactive", "no"}
_SEGMENT_RE = re.compile(r"^(\d{8}T\d{12}Z)-(\d{6})\.mkv$")
_LEGACY_SEGMENT_RE = re.compile(r"^(\d{8})-(\d{6})\.mkv$")
_SAFE_CACHED_MP4_RE = re.compile(
    r"^(?:motion-[1-9]\d*|range-\d+-\d+)\.mp4$"
)
_SAFE_CLIP_FILENAME_RE = re.compile(r"^clip-\d+-\d+-[0-9a-f]{12}\.mp4$")
_XIONGMAI_PULLPOINT_PATH_RE = re.compile(
    r"^/(?:event_service|events_service)(?:/[A-Za-z0-9._~-]{1,128}){1,4}/?$",
    re.IGNORECASE,
)
_SQLITE_MAX_ID = 2**63 - 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _local_day_bounds(day_text: str) -> tuple[datetime, datetime]:
    try:
        selected = date.fromisoformat(day_text)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    # A naive datetime.timestamp() deliberately asks the host C runtime to
    # apply the historical local-time rules for that date.  Capturing
    # datetime.now().astimezone().tzinfo would freeze today's UTC offset and
    # mis-index winter/summer dates across DST.
    start_local = datetime.combine(selected, dt_time.min)
    end_local = datetime.combine(selected + timedelta(days=1), dt_time.min)
    return (
        datetime.fromtimestamp(start_local.timestamp(), tz=timezone.utc),
        datetime.fromtimestamp(end_local.timestamp(), tz=timezone.utc),
    )


def _ffconcat_path(path: Path) -> str:
    text = str(path.resolve()).replace("\\", "/")
    return text.replace("'", "'\\''")


@dataclass(frozen=True)
class RecordingProfile:
    token: str
    name: str
    encoding: str
    width: int
    height: int

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "codec": self.encoding,
            "width": self.width,
            "height": self.height,
        }


class PlaybackPreparationError(RuntimeError):
    """A bounded DVR playback worker could not produce a trustworthy artifact."""


class CameraDVR:
    """Own one continuous stream-copy recorder and its bounded metadata index."""

    def __init__(
        self,
        exterior_camera,
        *,
        root: Path | str = DEFAULT_DVR_ROOT,
        segment_seconds: int = SEGMENT_SECONDS,
        reserve_bytes: int = OPERATING_RESERVE_BYTES,
        process_factory: Optional[Callable[..., Any]] = None,
        required_drive: Optional[str] = "E:",
    ):
        self.camera = exterior_camera
        self.root = Path(root).resolve()
        self.recordings_dir = self.root / "recordings"
        self.playback_dir = self.root / "playback-cache"
        self.clips_dir = self.root / "clips"
        self.db_path = self.root / "dvr.sqlite"
        self.segment_seconds = max(60, int(segment_seconds))
        self.reserve_bytes = max(256 * 1024 * 1024, int(reserve_bytes))
        self.required_drive = (
            str(required_drive).rstrip("\\/").upper()
            if required_drive is not None
            else None
        )
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._process = None
        self._stderr_task: Optional[asyncio.Task[bytes]] = None
        self._wait_task: Optional[asyncio.Task[Any]] = None
        self._stopped = False
        self._recording_started_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._profile: Optional[RecordingProfile] = None
        self._actual_profile: Optional[RecordingProfile] = None
        self._current_session_prefix: Optional[str] = None
        self._events_healthy = False
        self._subscription_renew_at = 0.0
        self._last_motion_at: Optional[str] = None
        self._db_lock = asyncio.Lock()
        self._index_lock = asyncio.Lock()
        self._io_lock = asyncio.Lock()
        self._last_index_at = 0.0
        self._progress_name: Optional[str] = None
        self._progress_size = -1
        self._last_progress_at = time.monotonic()
        self._playback_processes: dict[int, Any] = {}
        self._probe_retry_at: dict[tuple[str, int, int], float] = {}
        self._storage_initialized = False

    @property
    def events_healthy(self) -> bool:
        return bool(self._events_healthy)

    def mark_events_unhealthy(self) -> None:
        self._events_healthy = False

    def _volume_anchor(self) -> Path:
        if os.name == "nt":
            drive = self.root.drive
            if not drive:
                raise RuntimeError("DVR root must use an absolute Windows drive path.")
            return Path(f"{drive}/")
        return self.root

    def _ensure_storage_sync(self) -> None:
        anchor = self._volume_anchor()
        if (
            os.name == "nt"
            and self.required_drive is not None
            and self.root.drive.upper() != self.required_drive
        ):
            raise RuntimeError(f"DVR root must stay on {self.required_drive}.")
        if not anchor.exists():
            raise RuntimeError("DVR drive E: is not available.")
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.playback_dir.mkdir(parents=True, exist_ok=True)
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        if self._storage_initialized and self.db_path.is_file():
            return
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL UNIQUE,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    bytes INTEGER NOT NULL DEFAULT 0,
                    codec TEXT,
                    width INTEGER,
                    height INTEGER,
                    complete INTEGER NOT NULL DEFAULT 0 CHECK (complete IN (0,1)),
                    probed INTEGER NOT NULL DEFAULT 0 CHECK (probed IN (0,1)),
                    source_mtime_ns INTEGER NOT NULL DEFAULT 0,
                    indexed_at TEXT NOT NULL
                )
                """
            )
            segment_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(segments)")
            }
            if "probed" not in segment_columns:
                conn.execute(
                    "ALTER TABLE segments ADD COLUMN probed INTEGER NOT NULL "
                    "DEFAULT 0 CHECK (probed IN (0,1))"
                )
            if "source_mtime_ns" not in segment_columns:
                conn.execute(
                    "ALTER TABLE segments ADD COLUMN source_mtime_ns INTEGER NOT NULL DEFAULT 0"
                )
            # Rows from builds that trusted advertised ONVIF metadata are not
            # bitstream-verified.  Keep them explicitly unknown until ffprobe
            # verifies the local MKV; never silently retain a wrong codec.
            conn.execute(
                "UPDATE segments SET codec=NULL, width=NULL, height=NULL WHERE probed=0"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_segments_started ON segments(started_at)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS playback_artifacts (
                    filename TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            # Saved/exported clips live in self.clips_dir, never self.playback_dir,
            # and this table is never touched by _prune_locked -- a clip an
            # operator explicitly saved must survive circular retention until
            # they explicitly delete it.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS clips (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL UNIQUE,
                    title TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    bytes INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
            self._storage_initialized = True
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _segment_path(self, filename: object, *, require_file: bool = False) -> Path:
        name = str(filename or "")
        if not (_SEGMENT_RE.fullmatch(name) or _LEGACY_SEGMENT_RE.fullmatch(name)):
            raise FileNotFoundError("Recording segment filename is invalid.")
        root = self.recordings_dir.resolve()
        path = (self.recordings_dir / name).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise FileNotFoundError("Recording segment is outside the DVR archive.") from exc
        if require_file and not path.is_file():
            raise FileNotFoundError("Recording segment file is missing.")
        return path

    def _segment_start_from_name(self, filename: str) -> Optional[datetime]:
        match = _SEGMENT_RE.fullmatch(filename)
        if match:
            session = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%fZ").replace(
                tzinfo=timezone.utc
            )
            return session + timedelta(seconds=int(match.group(2)) * self.segment_seconds)
        legacy = _LEGACY_SEGMENT_RE.fullmatch(filename)
        if not legacy:
            return None
        # Legacy branch builds used local wall-clock filenames.  Resolve them
        # with the host's historical timezone rules rather than today's fixed
        # UTC offset.  New recordings use collision-safe UTC session names.
        naive_local = datetime.strptime("".join(legacy.groups()), "%Y%m%d%H%M%S")
        return datetime.fromtimestamp(naive_local.timestamp(), tz=timezone.utc)

    def _completed_end(self, path: Path, started: datetime) -> datetime:
        nominal = started + timedelta(seconds=self.segment_seconds)
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return nominal
        # FFmpeg closes each segment when its final packet is written, so mtime
        # is a better closure bound than the next filename.  Cap implausible
        # copied/restored mtimes instead of inventing footage across outages.
        if started < modified <= started + timedelta(seconds=self.segment_seconds * 2):
            return modified
        return nominal

    def _ffprobe_path(self) -> Optional[Path]:
        try:
            ffmpeg = Path(self.camera._require_ffmpeg())
        except (AttributeError, OSError, RuntimeError):
            return None
        sibling = ffmpeg.with_name(
            "ffprobe.exe" if ffmpeg.suffix.casefold() == ".exe" else "ffprobe"
        )
        if sibling.is_file():
            return sibling
        discovered = shutil.which("ffprobe")
        return Path(discovered).resolve() if discovered else None

    def _probe_segment_sync(self, path: Path) -> Optional[tuple[str, int, int]]:
        """Return the bitstream's real video metadata without decoding it."""
        ffprobe = self._ffprobe_path()
        if ffprobe is None:
            return None
        creationflags = 0x08000000 if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                [
                    str(ffprobe),
                    "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=codec_name,width,height",
                    "-of", "json",
                    str(path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=SEGMENT_PROBE_TIMEOUT_SECONDS,
                check=False,
                creationflags=creationflags,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        try:
            streams = json.loads(completed.stdout.decode("utf-8", "strict")).get(
                "streams", []
            )
            stream = streams[0]
            raw_codec = str(stream["codec_name"]).strip().casefold()
            width = int(stream["width"])
            height = int(stream["height"])
        except (IndexError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None
        if not raw_codec or width <= 0 or height <= 0:
            return None
        codec = {
            "h264": "H264",
            "avc": "H264",
            "hevc": "HEVC",
            "h265": "HEVC",
        }.get(raw_codec, re.sub(r"[^a-z0-9_.-]", "", raw_codec).upper())
        return (codec, width, height) if codec else None

    async def _index_segments(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_index_at < INDEX_REFRESH_SECONDS:
            return
        async with self._index_lock:
            now = time.monotonic()
            if not force and now - self._last_index_at < INDEX_REFRESH_SECONDS:
                return
            try:
                await self._index_segments_locked()
            except asyncio.CancelledError:
                raise
            except (OSError, sqlite3.Error):
                self._last_error = "DVR index is unavailable; recording will retry."
            self._last_index_at = time.monotonic()

    async def _index_segments_locked(self) -> None:
        try:
            await asyncio.to_thread(self._ensure_storage_sync)
        except RuntimeError as exc:
            self._last_error = str(exc)
            return
        except (OSError, sqlite3.Error):
            self._last_error = "DVR storage is unavailable; recording will retry."
            return
        files = list(self.recordings_dir.glob("*.mkv"))
        parsed: list[tuple[Path, datetime]] = []
        for path in files:
            started = self._segment_start_from_name(path.name)
            if started is None:
                continue
            try:
                safe_path = self._segment_path(path.name, require_file=True)
            except FileNotFoundError:
                log.warning("Ignoring an unsafe DVR segment entry.")
                continue
            parsed.append((safe_path, started))
        parsed.sort(key=lambda item: (item[1], item[0].name))
        active = self._process is not None and getattr(self._process, "returncode", None) is None
        active_filename: Optional[str] = None
        if active and self._current_session_prefix:
            current_session_files = [
                path
                for path, _started in parsed
                if path.name.startswith(f"{self._current_session_prefix}-")
            ]
            if current_session_files:
                active_filename = current_session_files[-1].name
        stamp = _utc_iso(_utc_now())
        present = {path.name for path, _ in parsed}
        file_state: dict[str, tuple[int, int, bool]] = {}
        for index, (path, _started) in enumerate(parsed):
            try:
                stat = path.stat()
            except OSError:
                continue
            complete = path.name != active_filename
            file_state[path.name] = (stat.st_size, stat.st_mtime_ns, complete)

        # Snapshot the index under its own short lock, then run the bounded
        # local probes without holding either the DB or playback/retention lock.
        async with self._db_lock:
            conn = self._conn()
            try:
                existing = {
                    row["filename"]: dict(row)
                    for row in conn.execute("SELECT * FROM segments").fetchall()
                }
            finally:
                conn.close()

        now = time.monotonic()
        candidates: list[tuple[Path, tuple[int, int, bool]]] = []
        for path, _started in reversed(parsed):
            state = file_state.get(path.name)
            if state is None or not state[2] or state[0] <= 0:
                continue
            size, mtime_ns, _complete = state
            prior = existing.get(path.name)
            prior_matches = (
                prior is not None
                and int(prior.get("bytes") or -1) == size
                and int(prior.get("source_mtime_ns") or -1) == mtime_ns
            )
            if prior_matches and bool(prior.get("probed")):
                continue
            retry_key = (path.name, size, mtime_ns)
            if now < self._probe_retry_at.get(retry_key, 0.0):
                continue
            candidates.append((path, state))
            if len(candidates) >= MAX_SEGMENT_PROBES_PER_INDEX:
                break

        probe_results: dict[str, tuple[tuple[int, int, bool], tuple[str, int, int]]] = {}
        if candidates:
            results = await asyncio.gather(
                *(
                    asyncio.to_thread(self._probe_segment_sync, path)
                    for path, _state in candidates
                ),
                return_exceptions=True,
            )
            retry_base = time.monotonic()
            for (path, state), result in zip(candidates, results):
                metadata = result if isinstance(result, tuple) else None
                retry_key = (path.name, state[0], state[1])
                if metadata is None:
                    self._probe_retry_at[retry_key] = (
                        retry_base + SEGMENT_PROBE_RETRY_SECONDS
                    )
                else:
                    probe_results[path.name] = (state, metadata)
                    self._probe_retry_at.pop(retry_key, None)
        self._probe_retry_at = {
            retry_key: retry_at
            for retry_key, retry_at in self._probe_retry_at.items()
            if retry_key[0] in present
        }

        async with self._db_lock:
            conn = self._conn()
            try:
                for index, (path, started) in enumerate(parsed):
                    complete = path.name != active_filename
                    prior = existing.get(path.name)
                    captured = file_state.get(path.name)
                    try:
                        current_stat = path.stat()
                    except OSError:
                        continue
                    size = current_stat.st_size
                    mtime_ns = current_stat.st_mtime_ns
                    capture_unchanged = (
                        captured is not None
                        and captured[0] == size
                        and captured[1] == mtime_ns
                        and captured[2] == complete
                    )
                    actual_metadata: Optional[tuple[str, int, int]] = None
                    prior_verified = (
                        prior is not None
                        and bool(prior.get("probed"))
                        and int(prior.get("bytes") or -1) == size
                        and int(prior.get("source_mtime_ns") or -1) == mtime_ns
                    )
                    if prior_verified:
                        try:
                            actual_metadata = (
                                str(prior["codec"]),
                                int(prior["width"]),
                                int(prior["height"]),
                            )
                        except (TypeError, ValueError):
                            prior_verified = False
                            actual_metadata = None
                    fresh_probe = probe_results.get(path.name)
                    fresh_verified = bool(
                        fresh_probe is not None
                        and capture_unchanged
                        and fresh_probe[0] == captured
                    )
                    if fresh_verified and fresh_probe is not None:
                        actual_metadata = fresh_probe[1]
                    probed = int(prior_verified or fresh_verified)
                    if (
                        prior is not None
                        and bool(prior["complete"])
                        and complete
                        and fresh_probe is None
                        and int(prior.get("bytes") or -1) == size
                        and int(prior.get("source_mtime_ns") or -1) == mtime_ns
                    ):
                        continue
                    if actual_metadata is not None:
                        row_codec, row_width, row_height = actual_metadata
                    else:
                        row_codec = row_width = row_height = None
                    ended = (
                        self._completed_end(path, started)
                        if complete
                        else started + timedelta(seconds=self.segment_seconds)
                    )
                    conn.execute(
                        """
                        INSERT INTO segments
                            (filename, started_at, ended_at, bytes, codec, width, height,
                             complete, probed, source_mtime_ns, indexed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(filename) DO UPDATE SET
                            started_at=excluded.started_at,
                            ended_at=excluded.ended_at,
                            bytes=excluded.bytes,
                            codec=excluded.codec,
                            width=excluded.width,
                            height=excluded.height,
                            complete=excluded.complete,
                            probed=excluded.probed,
                            source_mtime_ns=excluded.source_mtime_ns,
                            indexed_at=excluded.indexed_at
                        """,
                        (
                            path.name,
                            _utc_iso(started),
                            _utc_iso(ended),
                            size,
                            row_codec,
                            row_width,
                            row_height,
                            int(complete),
                            int(probed),
                            mtime_ns,
                            stamp,
                        ),
                    )
                    if (
                        fresh_verified
                        and actual_metadata is not None
                        and self._current_session_prefix
                        and path.name.startswith(f"{self._current_session_prefix}-")
                        and self._profile is not None
                    ):
                        self._actual_profile = RecordingProfile(
                            token=self._profile.token,
                            name=self._profile.name,
                            encoding=actual_metadata[0],
                            width=actual_metadata[1],
                            height=actual_metadata[2],
                        )
                    if active and not complete:
                        if self._progress_name != path.name or self._progress_size != size:
                            self._progress_name = path.name
                            self._progress_size = size
                            self._last_progress_at = time.monotonic()
                rows = conn.execute("SELECT id, filename FROM segments").fetchall()
                for row in rows:
                    if row["filename"] not in present:
                        conn.execute("DELETE FROM segments WHERE id = ?", (row["id"],))
                conn.commit()
            finally:
                conn.close()

    def _recorder_stalled(self) -> bool:
        if self._process is None or getattr(self._process, "returncode", None) is not None:
            return False
        return time.monotonic() - self._last_progress_at >= RECORDER_STALL_SECONDS

    def _disk_usage_sync(self):
        return shutil.disk_usage(self._volume_anchor())

    async def _prune(self) -> None:
        async with self._io_lock:
            try:
                await self._prune_locked()
            except asyncio.CancelledError:
                raise
            except (OSError, sqlite3.Error):
                self._last_error = "DVR retention maintenance could not access its index."

    async def _prune_locked(self) -> None:
        try:
            usage = await asyncio.to_thread(self._disk_usage_sync)
        except (OSError, RuntimeError):
            return
        if usage.free >= self.reserve_bytes:
            await self._prune_old_playback_cache()
            return
        await self._clear_playback_cache()
        if not self.db_path.is_file():
            return
        while True:
            try:
                usage = await asyncio.to_thread(self._disk_usage_sync)
            except (OSError, RuntimeError):
                return
            if usage.free >= self.reserve_bytes:
                return
            async with self._db_lock:
                conn = self._conn()
                try:
                    row = conn.execute(
                        "SELECT * FROM segments WHERE complete = 1 ORDER BY started_at ASC LIMIT 1"
                    ).fetchone()
                    if row is None:
                        return
                    try:
                        path = self._segment_path(row["filename"])
                    except FileNotFoundError:
                        conn.execute("DELETE FROM segments WHERE id = ?", (row["id"],))
                        conn.commit()
                        continue
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        return
                    conn.execute("DELETE FROM segments WHERE id = ?", (row["id"],))
                    conn.commit()
                finally:
                    conn.close()

    async def _clear_playback_cache(self) -> None:
        if not self.playback_dir.exists():
            return
        removed: list[str] = []
        for path in self.playback_dir.glob("*.mp4"):
            try:
                path.unlink(missing_ok=True)
                removed.append(path.name)
            except OSError:
                pass
        if removed:
            async with self._db_lock:
                await asyncio.to_thread(self._forget_artifacts_sync, removed)

    async def _prune_old_playback_cache(self) -> None:
        if not self.playback_dir.exists():
            return
        cutoff = _utc_now() - PLAYBACK_CACHE_MAX_AGE
        removed: list[str] = []
        for path in self.playback_dir.glob("*.mp4"):
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                if modified < cutoff:
                    path.unlink(missing_ok=True)
                    removed.append(path.name)
            except OSError:
                pass
        if removed:
            async with self._db_lock:
                await asyncio.to_thread(self._forget_artifacts_sync, removed)

    def _list_segments_sync(
        self,
        *,
        since: Optional[str],
        until: Optional[str],
        limit: int,
        complete_only: bool,
    ) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        clauses = []
        params: list[Any] = []
        if since:
            clauses.append("ended_at > ?")
            params.append(since)
        if until:
            clauses.append("started_at < ?")
            params.append(until)
        if complete_only:
            clauses.append("complete = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), MAX_UI_SEGMENTS)))
        conn = self._conn()
        try:
            rows = conn.execute(
                f"SELECT * FROM segments {where} ORDER BY started_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    async def list_segments(
        self,
        *,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = MAX_TOOL_SEGMENTS,
        complete_only: bool = False,
    ) -> list[dict[str, Any]]:
        await self._index_segments()
        try:
            return await asyncio.to_thread(
                self._list_segments_sync,
                since=since,
                until=until,
                limit=limit,
                complete_only=complete_only,
            )
        except (OSError, sqlite3.Error):
            self._last_error = "DVR index is unavailable; recording will retry."
            return []

    def _get_segment_sync(self, segment_id: int) -> Optional[dict[str, Any]]:
        if not self.db_path.exists():
            return None
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def get_segment(self, segment_id: int) -> Optional[dict[str, Any]]:
        return await asyncio.to_thread(self._get_segment_sync, int(segment_id))

    def _register_artifact_sync(self, filename: str, kind: str, source_key: str) -> None:
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO playback_artifacts (filename, kind, source_key, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(filename) DO UPDATE SET
                    kind=excluded.kind,
                    source_key=excluded.source_key,
                    created_at=excluded.created_at
                """,
                (filename, kind, source_key, _utc_iso(_utc_now())),
            )
            conn.commit()
        finally:
            conn.close()

    def cache_artifact_is_tracked(
        self, filename: str, *, source_key: Optional[str] = None
    ) -> bool:
        if not self.db_path.is_file():
            return False
        conn = self._conn()
        try:
            if source_key is None:
                row = conn.execute(
                    "SELECT 1 FROM playback_artifacts WHERE filename = ? LIMIT 1",
                    (filename,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT 1 FROM playback_artifacts
                    WHERE filename = ? AND source_key = ? LIMIT 1
                    """,
                    (filename, source_key),
                ).fetchone()
            return row is not None
        finally:
            conn.close()

    @staticmethod
    def _validate_range_coverage(
        segments: list[dict[str, Any]], since: datetime, until: datetime
    ) -> str:
        parsed: list[tuple[datetime, datetime, dict[str, Any]]] = []
        for row in segments:
            started = _parse_time(row.get("started_at"))
            ended = _parse_time(row.get("ended_at"))
            if started is None or ended is None or ended <= started:
                raise FileNotFoundError("Continuous recording coverage is incomplete.")
            parsed.append((started, ended, row))
        parsed.sort(key=lambda item: (item[0], str(item[2].get("filename") or "")))
        if not parsed or parsed[0][0] > since:
            raise FileNotFoundError("Continuous recording does not cover the requested start time.")
        coverage_end = parsed[0][1]
        for started, ended, _row in parsed[1:]:
            if started > coverage_end + timedelta(seconds=MAX_PLAYBACK_GAP_SECONDS):
                raise FileNotFoundError("Continuous recording has a gap in the requested range.")
            coverage_end = max(coverage_end, ended)
        if coverage_end < until:
            raise FileNotFoundError("Continuous recording does not yet cover the requested end time.")
        fingerprint = "\n".join(
            [
                _utc_iso(since),
                _utc_iso(until),
                *(
                    "|".join(
                        str(row.get(field) or "")
                        for field in (
                            "id", "filename", "started_at", "ended_at", "bytes",
                            "codec", "width", "height", "probed", "source_mtime_ns"
                        )
                    )
                    for _started, _ended, row in parsed
                ),
            ]
        )
        return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()

    def _forget_artifacts_sync(self, filenames: list[str]) -> None:
        if not filenames or not self.db_path.is_file():
            return
        conn = self._conn()
        try:
            conn.executemany(
                "DELETE FROM playback_artifacts WHERE filename = ?",
                ((name,) for name in filenames),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _select_recording_profile(profiles) -> RecordingProfile:
        h264 = [p for p in profiles if p.encoding.replace(".", "").upper() in {"H264", "AVC"}]
        h265 = [p for p in profiles if p.encoding.replace(".", "").upper() in {"H265", "HEVC"}]
        if not h264 and not h265:
            raise exterior_camera_svc.ExteriorCameraUnavailable(
                "Exterior camera did not advertise an H.264 or H.265 recording profile."
            )
        best_h264 = max(h264, key=lambda p: (p.width * p.height, -p.ordinal), default=None)
        best_h265 = max(h265, key=lambda p: (p.width * p.height, -p.ordinal), default=None)
        if best_h264 is None:
            selected = best_h265
        elif best_h265 is None:
            selected = best_h264
        else:
            h264_area = best_h264.width * best_h264.height
            h265_area = best_h265.width * best_h265.height
            h264_is_main = "main" in str(best_h264.name or "").casefold()
            h264_is_useful = h264_area >= 1920 * 1080 or h264_area * 2 >= h265_area
            selected = best_h264 if h264_is_main or h264_is_useful else best_h265
        assert selected is not None
        return RecordingProfile(
            token=selected.token,
            name=selected.name,
            encoding=selected.encoding,
            width=selected.width,
            height=selected.height,
        )

    async def _recording_manifest(self) -> tuple[bytes, RecordingProfile]:
        credentials = self.camera._load_credentials()
        timeout = httpx.Timeout(self.camera.onvif_timeout_seconds)
        async with httpx.AsyncClient(
            transport=self.camera._onvif_transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            profiles_body = await self.camera._post_onvif(
                client, credentials=credentials, operation="GetProfiles"
            )
            profile = self._select_recording_profile(
                self.camera._profiles_from_response(profiles_body)
            )

            def stream_setup(operation: ET.Element) -> None:
                setup = ET.SubElement(operation, f"{{{exterior_camera_svc._MEDIA_NS}}}StreamSetup")
                ET.SubElement(
                    setup, f"{{{exterior_camera_svc._SCHEMA_NS}}}Stream"
                ).text = "RTP-Unicast"
                transport = ET.SubElement(
                    setup, f"{{{exterior_camera_svc._SCHEMA_NS}}}Transport"
                )
                ET.SubElement(
                    transport, f"{{{exterior_camera_svc._SCHEMA_NS}}}Protocol"
                ).text = "RTSP"
                ET.SubElement(
                    operation, f"{{{exterior_camera_svc._MEDIA_NS}}}ProfileToken"
                ).text = profile.token

            uri_body = await self.camera._post_onvif(
                client,
                credentials=credentials,
                operation="GetStreamUri",
                body_builder=stream_setup,
            )
            uri = self.camera._stream_uri_from_response(uri_body, host=credentials.host)
        manifest = self.camera._concat_manifest(uri)
        uri = ""
        return manifest, profile

    def _record_args(self) -> list[str]:
        session = _utc_now().strftime("%Y%m%dT%H%M%S%fZ")
        self._current_session_prefix = session
        self._actual_profile = None
        output = str(self.recordings_dir / f"{session}-%06d.mkv")
        return self.camera._base_args() + [
            "-map", "0:v:0",
            "-an",
            "-c:v", "copy",
            "-f", "segment",
            "-segment_format", "matroska",
            "-segment_time", str(self.segment_seconds),
            "-reset_timestamps", "1",
            output,
        ]

    async def _drain_stderr(self, reader) -> bytes:
        captured = bytearray()
        while True:
            chunk = await reader.read(64 * 1024)
            if not chunk:
                return bytes(captured)
            remaining = 64 * 1024 - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])

    @staticmethod
    def _safe_recorder_failure(stderr: bytes) -> str:
        lowered = bytes(stderr or b"").lower()
        if b"401" in lowered or b"unauthorized" in lowered:
            return "DVR camera credentials were rejected."
        if b"no space left" in lowered or b"disk full" in lowered:
            return "DVR storage is full; retention cleanup is retrying."
        return "DVR recorder lost the camera stream and is retrying."

    @staticmethod
    async def _terminate_process(process: Any) -> bool:
        if process is None or getattr(process, "returncode", None) is not None:
            return True
        try:
            process.terminate()
            await asyncio.wait_for(
                process.wait(), timeout=PROCESS_STOP_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
                await asyncio.wait_for(
                    process.wait(), timeout=PROCESS_STOP_TIMEOUT_SECONDS
                )
            except (asyncio.TimeoutError, ProcessLookupError, OSError):
                return getattr(process, "returncode", None) is not None
        except (ProcessLookupError, OSError):
            return getattr(process, "returncode", None) is not None
        return getattr(process, "returncode", None) is not None

    @staticmethod
    async def _finish_wait_task(wait_task: Optional[asyncio.Task[Any]]) -> None:
        if wait_task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(wait_task), timeout=1)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            wait_task.cancel()
            await asyncio.gather(wait_task, return_exceptions=True)

    @staticmethod
    def _safe_start_failure(exc: Exception) -> str:
        text = str(exc)
        if isinstance(exc, RuntimeError) and text in {
            "DVR drive E: is not available.",
            "DVR root must use an absolute Windows drive path.",
        }:
            return text
        if isinstance(exc, RuntimeError) and text.startswith("DVR root must stay on E:"):
            return text
        if isinstance(exc, (OSError, sqlite3.Error)):
            return "DVR storage is unavailable; recording will retry."
        return f"DVR recorder could not start ({type(exc).__name__})."

    async def _start_recorder(self) -> None:
        await asyncio.to_thread(self._ensure_storage_sync)
        manifest, profile = await self._recording_manifest()
        args = self._record_args()
        creationflags = 0x08000000 if os.name == "nt" else 0
        process = None
        try:
            process = await self._process_factory(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
            # Own the child immediately.  Pipe validation or a canceled stdin
            # write must never leave an FFmpeg process outside shutdown's view.
            self._process = process
            self._wait_task = asyncio.create_task(process.wait())
            if process.stdin is None or process.stderr is None:
                raise RuntimeError("FFmpeg did not expose recorder pipes.")
            process.stdin.write(manifest)
            await process.stdin.drain()
            process.stdin.close()
            wait_closed = getattr(process.stdin, "wait_closed", None)
            if wait_closed:
                await wait_closed()
            self._stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))
            self._profile = profile
            self._recording_started_at = _utc_iso(_utc_now())
            self._progress_name = None
            self._progress_size = -1
            self._last_progress_at = time.monotonic()
            self._last_error = None
            log.info(
                "DVR recording started: %s %sx%s -> %s",
                profile.encoding, profile.width, profile.height, self.recordings_dir,
            )
        except BaseException:
            wait_task = self._wait_task
            stopped = await self._terminate_process(process)
            if stopped:
                self._process = None
                self._wait_task = None
                await self._finish_wait_task(wait_task)
            else:
                self._last_error = (
                    "DVR recorder did not stop; process ownership was retained."
                )
            raise
        finally:
            manifest = b""

    async def _stop_recorder(self) -> None:
        process = self._process
        wait_task = self._wait_task
        stopped = await self._terminate_process(process)
        if not stopped:
            self._last_error = (
                "DVR recorder did not stop; process ownership was retained."
            )
            return
        self._process = None
        self._wait_task = None
        await self._finish_wait_task(wait_task)
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._stderr_task), timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._stderr_task.cancel()
            self._stderr_task = None
        await self._index_segments(force=True)

    async def run_forever(self) -> None:
        failures = 0
        try:
            while not self._stopped:
                if self._process is None:
                    await self._index_segments(force=True)
                    await self._prune()
                    try:
                        await self._start_recorder()
                    except asyncio.CancelledError:
                        raise
                    except exterior_camera_svc.ExteriorCameraError as exc:
                        self._last_error = str(exc)
                    except Exception as exc:
                        if not (
                            self._process is not None
                            and getattr(self._process, "returncode", None) is None
                        ):
                            self._last_error = self._safe_start_failure(exc)
                    if self._process is None:
                        failures += 1
                        delay = min(
                            MAX_RESTART_DELAY_SECONDS,
                            RESTART_DELAY_SECONDS * (2 ** min(failures - 1, 4)),
                        )
                        await asyncio.sleep(delay)
                        continue

                wait_task = self._wait_task
                if wait_task is None:
                    self._last_error = "DVR recorder ownership state was lost; restarting."
                    await self._stop_recorder()
                    failures += 1
                    continue

                done, _pending = await asyncio.wait(
                    {wait_task}, timeout=MAINTENANCE_SECONDS
                )
                if done:
                    stderr = b""
                    if self._stderr_task is not None:
                        try:
                            stderr = await asyncio.wait_for(
                                asyncio.shield(self._stderr_task), timeout=2
                            )
                        except asyncio.TimeoutError:
                            self._stderr_task.cancel()
                        except asyncio.CancelledError:
                            raise
                    self._last_error = self._safe_recorder_failure(stderr)
                    await self._stop_recorder()
                    await self._prune()
                    failures += 1
                    delay = min(
                        MAX_RESTART_DELAY_SECONDS,
                        RESTART_DELAY_SECONDS * (2 ** min(failures - 1, 4)),
                    )
                    await asyncio.sleep(delay)
                    continue

                await self._index_segments(force=True)
                await self._prune()
                if self._recorder_stalled():
                    self._last_error = (
                        "DVR recorder stopped producing data and is being restarted."
                    )
                    await self._stop_recorder()
                    failures += 1
                    continue
                failures = 0
        finally:
            await self._stop_recorder()

    def stop(self) -> None:
        self._stopped = True

    async def shutdown(self) -> None:
        self._stopped = True
        await self._stop_recorder()
        playback = list(self._playback_processes.values())
        self._playback_processes.clear()
        await asyncio.gather(
            *(self._terminate_process(process) for process in playback),
            return_exceptions=True,
        )

    async def status(self) -> dict[str, Any]:
        recording = self._process is not None and getattr(self._process, "returncode", None) is None
        try:
            usage = await asyncio.to_thread(self._disk_usage_sync)
            drive = {
                "path": str(self.root),
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "reserve_bytes": self.reserve_bytes,
            }
        except (OSError, RuntimeError):
            drive = {
                "path": str(self.root),
                "total_bytes": None,
                "used_bytes": None,
                "free_bytes": None,
                "reserve_bytes": self.reserve_bytes,
            }
        latest = (await self.list_segments(limit=1))
        actual_profile = self._actual_profile.public() if self._actual_profile else None
        if actual_profile is not None:
            actual_profile["metadata_source"] = "bitstream"
        advertised_profile = self._profile.public() if self._profile else None
        if advertised_profile is not None:
            advertised_profile["metadata_source"] = "onvif_advertised"
        return {
            "ok": recording,
            "recording": recording,
            "recorder_pid": getattr(self._process, "pid", None) if recording else None,
            "root": str(self.root),
            "segment_seconds": self.segment_seconds,
            "profile": actual_profile,
            "advertised_profile": advertised_profile,
            "recording_started_at": self._recording_started_at,
            "last_error": self._last_error,
            "onvif_motion_events": self._events_healthy,
            "last_motion_at": self._last_motion_at,
            "drive": drive,
            "latest_segment": latest[0] if latest else None,
        }

    @staticmethod
    def _event_url(credentials) -> str:
        return f"http://{credentials.host}:{exterior_camera_svc._ONVIF_PORT}/onvif/event_service"

    @staticmethod
    def _pinned_subscription_url(value: str, *, host: str) -> str:
        parsed = urlsplit(str(value or "").strip())
        if parsed.scheme != "http" or parsed.username or parsed.password or parsed.fragment:
            raise exterior_camera_svc.ExteriorCameraUnavailable(
                "Exterior camera ONVIF subscription address was invalid."
            )
        try:
            port = parsed.port
        except ValueError as exc:
            raise exterior_camera_svc.ExteriorCameraUnavailable(
                "Exterior camera ONVIF subscription address was invalid."
            ) from exc
        if port not in {None, exterior_camera_svc._ONVIF_PORT}:
            raise exterior_camera_svc.ExteriorCameraUnavailable(
                "Exterior camera ONVIF subscription address was invalid."
            )
        normalized_path = parsed.path or ""
        if not (
            normalized_path.casefold().startswith("/onvif/")
            or _XIONGMAI_PULLPOINT_PATH_RE.fullmatch(normalized_path)
        ):
            raise exterior_camera_svc.ExteriorCameraUnavailable(
                "Exterior camera ONVIF subscription address was invalid."
            )
        netloc = f"{host}:{exterior_camera_svc._ONVIF_PORT}"
        return urlunsplit(("http", netloc, parsed.path, parsed.query, ""))

    async def _post_event(
        self,
        client: httpx.AsyncClient,
        *,
        credentials,
        url: str,
        operation: str,
        body_builder: Optional[Callable[[ET.Element], None]] = None,
        namespace: str = _EVENTS_NS,
        allow_empty_response: bool = False,
    ) -> ET.Element:
        payload = exterior_camera_svc._soap_envelope(
            namespace=namespace,
            operation=operation,
            credentials=credentials,
            body_builder=body_builder,
        )
        action = _EVENT_ACTIONS[operation]
        try:
            async with client.stream(
                "POST",
                url,
                headers={
                    "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"',
                    "Accept": "application/soap+xml, text/xml",
                },
                content=payload,
            ) as response:
                if response.status_code in {401, 403}:
                    raise exterior_camera_svc.ExteriorCameraAuthError(
                        "Exterior camera ONVIF event credentials were rejected."
                    )
                if response.status_code < 200 or response.status_code >= 300:
                    raise exterior_camera_svc.ExteriorCameraUnavailable(
                        f"Exterior camera ONVIF event request failed ({response.status_code})."
                    )
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > exterior_camera_svc.MAX_ONVIF_RESPONSE_BYTES:
                        raise exterior_camera_svc.ExteriorCameraUnavailable(
                            "Exterior camera ONVIF event response was too large."
                        )
            response_bytes = bytes(raw)
            if allow_empty_response and not response_bytes.strip():
                return ET.Element("Body", {"xomni-vendor-empty": "1"})
            return exterior_camera_svc._parse_onvif_xml(response_bytes)
        finally:
            payload = b""

    @staticmethod
    def _event_response(body: ET.Element, expected: str) -> ET.Element:
        if any(
            exterior_camera_svc._xml_name(node) == "Fault"
            for node in body.iter()
        ):
            raise exterior_camera_svc.ExteriorCameraUnavailable(
                "Exterior camera ONVIF event request was rejected."
            )
        expected_namespace = (
            _WSN_NS
            if expected in {"RenewResponse", "UnsubscribeResponse"}
            else _EVENTS_NS
        )
        responses = [
            child
            for child in body
            if child.tag == f"{{{expected_namespace}}}{expected}"
        ]
        if len(responses) != 1:
            raise exterior_camera_svc.ExteriorCameraUnavailable(
                "Exterior camera ONVIF event response was invalid."
            )
        return responses[0]

    def _set_subscription_renewal(
        self,
        response: ET.Element,
        *,
        default_seconds: float = SUBSCRIPTION_RECREATE_SECONDS,
        require_times: bool = False,
    ) -> None:
        current_nodes = [
            node for node in response.iter()
            if exterior_camera_svc._xml_name(node) == "CurrentTime"
        ]
        termination_nodes = [
            node for node in response.iter()
            if exterior_camera_svc._xml_name(node) == "TerminationTime"
        ]
        renew_in = float(default_seconds)
        parsed_times = False
        if len(current_nodes) == 1 and len(termination_nodes) == 1:
            current = _parse_time(current_nodes[0].text)
            termination = _parse_time(termination_nodes[0].text)
            if current is not None and termination is not None:
                parsed_times = True
                lease_seconds = max(0.0, (termination - current).total_seconds())
                renew_in = max(
                    0.0,
                    min(lease_seconds * 0.5, max(0.0, lease_seconds - 30.0)),
                )
        if require_times and not parsed_times:
            raise exterior_camera_svc.ExteriorCameraUnavailable(
                "Exterior camera ONVIF event lease was invalid."
            )
        self._subscription_renew_at = time.monotonic() + renew_in

    async def _renew_subscription(self, client, credentials, url: str) -> None:
        def body_builder(operation: ET.Element) -> None:
            ET.SubElement(operation, f"{{{_WSN_NS}}}TerminationTime").text = "PT10M"

        body = await self._post_event(
            client,
            credentials=credentials,
            url=url,
            operation="Renew",
            body_builder=body_builder,
            namespace=_WSN_NS,
            allow_empty_response=True,
        )
        if body.attrib.get("xomni-vendor-empty") == "1":
            self._subscription_renew_at = (
                time.monotonic() + SUBSCRIPTION_RECREATE_SECONDS
            )
            return
        response = self._event_response(body, "RenewResponse")
        self._set_subscription_renewal(response, require_times=True)

    async def _unsubscribe_subscription(self, client, credentials, url: str) -> None:
        body = await self._post_event(
            client,
            credentials=credentials,
            url=url,
            operation="Unsubscribe",
            namespace=_WSN_NS,
            allow_empty_response=True,
        )
        if body.attrib.get("xomni-vendor-empty") == "1":
            return
        self._event_response(body, "UnsubscribeResponse")

    async def _unsubscribe_best_effort(self, client, credentials, url: str) -> None:
        try:
            await self._unsubscribe_subscription(client, credentials, url)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.info("ONVIF subscription cleanup was not acknowledged.")

    async def _create_subscription(self, client, credentials) -> str:
        def body_builder(operation: ET.Element) -> None:
            ET.SubElement(operation, f"{{{_EVENTS_NS}}}InitialTerminationTime").text = "PT10M"

        body = await self._post_event(
            client,
            credentials=credentials,
            url=self._event_url(credentials),
            operation="CreatePullPointSubscription",
            body_builder=body_builder,
        )
        response = self._event_response(body, "CreatePullPointSubscriptionResponse")
        addresses = [
            str(node.text or "").strip()
            for node in response.iter()
            if exterior_camera_svc._xml_name(node) == "Address" and str(node.text or "").strip()
        ]
        self._set_subscription_renewal(response, require_times=True)
        if not addresses:
            return self._event_url(credentials)
        return self._pinned_subscription_url(addresses[0], host=credentials.host)

    @staticmethod
    def _parse_bool(value: object) -> Optional[bool]:
        text = str(value or "").strip().casefold()
        if text in _BOOL_TRUE:
            return True
        if text in _BOOL_FALSE:
            return False
        return None

    @classmethod
    def motion_states_from_body(cls, body: ET.Element) -> list[bool]:
        states: list[bool] = []
        for notification in [
            node for node in body.iter()
            if exterior_camera_svc._xml_name(node) == "NotificationMessage"
        ]:
            topic_text = " ".join(
                str(node.text or "")
                for node in notification.iter()
                if exterior_camera_svc._xml_name(node) == "Topic"
            ).casefold()
            simple_items = [
                node for node in notification.iter()
                if exterior_camera_svc._xml_name(node) == "SimpleItem"
            ]
            motion_named = any(
                any(marker in str(node.attrib.get("Name") or "").casefold() for marker in ("motion", "move"))
                for node in simple_items
            )
            if "motion" not in topic_text and "move" not in topic_text and not motion_named:
                continue
            candidate: Optional[bool] = None
            for node in simple_items:
                name = str(node.attrib.get("Name") or "").casefold()
                if any(marker in name for marker in ("motion", "state", "alarm", "active")):
                    candidate = cls._parse_bool(node.attrib.get("Value"))
                    if candidate is not None:
                        break
            if candidate is None:
                for node in notification.iter():
                    if any(marker in exterior_camera_svc._xml_name(node).casefold() for marker in ("motion", "state")):
                        candidate = cls._parse_bool(node.text)
                        if candidate is not None:
                            break
            if candidate is not None:
                states.append(candidate)
        return states

    @staticmethod
    def _pull_cycle_delay(started_at: float, *, now: Optional[float] = None) -> float:
        elapsed = (time.monotonic() if now is None else now) - started_at
        return max(0.0, ONVIF_MIN_PULL_CYCLE_SECONDS - max(0.0, elapsed))

    async def motion_states(self) -> AsyncIterator[bool]:
        try:
            while not self._stopped:
                try:
                    credentials = self.camera._load_credentials()
                    timeout = httpx.Timeout(connect=5, read=30, write=5, pool=5)
                    async with httpx.AsyncClient(
                        transport=self.camera._onvif_transport,
                        timeout=timeout,
                        follow_redirects=False,
                        trust_env=False,
                    ) as client:
                        subscription_url = await self._create_subscription(client, credentials)
                        self._events_healthy = False
                        try:
                            while not self._stopped:
                                if time.monotonic() >= self._subscription_renew_at:
                                    await self._renew_subscription(
                                        client, credentials, subscription_url
                                    )

                                def pull_builder(operation: ET.Element) -> None:
                                    ET.SubElement(operation, f"{{{_EVENTS_NS}}}Timeout").text = "PT5S"
                                    ET.SubElement(operation, f"{{{_EVENTS_NS}}}MessageLimit").text = "32"

                                pull_started_at = time.monotonic()
                                body = await self._post_event(
                                    client,
                                    credentials=credentials,
                                    url=subscription_url,
                                    operation="PullMessages",
                                    body_builder=pull_builder,
                                )
                                response = self._event_response(
                                    body, "PullMessagesResponse"
                                )
                                self._set_subscription_renewal(
                                    response, require_times=True
                                )
                                # Only a correctly shaped PullMessages response
                                # grants ONVIF authority over frame-diff fallback.
                                self._events_healthy = True
                                for state in self.motion_states_from_body(body):
                                    if state:
                                        self._last_motion_at = _utc_iso(_utc_now())
                                    yield state
                                # XM530 may ignore the requested PT5S long poll
                                # and return immediately. Cap all fast cycles so
                                # a healthy subscription cannot become a busy loop.
                                pull_delay = self._pull_cycle_delay(pull_started_at)
                                if pull_delay > 0:
                                    await asyncio.sleep(pull_delay)
                        finally:
                            self._events_healthy = False
                            await self._unsubscribe_best_effort(
                                client, credentials, subscription_url
                            )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._events_healthy = False
                    log.warning("ONVIF motion-event subscription unavailable: %s", exc)
                    await asyncio.sleep(RESTART_DELAY_SECONDS)
        finally:
            self._events_healthy = False

    async def _run_ffmpeg(
        self,
        args: list[str],
        *,
        timeout_seconds: float = PLAYBACK_TIMEOUT_SECONDS,
    ) -> tuple[int, bytes]:
        """Run one bounded playback worker while continually draining stderr.

        FFmpeg sends diagnostics to stderr. Keep draining that pipe while the
        worker runs so corrupt media cannot hold a failed request open.
        """
        ffmpeg = self.camera._require_ffmpeg()
        creationflags = 0x08000000 if os.name == "nt" else 0
        proc = await self._process_factory(
            str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        if proc.stderr is None:
            await self._terminate_process(proc)
            return -1, b"Playback worker did not expose diagnostics."
        key = id(proc)
        self._playback_processes[key] = proc
        stderr_task = asyncio.create_task(self._drain_stderr(proc.stderr))
        timed_out = False
        try:
            try:
                await asyncio.wait_for(
                    proc.wait(), timeout=max(1.0, float(timeout_seconds))
                )
            except asyncio.TimeoutError:
                timed_out = True
                await self._terminate_process(proc)
            except asyncio.CancelledError:
                await self._terminate_process(proc)
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
                raise
        finally:
            self._playback_processes.pop(key, None)
        try:
            stderr = await asyncio.wait_for(asyncio.shield(stderr_task), timeout=2)
        except asyncio.TimeoutError:
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            stderr = b""
        if timed_out:
            return -1, b"Playback preparation timed out. " + (stderr or b"")[-512:]
        return int(proc.returncode if proc.returncode is not None else -1), stderr or b""

    @staticmethod
    def _playback_codec_args(codec: object) -> list[str]:
        normalized = str(codec or "").replace(".", "").upper()
        if normalized in {"H264", "AVC", "AVC1"}:
            return ["-c:v", "copy"]
        # Archive recording remains native stream copy.  Only an explicit
        # browser playback artifact is transcoded, on demand and on CPU.  An
        # unknown/unprobed codec must fail safe to compatible H.264, never copy
        # an accidentally HEVC stream into a browser MP4.
        return [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p",
        ]

    @staticmethod
    def _segment_source_key(segment: dict[str, Any]) -> str:
        fingerprint = "|".join(
            str(segment.get(field) or "")
            for field in (
                "id", "filename", "started_at", "ended_at", "bytes", "codec",
                "width", "height", "probed", "source_mtime_ns",
            )
        )
        return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()

    async def _write_playback_target(
        self,
        target: Path,
        args_without_output: list[str],
        *,
        artifact_kind: Optional[str] = None,
        source_key: Optional[str] = None,
    ) -> Path:
        temp_target = self.playback_dir / (
            f".{target.stem}-{uuid.uuid4().hex}.part.mp4"
        )
        temp_target.unlink(missing_ok=True)
        try:
            rc, stderr = await self._run_ffmpeg([*args_without_output, str(temp_target)])
            if rc != 0 or not temp_target.is_file() or temp_target.stat().st_size <= 0:
                detail = stderr.decode("utf-8", "replace")[-500:]
                log.warning("DVR playback preparation failed (rc=%s): %s", rc, detail or "no FFmpeg diagnostic")
                if "timed out" in detail.casefold():
                    raise PlaybackPreparationError("Playback preparation timed out.")
                raise PlaybackPreparationError("Playback preparation failed.")
            os.replace(temp_target, target)
            if artifact_kind and source_key:
                async with self._db_lock:
                    await asyncio.to_thread(
                        self._register_artifact_sync,
                        target.name,
                        artifact_kind,
                        source_key,
                    )
            return target
        finally:
            temp_target.unlink(missing_ok=True)

    async def segment_playback(self, segment_id: int) -> Path:
        # Refresh and, when needed, bitstream-probe the immutable source before
        # taking the playback/retention lock. A cache key is only authoritative
        # when the indexed size and mtime still match the local archive file.
        await self._index_segments(force=True)
        self.playback_dir.mkdir(parents=True, exist_ok=True)
        target = self.playback_dir / f"segment-{segment_id}.mp4"
        async with self._io_lock:
            # Re-read only after acquiring the same lock used by retention and
            # playback creation so the source and cache fingerprint are one
            # coherent immutable view.
            segment = await self.get_segment(segment_id)
            if not segment:
                raise FileNotFoundError("Recording segment was not found.")
            if not bool(segment.get("complete")):
                raise FileNotFoundError("Recording segment is still being written.")
            source = self._segment_path(segment["filename"], require_file=True)
            try:
                source_stat = source.stat()
            except OSError as exc:
                raise FileNotFoundError("Recording segment is unavailable.") from exc
            if (
                int(segment.get("bytes") or -1) != source_stat.st_size
                or int(segment.get("source_mtime_ns") or -1) != source_stat.st_mtime_ns
            ):
                raise FileNotFoundError("Recording segment changed; retry playback.")
            source_key = self._segment_source_key(segment)
            if (
                target.is_file()
                and target.stat().st_size > 0
                and self.cache_artifact_is_tracked(
                    target.name, source_key=source_key
                )
            ):
                return target
            target.unlink(missing_ok=True)
            return await self._write_playback_target(
                target,
                [
                    "-y", "-i", str(source), "-map", "0:v:0", "-an",
                    *self._playback_codec_args(
                        segment.get("codec") if segment.get("probed") else None
                    ),
                    "-movflags", "+faststart",
                ],
                artifact_kind="segment",
                source_key=source_key,
            )

    async def range_clip(self, since: datetime, until: datetime, *, cache_name: str) -> Path:
        if until <= since:
            raise ValueError("Clip end must be after clip start.")
        since = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since.astimezone(timezone.utc)
        until = until.replace(tzinfo=timezone.utc) if until.tzinfo is None else until.astimezone(timezone.utc)
        if (until - since).total_seconds() > MAX_PLAYBACK_DURATION_SECONDS:
            raise ValueError("Continuous playback clips are limited to 30 minutes.")
        await self._index_segments(force=True)
        segments = await self.list_segments(
            since=_utc_iso(since),
            until=_utc_iso(until),
            limit=MAX_UI_SEGMENTS,
            complete_only=True,
        )
        segments = sorted(segments, key=lambda row: row["started_at"])
        if not segments:
            raise FileNotFoundError("No continuous recording covers that time range.")
        if len(segments) > MAX_PLAYBACK_SEGMENTS:
            raise ValueError("Continuous playback requires too many recording segments.")
        source_key = self._validate_range_coverage(segments, since, until)
        self.playback_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", cache_name).strip("-.")[:130] or "clip"
        target = self.playback_dir / f"{safe_name}.mp4"
        artifact_kind = "motion" if safe_name.startswith("motion-") else "range"
        async with self._io_lock:
            source_paths = [
                self._segment_path(row["filename"], require_file=True)
                for row in segments
            ]
            for row, source_path in zip(segments, source_paths):
                try:
                    source_stat = source_path.stat()
                except OSError as exc:
                    raise FileNotFoundError(
                        "Continuous recording source is unavailable."
                    ) from exc
                if (
                    int(row.get("bytes") or -1) != source_stat.st_size
                    or int(row.get("source_mtime_ns") or -1)
                    != source_stat.st_mtime_ns
                ):
                    raise FileNotFoundError(
                        "Continuous recording changed; retry playback."
                    )
            if (
                target.is_file()
                and target.stat().st_size > 0
                and self.cache_artifact_is_tracked(
                    target.name, source_key=source_key
                )
            ):
                return target
            target.unlink(missing_ok=True)
            first_start = _parse_time(segments[0]["started_at"]) or since
            offset = max(0.0, (since - first_start).total_seconds())
            duration = max(1.0, (until - since).total_seconds())
            if len(segments) > 1:
                if not all(row.get("probed") for row in segments):
                    raise FileNotFoundError(
                        "Continuous recording metadata is still being verified."
                    )
                formats = {
                    (
                        str(row.get("codec") or "").upper(),
                        int(row.get("width") or 0),
                        int(row.get("height") or 0),
                    )
                    for row in segments
                }
                if len(formats) != 1:
                    raise FileNotFoundError(
                        "Continuous recording format changed inside that time range."
                    )
            codecs = {
                str(row.get("codec") or "").upper()
                for row in segments
                if row.get("probed")
            }
            playback_codec = (
                next(iter(codecs))
                if len(codecs) == 1 and all(row.get("probed") for row in segments)
                else None
            )
            # Any multi-segment scratch stays on the assigned DVR volume.  A
            # playback request must never duplicate a large archive onto C:.
            with tempfile.TemporaryDirectory(
                prefix=".xomni-dvr-work-", dir=self.playback_dir
            ) as temp:
                temp_dir = Path(temp)
                if len(source_paths) == 1:
                    merged = source_paths[0]
                else:
                    concat_file = temp_dir / "segments.ffconcat"
                    concat_file.write_text(
                        "ffconcat version 1.0\n" + "".join(
                            f"file '{_ffconcat_path(path)}'\n" for path in source_paths
                        ),
                        encoding="utf-8",
                    )
                    merged = temp_dir / "merged.mkv"
                    rc, stderr = await self._run_ffmpeg([
                        "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
                        "-map", "0:v:0", "-an", "-c:v", "copy", str(merged),
                    ], timeout_seconds=CONCAT_TIMEOUT_SECONDS)
                    if rc != 0 or not merged.is_file():
                        detail = stderr.decode("utf-8", "replace")[-500:]
                        log.warning("DVR segment concat failed (rc=%s): %s", rc, detail or "no FFmpeg diagnostic")
                        if "timed out" in detail.casefold():
                            raise PlaybackPreparationError("Segment preparation timed out.")
                        raise PlaybackPreparationError("Segment preparation failed.")
                return await self._write_playback_target(
                    target,
                    [
                        "-y", "-ss", f"{offset:.3f}", "-i", str(merged),
                        "-t", f"{duration:.3f}", "-map", "0:v:0", "-an",
                        *self._playback_codec_args(playback_codec),
                        "-movflags", "+faststart",
                    ],
                    artifact_kind=artifact_kind,
                    source_key=source_key,
                )

    def _insert_clip_sync(
        self, filename: str, title: Optional[str], since: datetime, until: datetime, size: int
    ) -> int:
        conn = self._conn()
        try:
            cursor = conn.execute(
                """
                INSERT INTO clips (filename, title, started_at, ended_at, bytes, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (filename, title, _utc_iso(since), _utc_iso(until), int(size), _utc_iso(_utc_now())),
            )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()

    def _list_saved_clips_sync(self) -> list[dict[str, Any]]:
        if not self.db_path.is_file():
            return []
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM clips ORDER BY started_at DESC LIMIT ?", (MAX_UI_SEGMENTS,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    async def list_saved_clips(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_saved_clips_sync)

    def _get_saved_clip_sync(self, clip_id: int) -> Optional[dict[str, Any]]:
        if not self.db_path.is_file():
            return None
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def get_saved_clip(self, clip_id: int) -> Optional[dict[str, Any]]:
        return await asyncio.to_thread(self._get_saved_clip_sync, int(clip_id))

    def saved_clip_path(self, filename: object, *, require_file: bool = False) -> Path:
        name = str(filename or "")
        if not _SAFE_CLIP_FILENAME_RE.fullmatch(name):
            raise FileNotFoundError("Saved clip filename is invalid.")
        root = self.clips_dir.resolve()
        path = (self.clips_dir / name).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise FileNotFoundError("Saved clip is outside the DVR clip archive.") from exc
        if require_file and not path.is_file():
            raise FileNotFoundError("Saved clip file is missing.")
        return path

    async def delete_saved_clip(self, clip_id: int) -> bool:
        """Permanently remove one operator-saved clip. Never called by retention."""
        row = await self.get_saved_clip(clip_id)
        if row is None:
            return False
        async with self._io_lock:
            try:
                path = self.saved_clip_path(row["filename"])
            except FileNotFoundError:
                path = None
            if path is not None:
                await asyncio.to_thread(path.unlink, True)
            async with self._db_lock:
                conn = self._conn()
                try:
                    conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
                    conn.commit()
                finally:
                    conn.close()
        return True

    async def export_clip(
        self, since: datetime, until: datetime, *, title: Optional[str] = None
    ) -> dict[str, Any]:
        """Save a human-selected time range as a protected clip under clips/.

        Reuses the same bounded, hardened range-preparation pipeline as chat
        playback, then copies the result into storage retention never touches.
        """
        since = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since.astimezone(timezone.utc)
        until = until.replace(tzinfo=timezone.utc) if until.tzinfo is None else until.astimezone(timezone.utc)
        prepared = await self.range_clip(since, until, cache_name=f"export-{uuid.uuid4().hex}")
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        filename = f"clip-{int(since.timestamp())}-{int(until.timestamp())}-{uuid.uuid4().hex[:12]}.mp4"
        target = self.clips_dir / filename
        async with self._io_lock:
            await asyncio.to_thread(shutil.copy2, prepared, target)
        safe_title = (str(title).strip()[:200] or None) if title else None
        size = target.stat().st_size
        clip_id = await asyncio.to_thread(
            self._insert_clip_sync, filename, safe_title, since, until, size
        )
        return {
            "id": clip_id,
            "filename": filename,
            "title": safe_title,
            "started_at": _utc_iso(since),
            "ended_at": _utc_iso(until),
            "bytes": size,
        }

    @staticmethod
    def _footage_sample_times(
        since: datetime, until: datetime, sample_count: int
    ) -> list[datetime]:
        """Return chronological, inclusive strategic sample times.

        Including both endpoints is deliberate: a temporal conclusion needs a
        before and after view, not a collection of near-duplicate middle frames.
        """
        count = min(max(int(sample_count), MIN_FOOTAGE_ANALYSIS_SAMPLES), MAX_FOOTAGE_ANALYSIS_SAMPLES)
        span_seconds = max(0.0, (until - since).total_seconds())
        if count == 1 or span_seconds <= 0:
            return [since]
        return [
            since + timedelta(seconds=span_seconds * index / (count - 1))
            for index in range(count)
        ]

    @staticmethod
    def _footage_contact_sheet(samples: list[tuple[datetime, bytes]]) -> bytes:
        """Build one bounded chronological JPEG for the existing vision worker."""
        if not samples:
            raise PlaybackPreparationError("No DVR frames were available for analysis.")
        columns = min(3, len(samples))
        tile_width, tile_height, label_height, gutter = 400, 225, 24, 8
        rows = math.ceil(len(samples) / columns)
        sheet = Image.new(
            "RGB",
            (
                gutter + columns * (tile_width + gutter),
                gutter + rows * (tile_height + label_height + gutter),
            ),
            "black",
        )
        draw = ImageDraw.Draw(sheet)
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        for index, (captured_at, raw) in enumerate(samples):
            with Image.open(io.BytesIO(raw)) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
                image.thumbnail((tile_width, tile_height), resampling)
                tile = Image.new("RGB", (tile_width, tile_height), "black")
                tile.paste(
                    image,
                    ((tile_width - image.width) // 2, (tile_height - image.height) // 2),
                )
            column, row = index % columns, index // columns
            x = gutter + column * (tile_width + gutter)
            y = gutter + row * (tile_height + label_height + gutter)
            sheet.paste(tile, (x, y))
            timestamp = captured_at.astimezone().strftime("%H:%M:%S %Z")
            draw.text((x + 3, y + tile_height + 3), f"{index + 1}. {timestamp}", fill="white")
        encoded = io.BytesIO()
        sheet.save(encoded, format="JPEG", quality=88, optimize=True)
        return encoded.getvalue()

    async def footage_analysis_samples(
        self,
        since: datetime,
        until: datetime,
        *,
        sample_count: Optional[int] = None,
    ) -> dict[str, Any]:
        """Extract bounded, chronological actual-DVR samples for temporal review.

        This never decodes the recording continuously and never touches an
        active segment.  Each source is revalidated under the DVR I/O lock
        before FFmpeg reads it, so the model receives only immutable archive
        footage from the exact resolved interval.
        """
        if until <= since:
            raise ValueError("Footage-analysis end time must be after start time.")
        since = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since.astimezone(timezone.utc)
        until = until.replace(tzinfo=timezone.utc) if until.tzinfo is None else until.astimezone(timezone.utc)
        duration_seconds = (until - since).total_seconds()
        if duration_seconds > MAX_FOOTAGE_ANALYSIS_DURATION_SECONDS:
            raise ValueError("Footage analysis is limited to a three-minute DVR interval.")
        derived_count = max(
            MIN_FOOTAGE_ANALYSIS_SAMPLES,
            min(MAX_FOOTAGE_ANALYSIS_SAMPLES, math.ceil(duration_seconds / 12.0) + 1),
        )
        count = derived_count if sample_count is None else min(
            max(int(sample_count), MIN_FOOTAGE_ANALYSIS_SAMPLES),
            MAX_FOOTAGE_ANALYSIS_SAMPLES,
        )
        await self._index_segments(force=True)
        segments = await self.list_segments(
            since=_utc_iso(since),
            until=_utc_iso(until),
            limit=MAX_PLAYBACK_SEGMENTS,
            complete_only=True,
        )
        segments = sorted(segments, key=lambda row: row["started_at"])
        if not segments:
            raise FileNotFoundError("No continuous recording covers that analysis interval.")
        if len(segments) > MAX_PLAYBACK_SEGMENTS:
            raise ValueError("Footage analysis requires too many recording segments.")
        self._validate_range_coverage(segments, since, until)
        if not all(bool(row.get("probed")) for row in segments):
            raise FileNotFoundError("Continuous DVR metadata is still being verified.")
        sample_times = self._footage_sample_times(since, until, count)
        self.playback_dir.mkdir(parents=True, exist_ok=True)
        async with self._io_lock:
            indexed: list[tuple[datetime, datetime, dict[str, Any], Path]] = []
            for row in segments:
                source = self._segment_path(row["filename"], require_file=True)
                try:
                    source_stat = source.stat()
                except OSError as exc:
                    raise FileNotFoundError("Continuous recording source is unavailable.") from exc
                if (
                    int(row.get("bytes") or -1) != source_stat.st_size
                    or int(row.get("source_mtime_ns") or -1) != source_stat.st_mtime_ns
                ):
                    raise FileNotFoundError("Continuous recording changed; retry analysis.")
                started = _parse_time(row.get("started_at"))
                ended = _parse_time(row.get("ended_at"))
                if started is None or ended is None or ended <= started:
                    raise FileNotFoundError("Continuous recording coverage is incomplete.")
                indexed.append((started, ended, row, source))

            with tempfile.TemporaryDirectory(
                prefix=".xomni-dvr-analysis-", dir=self.playback_dir
            ) as temp:
                temp_dir = Path(temp)
                extracted: list[tuple[datetime, bytes]] = []
                for index, captured_at in enumerate(sample_times):
                    matches = [
                        item for item in indexed
                        if item[0] <= captured_at <= item[1]
                    ]
                    if not matches:
                        raise FileNotFoundError(
                            "Continuous recording does not cover a requested analysis frame."
                        )
                    started, ended, _row, source = max(matches, key=lambda item: item[0])
                    offset = min(
                        max(0.0, (captured_at - started).total_seconds()),
                        max(0.0, (ended - started).total_seconds() - 0.05),
                    )
                    frame_path = temp_dir / f"frame-{index:02d}.jpg"
                    rc, stderr = await self._run_ffmpeg(
                        [
                            "-y", "-ss", f"{offset:.3f}", "-i", str(source),
                            "-map", "0:v:0", "-frames:v", "1",
                            "-vf", f"scale={FOOTAGE_ANALYSIS_FRAME_WIDTH}:-2:force_original_aspect_ratio=decrease",
                            "-q:v", "4", str(frame_path),
                        ],
                        timeout_seconds=FOOTAGE_ANALYSIS_FRAME_TIMEOUT_SECONDS,
                    )
                    if rc != 0 or not frame_path.is_file() or frame_path.stat().st_size <= 0:
                        detail = stderr.decode("utf-8", "replace")[-500:]
                        log.warning("DVR frame extraction failed (rc=%s): %s", rc, detail or "no FFmpeg diagnostic")
                        if "timed out" in detail.casefold():
                            raise PlaybackPreparationError("DVR frame extraction timed out.")
                        raise PlaybackPreparationError("DVR frame extraction failed.")
                    try:
                        raw = frame_path.read_bytes()
                    except OSError as exc:
                        raise PlaybackPreparationError("DVR frame extraction failed.") from exc
                    if raw:
                        extracted.append((captured_at, raw))
                if len(extracted) < MIN_FOOTAGE_ANALYSIS_SAMPLES:
                    raise PlaybackPreparationError("Insufficient DVR frames were extracted for temporal analysis.")
                contact_sheet = await asyncio.to_thread(self._footage_contact_sheet, extracted)

        return {
            "analyzed_started_at": _utc_iso(since),
            "analyzed_ended_at": _utc_iso(until),
            "sample_count": len(extracted),
            "sampled_at": [_utc_iso(value) for value, _raw in extracted],
            "contact_sheet": contact_sheet,
            "source_segments": [
                {
                    "id": row["id"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "codec": row.get("codec"),
                    "width": row.get("width"),
                    "height": row.get("height"),
                }
                for row in segments
            ],
        }

    async def event_clip(self, store, burst_id: int) -> Path:
        events = store.list_camera_events_by_burst(int(burst_id))
        if not events:
            raise FileNotFoundError("Motion event was not found.")
        captured = sorted(
            value
            for value in (_parse_time(row.get("captured_at")) for row in events)
            if value is not None
        )
        if not captured:
            raise ValueError("Motion event timestamps are invalid.")
        try:
            return await self.range_clip(
                captured[0] - timedelta(seconds=10),
                captured[-1] + timedelta(seconds=20),
                cache_name=f"motion-{burst_id}",
            )
        except FileNotFoundError:
            # Padding is desirable, but a retention/restart boundary just
            # outside the documented burst must not demote real event footage
            # to the legacy still-frame timelapse. Retry the event itself.
            event_end = max(captured[-1], captured[0] + timedelta(seconds=1))
            return await self.range_clip(
                captured[0],
                event_end,
                cache_name=f"motion-{burst_id}",
            )

    async def tool_recordings(
        self, *, since: Optional[str], until: Optional[str], limit: int
    ) -> dict[str, Any]:
        segments = await self.list_segments(since=since, until=until, limit=min(limit, MAX_TOOL_SEGMENTS))
        return {
            "status": await self.status(),
            "segments": [
                {
                    "id": row["id"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "bytes": row["bytes"],
                    "codec": row["codec"],
                    "width": row["width"],
                    "height": row["height"],
                    "metadata_source": "bitstream" if row.get("probed") else "pending",
                    "complete": bool(row["complete"]),
                }
                for row in segments
            ],
        }


class DvrRangeClipRequest(BaseModel):
    since: str
    until: str


class DvrAnalysisSamplesRequest(BaseModel):
    since: str
    until: str


class DvrClipExportRequest(BaseModel):
    since: str
    until: str
    title: Optional[str] = None


def create_router(
    settings,
    store,
    require_session,
    dvr: CameraDVR,
    *,
    internal_token: Optional[str] = None,
    extra_allowed_origins: tuple[str, ...] = (),
) -> APIRouter:
    """Standalone, Owner-only DVR UI. It is separate from chat but shares auth.

    A narrow second credential -- a loopback-only shared token, never sent to
    a browser or the model -- lets X Omni Core call the handful of read/prep
    endpoints it needs as a server-to-server client with no session cookie of
    its own. It grants nothing a cookie-holding Owner could not already do.
    """
    router = APIRouter(prefix="/dvr", tags=["dvr"])

    async def require_owner(session: dict = Depends(require_session)) -> dict:
        if session.get("role") != "owner":
            raise HTTPException(403, "Owner authorization is required.")
        return session

    async def require_owner_or_internal(request: Request) -> dict:
        token = str(request.headers.get("x-xomni-internal-token") or "")
        if internal_token and token and hmac.compare_digest(token, internal_token):
            return {"role": "owner", "user_id": "xomni-core", "internal": True}
        session = await require_session(request)
        if session.get("role") != "owner":
            raise HTTPException(403, "Owner authorization is required.")
        return session

    def owner_session_id(session: dict) -> str:
        token_hash = str(session.get("token_hash") or "").strip()
        if token_hash:
            return f"session:{token_hash}"
        return f"local:{session.get('google_sub') or 'local-dev'}"

    def require_exact_origin(request: Request, message: str) -> None:
        origin = str(request.headers.get("origin") or "").strip().rstrip("/")
        allowed_origins = {
            str(getattr(settings, "local_origin", "") or "").rstrip("/"),
            str(getattr(settings, "public_origin", "") or "").rstrip("/"),
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        }
        allowed_origins.update(origin.rstrip("/") for origin in extra_allowed_origins if origin)
        allowed_origins.discard("")
        if not origin or origin not in allowed_origins:
            raise HTTPException(403, message)

    def exterior_camera_http_error(exc: BaseException) -> HTTPException:
        if isinstance(exc, exterior_camera_svc.ExteriorCameraAuthError):
            # A camera-side authentication failure is not an X Omni Owner
            # session failure. Keep the operator signed in and show the camera
            # error in the DVR instead of triggering the UI's 401 redirect.
            return HTTPException(502, "Exterior camera credentials were rejected.")
        if isinstance(exc, exterior_camera_svc.ExteriorCameraSessionNotFound):
            return HTTPException(404, str(exc))
        if isinstance(
            exc,
            (
                exterior_camera_svc.ExteriorCameraNotConfigured,
                exterior_camera_svc.ExteriorCameraConflict,
            ),
        ):
            return HTTPException(409, str(exc))
        if isinstance(exc, ValueError):
            return HTTPException(400, str(exc))
        return HTTPException(503, "Exterior camera is temporarily unavailable.")

    def bounded_id(value: int, label: str) -> int:
        if value < 1 or value > _SQLITE_MAX_ID:
            raise HTTPException(404, f"{label} not found.")
        return value

    def normalized_bounds(
        since: Optional[str], until: Optional[str], *, sqlite_format: bool
    ) -> tuple[Optional[str], Optional[str]]:
        normalized: list[Optional[str]] = []
        for raw in (since, until):
            if raw is None:
                normalized.append(None)
                continue
            parsed = _parse_time(raw)
            if parsed is None:
                raise HTTPException(400, "Time bounds must be valid ISO timestamps.")
            normalized.append(
                parsed.strftime("%Y-%m-%d %H:%M:%S")
                if sqlite_format
                else _utc_iso(parsed)
            )
        return normalized[0], normalized[1]

    def inline_video(path: Path) -> FileResponse:
        return FileResponse(
            path,
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'inline; filename="{path.name}"',
                "Cache-Control": "private, no-store",
            },
        )

    ui_root = Path(settings.root) / "ui" / "dvr"

    @router.get("")
    @router.get("/")
    async def dvr_home(_session: dict = Depends(require_owner)):
        path = ui_root / "index.html"
        if not path.is_file():
            raise HTTPException(404, "DVR UI is not installed.")
        return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-store"})

    @router.get("/style.css")
    async def dvr_css(_session: dict = Depends(require_owner)):
        return FileResponse(
            ui_root / "style.css", media_type="text/css", headers={"Cache-Control": "no-store"}
        )

    @router.get("/app.js")
    async def dvr_js(_session: dict = Depends(require_owner)):
        return FileResponse(
            ui_root / "app.js", media_type="text/javascript", headers={"Cache-Control": "no-store"}
        )

    @router.get("/api/status")
    async def dvr_status(_session: dict = Depends(require_owner_or_internal)):
        return await dvr.status()

    @router.post("/api/live/sessions")
    async def create_dvr_live_session(
        request: Request,
        session: dict = Depends(require_owner),
    ):
        require_exact_origin(
            request, "Starting a DVR live watch requires the exact X Omni origin."
        )
        content_length = str(request.headers.get("content-length") or "").strip()
        if content_length:
            try:
                if int(content_length) != 0:
                    raise HTTPException(400, "DVR live-session start accepts no body.")
            except ValueError:
                raise HTTPException(400, "Invalid live-session Content-Length.") from None
        try:
            async for chunk in request.stream():
                if chunk:
                    raise HTTPException(400, "DVR live-session start accepts no body.")
        except ClientDisconnect:
            raise HTTPException(499, "DVR live-session request was cancelled.") from None
        try:
            result = await dvr.camera.create_watch_session(
                owner_id=owner_session_id(session)
            )
        except exterior_camera_svc.ExteriorCameraError as exc:
            raise exterior_camera_http_error(exc) from exc
        except ValueError as exc:
            raise exterior_camera_http_error(exc) from exc
        session_id = str(result.get("session_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", session_id):
            raise HTTPException(503, "Exterior camera returned an invalid live session.")
        if await request.is_disconnected():
            try:
                await dvr.camera.delete_session(
                    session_id=session_id,
                    owner_id=owner_session_id(session),
                )
            except exterior_camera_svc.ExteriorCameraError:
                log.info("Disconnected DVR live-session request was already released")
            raise HTTPException(499, "DVR live-session request was cancelled.")
        audit = getattr(store, "audit", None)
        if callable(audit):
            audit(
                "standalone_dvr_live_session_started",
                {"label": result.get("label"), "streaming": False},
            )
        return {
            "ok": True,
            "status": result.get("status") or "ready",
            "session_id": session_id,
            "stream_url": f"/dvr/api/live/sessions/{session_id}/stream.mjpg",
            "label": result.get("label") or "Exterior camera",
            "expires_at": result.get("expires_at"),
            "streaming": False,
        }

    @router.get("/api/live/sessions/{camera_session_id}/stream.mjpg")
    async def dvr_live_stream(
        camera_session_id: str,
        session: dict = Depends(require_owner),
    ):
        try:
            iterator = await dvr.camera.stream(
                session_id=camera_session_id,
                owner_id=owner_session_id(session),
            )
        except exterior_camera_svc.ExteriorCameraError as exc:
            raise exterior_camera_http_error(exc) from exc
        return StreamingResponse(
            iterator,
            media_type="multipart/x-mixed-replace; boundary=xomni",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.delete("/api/live/sessions/{camera_session_id}")
    async def delete_dvr_live_session(
        camera_session_id: str,
        request: Request,
        session: dict = Depends(require_owner),
    ):
        require_exact_origin(
            request, "Stopping a DVR live watch requires the exact X Omni origin."
        )
        try:
            result = await dvr.camera.delete_session(
                session_id=camera_session_id,
                owner_id=owner_session_id(session),
            )
        except exterior_camera_svc.ExteriorCameraError as exc:
            raise exterior_camera_http_error(exc) from exc
        audit = getattr(store, "audit", None)
        if callable(audit):
            audit("standalone_dvr_live_session_stopped", {"streaming": False})
        return result

    @router.get("/api/segments")
    async def dvr_segments(
        day: Optional[str] = Query(default=None, alias="date"),
        since: Optional[str] = None,
        until: Optional[str] = None,
        _session: dict = Depends(require_owner_or_internal),
    ):
        if day:
            try:
                start, end = _local_day_bounds(day)
                since, until = _utc_iso(start), _utc_iso(end)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        else:
            since, until = normalized_bounds(since, until, sqlite_format=False)
        rows = await dvr.list_segments(since=since, until=until, limit=MAX_UI_SEGMENTS)
        return {"items": rows, "status": await dvr.status()}

    @router.get("/api/segments/{segment_id}/video.mp4")
    async def dvr_segment_video(segment_id: int, _session: dict = Depends(require_owner)):
        segment_id = bounded_id(segment_id, "Recording segment")
        try:
            path = await dvr.segment_playback(segment_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except Exception as exc:
            log.warning("DVR segment playback failed: %s", exc)
            raise HTTPException(503, "Recording could not be prepared for playback.") from exc
        return inline_video(path)

    @router.get("/api/events")
    async def dvr_events(
        day: Optional[str] = Query(default=None, alias="date"),
        since: Optional[str] = None,
        until: Optional[str] = None,
        _session: dict = Depends(require_owner),
    ):
        if day:
            try:
                start, end = _local_day_bounds(day)
                since = start.strftime("%Y-%m-%d %H:%M:%S")
                until = end.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        else:
            since, until = normalized_bounds(since, until, sqlite_format=True)
        rows = store.list_camera_events(since=since, until=until, limit=MAX_UI_EVENTS)
        bursts: dict[int, dict[str, Any]] = {}
        for row in reversed(rows):
            burst_id = row.get("burst_id")
            if burst_id is None:
                continue
            burst = bursts.setdefault(int(burst_id), {
                "burst_id": int(burst_id),
                "started_at": row["captured_at"],
                "ended_at": row["captured_at"],
                "caption": None,
                "person_detected": False,
                "vehicle_detected": False,
                "frame_count": 0,
                "snapshot_url": None,
                "video_url": f"/dvr/api/events/{int(burst_id)}/video.mp4",
            })
            burst["ended_at"] = row["captured_at"]
            burst["frame_count"] += 1
            if burst["snapshot_url"] is None:
                burst["snapshot_url"] = f"/api/camera-snapshots/{row['snapshot_filename']}"
            if row.get("caption"):
                is_positive = bool(row.get("person_detected") or row.get("vehicle_detected"))
                if burst["caption"] is None or is_positive:
                    burst["caption"] = row["caption"]
                    burst["snapshot_url"] = f"/api/camera-snapshots/{row['snapshot_filename']}"
            burst["person_detected"] = bool(burst["person_detected"] or row.get("person_detected"))
            burst["vehicle_detected"] = bool(burst["vehicle_detected"] or row.get("vehicle_detected"))
        snapshots = [
            {
                "id": row["id"],
                "captured_at": row["captured_at"],
                "trigger": row["trigger"],
                "caption": row["caption"],
                "person_detected": bool(row["person_detected"]) if row["person_detected"] is not None else None,
                "vehicle_detected": bool(row["vehicle_detected"]) if row["vehicle_detected"] is not None else None,
                "burst_id": row.get("burst_id"),
                "snapshot_url": f"/api/camera-snapshots/{row['snapshot_filename']}",
            }
            for row in rows
        ]
        return {"bursts": list(reversed(list(bursts.values()))), "snapshots": snapshots}

    @router.get("/api/events/{burst_id}/video.mp4")
    async def dvr_event_video(burst_id: int, _session: dict = Depends(require_owner)):
        burst_id = bounded_id(burst_id, "Motion event")
        try:
            path = await dvr.event_clip(store, burst_id)
        except Exception as exc:
            log.info("Continuous DVR event footage unavailable; trying historical frames")
            events = store.list_camera_events_by_burst(burst_id)
            if not events:
                raise HTTPException(404, "Motion event not found.") from exc
            try:
                fallback = await camera_monitoring_svc.camera_motion_clip(
                    store,
                    settings,
                    dvr.camera.ffmpeg_path,
                    {"event_id": events[0]["id"]},
                )
            except Exception as fallback_exc:
                raise HTTPException(
                    503, "Motion footage could not be prepared for playback."
                ) from fallback_exc
            if not fallback.get("ok"):
                raise HTTPException(
                    503, "Motion footage could not be prepared for playback."
                ) from exc
            filename = Path(str(fallback.get("clip_url") or "")).name
            if not filename or not store.camera_clip_is_tracked(filename):
                raise HTTPException(503, "Motion footage could not be verified.") from exc
            path = Path(settings.camera_snapshot_dir) / camera_monitoring_svc.CLIP_SUBDIR / filename
            if not path.is_file():
                raise HTTPException(404, "Historical motion footage is missing.") from exc
        return inline_video(path)

    @router.get("/api/clips/{filename}")
    async def dvr_cached_clip(filename: str, _session: dict = Depends(require_owner)):
        if not _SAFE_CACHED_MP4_RE.fullmatch(filename):
            raise HTTPException(404, "Clip not found.")
        path = dvr.playback_dir / filename
        try:
            path.resolve().relative_to(dvr.playback_dir.resolve())
        except ValueError as exc:
            raise HTTPException(404, "Clip not found.") from exc
        if not path.is_file():
            raise HTTPException(404, "Clip not found.")
        if not dvr.cache_artifact_is_tracked(filename):
            raise HTTPException(404, "Clip not found.")
        return inline_video(path)

    def _required_iso(value: str, label: str) -> datetime:
        parsed = _parse_time(value)
        if parsed is None:
            raise HTTPException(400, f"{label} must be a valid ISO timestamp.")
        return parsed

    def _prep_error(exc: Exception) -> HTTPException:
        if isinstance(exc, PlaybackPreparationError):
            return HTTPException(503, str(exc))
        if isinstance(exc, FileNotFoundError):
            return HTTPException(404, str(exc))
        if isinstance(exc, ValueError):
            return HTTPException(400, str(exc))
        log.warning("DVR playback preparation failed", exc_info=True)
        return HTTPException(503, "DVR playback could not be prepared.")

    @router.post("/api/clips/range")
    async def dvr_prepare_range_clip(
        body: DvrRangeClipRequest,
        _session: dict = Depends(require_owner_or_internal),
    ):
        """Server-to-server-only bounded range prep; range_clip enforces bounds."""
        since = _required_iso(body.since, "since")
        until = _required_iso(body.until, "until")
        if until <= since:
            raise HTTPException(400, "until must be after since.")
        try:
            path = await dvr.range_clip(
                since, until,
                cache_name=f"range-{int(since.timestamp())}-{int(until.timestamp())}",
            )
        except (PlaybackPreparationError, FileNotFoundError, ValueError) as exc:
            raise _prep_error(exc) from exc
        return {"filename": path.name}

    @router.post("/api/events/{burst_id}/clip")
    async def dvr_prepare_event_clip(
        burst_id: int,
        _session: dict = Depends(require_owner_or_internal),
    ):
        burst_id = bounded_id(burst_id, "Motion event")
        try:
            path = await dvr.event_clip(store, burst_id)
        except (PlaybackPreparationError, FileNotFoundError, ValueError) as exc:
            raise _prep_error(exc) from exc
        except Exception as exc:
            raise HTTPException(404, "Motion event footage could not be prepared.") from exc
        return {"filename": path.name}

    @router.post("/api/analysis/samples")
    async def dvr_analysis_samples(
        body: DvrAnalysisSamplesRequest,
        _session: dict = Depends(require_owner_or_internal),
    ):
        since = _required_iso(body.since, "since")
        until = _required_iso(body.until, "until")
        try:
            samples = await dvr.footage_analysis_samples(since, until)
        except (PlaybackPreparationError, FileNotFoundError, ValueError) as exc:
            raise _prep_error(exc) from exc
        payload = dict(samples)
        contact_sheet = bytes(payload.pop("contact_sheet"))
        payload["contact_sheet_base64"] = base64.b64encode(contact_sheet).decode("ascii")
        return payload

    @router.post("/api/clips/export")
    async def dvr_export_clip(
        body: DvrClipExportRequest,
        request: Request,
        session: dict = Depends(require_owner),
    ):
        require_exact_origin(request, "Saving a DVR clip requires the exact X Omni origin.")
        since = _required_iso(body.since, "since")
        until = _required_iso(body.until, "until")
        if until <= since:
            raise HTTPException(400, "Clip end must be after clip start.")
        try:
            result = await dvr.export_clip(since, until, title=body.title)
        except (PlaybackPreparationError, FileNotFoundError, ValueError) as exc:
            raise _prep_error(exc) from exc
        audit = getattr(store, "audit", None)
        if callable(audit):
            audit(
                "standalone_dvr_clip_saved",
                {"clip_id": result["id"], "title": result.get("title")},
            )
        result["video_url"] = f"/dvr/api/clips-saved/{result['id']}/video.mp4"
        return result

    @router.get("/api/clips-saved")
    async def dvr_saved_clips(_session: dict = Depends(require_owner)):
        rows = await dvr.list_saved_clips()
        return {
            "items": [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "bytes": row["bytes"],
                    "created_at": row["created_at"],
                    "video_url": f"/dvr/api/clips-saved/{row['id']}/video.mp4",
                }
                for row in rows
            ]
        }

    @router.get("/api/clips-saved/{clip_id}/video.mp4")
    async def dvr_saved_clip_video(clip_id: int, _session: dict = Depends(require_owner)):
        clip_id = bounded_id(clip_id, "Saved clip")
        row = await dvr.get_saved_clip(clip_id)
        if row is None:
            raise HTTPException(404, "Saved clip not found.")
        path = dvr.saved_clip_path(row["filename"], require_file=True)
        return inline_video(path)

    @router.delete("/api/clips-saved/{clip_id}")
    async def dvr_delete_saved_clip(
        clip_id: int, request: Request, session: dict = Depends(require_owner)
    ):
        require_exact_origin(request, "Deleting a DVR clip requires the exact X Omni origin.")
        clip_id = bounded_id(clip_id, "Saved clip")
        deleted = await dvr.delete_saved_clip(clip_id)
        if not deleted:
            raise HTTPException(404, "Saved clip not found.")
        audit = getattr(store, "audit", None)
        if callable(audit):
            audit("standalone_dvr_clip_deleted", {"clip_id": clip_id})
        return {"ok": True}

    @router.get("/api/healthz")
    async def dvr_healthz():
        return {"ok": True, "service": "dvr"}

    return router
