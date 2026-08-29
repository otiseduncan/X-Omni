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
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from . import exterior_camera as exterior_camera_svc

log = logging.getLogger("xomni.camera_dvr")

DEFAULT_DVR_ROOT = Path("E:/XOmni-DVR")
SEGMENT_SECONDS = 300
OPERATING_RESERVE_BYTES = 2 * 1024 * 1024 * 1024
MAINTENANCE_SECONDS = 30
RESTART_DELAY_SECONDS = 5
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
}
_BOOL_TRUE = {"true", "1", "on", "active", "yes"}
_BOOL_FALSE = {"false", "0", "off", "inactive", "no"}
_SEGMENT_RE = re.compile(r"^(\d{8})-(\d{6})\.mkv$")
_SAFE_MP4_RE = re.compile(r"^[A-Za-z0-9_.-]{1,160}\.mp4$")


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


def _local_day_bounds(day_text: str) -> tuple[str, str]:
    try:
        selected = date.fromisoformat(day_text)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    start_local = datetime.combine(selected, dt_time.min, tzinfo=local_tz)
    end_local = start_local + timedelta(days=1)
    return _utc_iso(start_local), _utc_iso(end_local)


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
    ):
        self.camera = exterior_camera
        self.root = Path(root)
        self.recordings_dir = self.root / "recordings"
        self.playback_dir = self.root / "playback-cache"
        self.db_path = self.root / "dvr.sqlite"
        self.segment_seconds = max(60, int(segment_seconds))
        self.reserve_bytes = max(256 * 1024 * 1024, int(reserve_bytes))
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._process = None
        self._stderr_task: Optional[asyncio.Task[bytes]] = None
        self._stopped = False
        self._recording_started_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._profile: Optional[RecordingProfile] = None
        self._events_healthy = False
        self._last_motion_at: Optional[str] = None
        self._db_lock = asyncio.Lock()

    @property
    def events_healthy(self) -> bool:
        return bool(self._events_healthy)

    def _volume_anchor(self) -> Path:
        if os.name == "nt":
            drive = self.root.drive or "E:"
            return Path(f"{drive}/")
        return self.root

    def _ensure_storage_sync(self) -> None:
        anchor = self._volume_anchor()
        if os.name == "nt" and str(anchor).upper() != "E:\\":
            raise RuntimeError("DVR root must stay on E:.")
        if not anchor.exists():
            raise RuntimeError("DVR drive E: is not available.")
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.playback_dir.mkdir(parents=True, exist_ok=True)
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
                    indexed_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_segments_started ON segments(started_at)")
            conn.commit()
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _segment_start_from_name(self, filename: str) -> Optional[datetime]:
        match = _SEGMENT_RE.fullmatch(filename)
        if not match:
            return None
        naive_local = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        return naive_local.replace(tzinfo=local_tz).astimezone(timezone.utc)

    async def _index_segments(self) -> None:
        try:
            await asyncio.to_thread(self._ensure_storage_sync)
        except RuntimeError as exc:
            self._last_error = str(exc)
            return
        files = sorted(self.recordings_dir.glob("*.mkv"))
        parsed: list[tuple[Path, datetime]] = []
        for path in files:
            started = self._segment_start_from_name(path.name)
            if started is not None:
                parsed.append((path, started))
        active = self._process is not None and getattr(self._process, "returncode", None) is None
        profile = self._profile
        stamp = _utc_iso(_utc_now())
        async with self._db_lock:
            conn = self._conn()
            try:
                present = {path.name for path, _ in parsed}
                for index, (path, started) in enumerate(parsed):
                    next_started = parsed[index + 1][1] if index + 1 < len(parsed) else None
                    complete = (index + 1 < len(parsed)) or not active
                    ended = next_started or (started + timedelta(seconds=self.segment_seconds))
                    try:
                        size = path.stat().st_size
                    except OSError:
                        continue
                    conn.execute(
                        """
                        INSERT INTO segments
                            (filename, started_at, ended_at, bytes, codec, width, height,
                             complete, indexed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(filename) DO UPDATE SET
                            started_at=excluded.started_at,
                            ended_at=excluded.ended_at,
                            bytes=excluded.bytes,
                            codec=COALESCE(excluded.codec, segments.codec),
                            width=COALESCE(excluded.width, segments.width),
                            height=COALESCE(excluded.height, segments.height),
                            complete=excluded.complete,
                            indexed_at=excluded.indexed_at
                        """,
                        (
                            path.name,
                            _utc_iso(started),
                            _utc_iso(ended),
                            size,
                            profile.encoding if profile else None,
                            profile.width if profile else None,
                            profile.height if profile else None,
                            int(complete),
                            stamp,
                        ),
                    )
                rows = conn.execute("SELECT id, filename FROM segments").fetchall()
                for row in rows:
                    if row["filename"] not in present:
                        conn.execute("DELETE FROM segments WHERE id = ?", (row["id"],))
                conn.commit()
            finally:
                conn.close()

    def _disk_usage_sync(self):
        return shutil.disk_usage(self._volume_anchor())

    async def _prune(self) -> None:
        try:
            usage = await asyncio.to_thread(self._disk_usage_sync)
        except OSError:
            return
        if usage.free >= self.reserve_bytes:
            await self._prune_old_playback_cache()
            return
        await self._clear_playback_cache()
        while True:
            try:
                usage = await asyncio.to_thread(self._disk_usage_sync)
            except OSError:
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
                    path = self.recordings_dir / row["filename"]
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
        for path in self.playback_dir.glob("*.mp4"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    async def _prune_old_playback_cache(self) -> None:
        if not self.playback_dir.exists():
            return
        cutoff = _utc_now() - PLAYBACK_CACHE_MAX_AGE
        for path in self.playback_dir.glob("*.mp4"):
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                if modified < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    def _list_segments_sync(
        self, *, since: Optional[str], until: Optional[str], limit: int
    ) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        clauses = []
        params: list[Any] = []
        if since:
            clauses.append("ended_at >= ?")
            params.append(since)
        if until:
            clauses.append("started_at <= ?")
            params.append(until)
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
        self, *, since: Optional[str] = None, until: Optional[str] = None, limit: int = MAX_TOOL_SEGMENTS
    ) -> list[dict[str, Any]]:
        await self._index_segments()
        return await asyncio.to_thread(
            self._list_segments_sync, since=since, until=until, limit=limit
        )

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

    @staticmethod
    def _select_recording_profile(profiles) -> RecordingProfile:
        h264 = [p for p in profiles if p.encoding.replace(".", "").upper() in {"H264", "AVC"}]
        h265 = [p for p in profiles if p.encoding.replace(".", "").upper() in {"H265", "HEVC"}]
        candidates = h264 or h265
        if not candidates:
            raise exterior_camera_svc.ExteriorCameraUnavailable(
                "Exterior camera did not advertise an H.264 or H.265 recording profile."
            )
        selected = max(candidates, key=lambda p: (p.width * p.height, -p.ordinal))
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
        output = str(self.recordings_dir / "%Y%m%d-%H%M%S.mkv")
        return self.camera._base_args() + [
            "-map", "0:v:0",
            "-an",
            "-c:v", "copy",
            "-f", "segment",
            "-segment_time", str(self.segment_seconds),
            "-reset_timestamps", "1",
            "-strftime", "1",
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

    async def _start_recorder(self) -> None:
        await asyncio.to_thread(self._ensure_storage_sync)
        manifest, profile = await self._recording_manifest()
        args = self._record_args()
        creationflags = 0x08000000 if os.name == "nt" else 0
        try:
            process = await self._process_factory(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
            if process.stdin is None or process.stderr is None:
                raise RuntimeError("FFmpeg did not expose recorder pipes.")
            process.stdin.write(manifest)
            await process.stdin.drain()
            process.stdin.close()
            wait_closed = getattr(process.stdin, "wait_closed", None)
            if wait_closed:
                await wait_closed()
            self._process = process
            self._stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))
            self._profile = profile
            self._recording_started_at = _utc_iso(_utc_now())
            self._last_error = None
            log.info(
                "DVR recording started: %s %sx%s -> %s",
                profile.encoding, profile.width, profile.height, self.recordings_dir,
            )
        finally:
            manifest = b""

    async def _stop_recorder(self) -> None:
        process = self._process
        self._process = None
        if process is not None and getattr(process, "returncode", None) is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
            except (ProcessLookupError, OSError):
                pass
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._stderr_task), timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._stderr_task.cancel()
            self._stderr_task = None
        await self._index_segments()

    async def run_forever(self) -> None:
        while not self._stopped:
            if self._process is None or getattr(self._process, "returncode", None) is not None:
                if self._process is not None:
                    stderr = b""
                    if self._stderr_task is not None and self._stderr_task.done():
                        try:
                            stderr = self._stderr_task.result()
                        except Exception:
                            stderr = b""
                    detail = stderr.decode("utf-8", "replace")[-500:].strip()
                    self._last_error = detail or "Recorder process stopped unexpectedly."
                    self._process = None
                try:
                    await self._start_recorder()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error = str(exc)
                    await self._index_segments()
                    await asyncio.sleep(RESTART_DELAY_SECONDS)
                    continue
            try:
                await asyncio.wait_for(asyncio.shield(self._process.wait()), timeout=MAINTENANCE_SECONDS)
            except asyncio.TimeoutError:
                await self._index_segments()
                await self._prune()
                continue
        await self._stop_recorder()

    def stop(self) -> None:
        self._stopped = True

    async def shutdown(self) -> None:
        self._stopped = True
        await self._stop_recorder()

    async def status(self) -> dict[str, Any]:
        await self._index_segments()
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
        except OSError:
            drive = {
                "path": str(self.root),
                "total_bytes": None,
                "used_bytes": None,
                "free_bytes": None,
                "reserve_bytes": self.reserve_bytes,
            }
        latest = (await self.list_segments(limit=1))
        return {
            "ok": recording or self.root.exists(),
            "recording": recording,
            "root": str(self.root),
            "segment_seconds": self.segment_seconds,
            "profile": self._profile.public() if self._profile else None,
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
        if not parsed.path.startswith("/onvif/"):
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
    ) -> ET.Element:
        payload = exterior_camera_svc._soap_envelope(
            namespace=_EVENTS_NS,
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
            return exterior_camera_svc._parse_onvif_xml(bytes(raw))
        finally:
            payload = b""

    async def _create_subscription(self, client, credentials) -> str:
        def body_builder(operation: ET.Element) -> None:
            ET.SubElement(operation, f"{{{_WSN_NS}}}InitialTerminationTime").text = "PT10M"

        body = await self._post_event(
            client,
            credentials=credentials,
            url=self._event_url(credentials),
            operation="CreatePullPointSubscription",
            body_builder=body_builder,
        )
        addresses = [
            str(node.text or "").strip()
            for node in body.iter()
            if exterior_camera_svc._xml_name(node) == "Address" and str(node.text or "").strip()
        ]
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

    async def motion_states(self) -> AsyncIterator[bool]:
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
                    self._events_healthy = True
                    while not self._stopped:
                        def pull_builder(operation: ET.Element) -> None:
                            ET.SubElement(operation, f"{{{_EVENTS_NS}}}Timeout").text = "PT20S"
                            ET.SubElement(operation, f"{{{_EVENTS_NS}}}MessageLimit").text = "32"

                        body = await self._post_event(
                            client,
                            credentials=credentials,
                            url=subscription_url,
                            operation="PullMessages",
                            body_builder=pull_builder,
                        )
                        for state in self.motion_states_from_body(body):
                            if state:
                                self._last_motion_at = _utc_iso(_utc_now())
                            yield state
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._events_healthy = False
                log.warning("ONVIF motion-event subscription unavailable: %s", exc)
                await asyncio.sleep(RESTART_DELAY_SECONDS)

    async def _run_ffmpeg(self, args: list[str]) -> tuple[int, bytes]:
        ffmpeg = self.camera._require_ffmpeg()
        creationflags = 0x08000000 if os.name == "nt" else 0
        proc = await self._process_factory(
            str(ffmpeg), *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        _out, stderr = await proc.communicate()
        return int(proc.returncode if proc.returncode is not None else -1), stderr or b""

    async def segment_playback(self, segment_id: int) -> Path:
        segment = await self.get_segment(segment_id)
        if not segment:
            raise FileNotFoundError("Recording segment was not found.")
        source = self.recordings_dir / segment["filename"]
        if not source.is_file():
            raise FileNotFoundError("Recording segment file is missing.")
        self.playback_dir.mkdir(parents=True, exist_ok=True)
        target = self.playback_dir / f"segment-{segment_id}.mp4"
        if target.is_file() and target.stat().st_size > 0:
            return target
        rc, stderr = await self._run_ffmpeg([
            "-y", "-i", str(source), "-map", "0:v:0", "-an", "-c:v", "copy",
            "-movflags", "+faststart", str(target),
        ])
        if rc != 0 or not target.is_file() or target.stat().st_size <= 0:
            target.unlink(missing_ok=True)
            raise RuntimeError(stderr.decode("utf-8", "replace")[-500:] or "Playback remux failed.")
        return target

    async def range_clip(self, since: datetime, until: datetime, *, cache_name: str) -> Path:
        if until <= since:
            raise ValueError("Clip end must be after clip start.")
        segments = await self.list_segments(
            since=_utc_iso(since), until=_utc_iso(until), limit=MAX_UI_SEGMENTS
        )
        segments = sorted(segments, key=lambda row: row["started_at"])
        if not segments:
            raise FileNotFoundError("No continuous recording covers that time range.")
        self.playback_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", cache_name).strip("-.")[:130] or "clip"
        target = self.playback_dir / f"{safe_name}.mp4"
        if target.is_file() and target.stat().st_size > 0:
            return target

        first_start = _parse_time(segments[0]["started_at"]) or since
        offset = max(0.0, (since - first_start).total_seconds())
        duration = max(1.0, (until - since).total_seconds())
        with tempfile.TemporaryDirectory(prefix="xomni-dvr-") as temp:
            temp_dir = Path(temp)
            if len(segments) == 1:
                merged = self.recordings_dir / segments[0]["filename"]
            else:
                concat_file = temp_dir / "segments.ffconcat"
                concat_file.write_text(
                    "ffconcat version 1.0\n" + "".join(
                        f"file '{_ffconcat_path(self.recordings_dir / row['filename'])}'\n"
                        for row in segments
                    ),
                    encoding="utf-8",
                )
                merged = temp_dir / "merged.mkv"
                rc, stderr = await self._run_ffmpeg([
                    "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
                    "-map", "0:v:0", "-an", "-c:v", "copy", str(merged),
                ])
                if rc != 0:
                    raise RuntimeError(stderr.decode("utf-8", "replace")[-500:] or "Segment concat failed.")
            rc, stderr = await self._run_ffmpeg([
                "-y", "-ss", f"{offset:.3f}", "-i", str(merged),
                "-t", f"{duration:.3f}", "-map", "0:v:0", "-an",
                "-c:v", "copy", "-movflags", "+faststart", str(target),
            ])
            if rc != 0 or not target.is_file() or target.stat().st_size <= 0:
                target.unlink(missing_ok=True)
                raise RuntimeError(stderr.decode("utf-8", "replace")[-500:] or "Clip extraction failed.")
        return target

    async def event_clip(self, store, burst_id: int) -> Path:
        events = store.list_camera_events_by_burst(int(burst_id))
        if not events:
            raise FileNotFoundError("Motion event was not found.")
        first = _parse_time(events[0]["captured_at"])
        last = _parse_time(events[-1]["captured_at"])
        if first is None or last is None:
            raise ValueError("Motion event timestamps are invalid.")
        return await self.range_clip(
            first - timedelta(seconds=10),
            last + timedelta(seconds=20),
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
                    "complete": bool(row["complete"]),
                }
                for row in segments
            ],
        }


def create_router(settings, store, require_session, dvr: CameraDVR) -> APIRouter:
    """Standalone, Owner-only DVR UI. It is separate from chat but shares auth."""
    router = APIRouter(prefix="/dvr", tags=["dvr"])

    async def require_owner(session: dict = Depends(require_session)) -> dict:
        if session.get("role", "owner") != "owner":
            raise HTTPException(403, "Owner authorization is required.")
        return session

    ui_root = Path(settings.root) / "ui" / "dvr"

    @router.get("")
    async def dvr_home(_session: dict = Depends(require_owner)):
        path = ui_root / "index.html"
        if not path.is_file():
            raise HTTPException(404, "DVR UI is not installed.")
        return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-store"})

    @router.get("/style.css")
    async def dvr_css(_session: dict = Depends(require_owner)):
        return FileResponse(ui_root / "style.css", media_type="text/css")

    @router.get("/app.js")
    async def dvr_js(_session: dict = Depends(require_owner)):
        return FileResponse(ui_root / "app.js", media_type="text/javascript")

    @router.get("/api/status")
    async def dvr_status(_session: dict = Depends(require_owner)):
        return await dvr.status()

    @router.get("/api/segments")
    async def dvr_segments(
        day: Optional[str] = Query(default=None, alias="date"),
        since: Optional[str] = None,
        until: Optional[str] = None,
        _session: dict = Depends(require_owner),
    ):
        if day:
            try:
                since, until = _local_day_bounds(day)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        rows = await dvr.list_segments(since=since, until=until, limit=MAX_UI_SEGMENTS)
        return {"items": rows, "status": await dvr.status()}

    @router.get("/api/segments/{segment_id}/video.mp4")
    async def dvr_segment_video(segment_id: int, _session: dict = Depends(require_owner)):
        try:
            path = await dvr.segment_playback(segment_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except Exception as exc:
            log.warning("DVR segment playback failed: %s", exc)
            raise HTTPException(503, "Recording could not be prepared for playback.") from exc
        return FileResponse(path, media_type="video/mp4", filename=path.name)

    @router.get("/api/events")
    async def dvr_events(
        day: Optional[str] = Query(default=None, alias="date"),
        since: Optional[str] = None,
        until: Optional[str] = None,
        _session: dict = Depends(require_owner),
    ):
        if day:
            try:
                since, until = _local_day_bounds(day)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
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
            if row.get("caption"):
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
        try:
            path = await dvr.event_clip(store, burst_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except Exception as exc:
            log.warning("DVR event clip failed: %s", exc)
            raise HTTPException(503, "Motion footage could not be prepared for playback.") from exc
        return FileResponse(path, media_type="video/mp4", filename=path.name)

    @router.get("/api/clips/{filename}")
    async def dvr_cached_clip(filename: str, _session: dict = Depends(require_owner)):
        if not _SAFE_MP4_RE.fullmatch(filename):
            raise HTTPException(404, "Clip not found.")
        path = dvr.playback_dir / filename
        try:
            path.resolve().relative_to(dvr.playback_dir.resolve())
        except ValueError as exc:
            raise HTTPException(404, "Clip not found.") from exc
        if not path.is_file():
            raise HTTPException(404, "Clip not found.")
        return FileResponse(path, media_type="video/mp4", filename=path.name)

    return router
