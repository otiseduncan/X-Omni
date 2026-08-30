"""Deterministic adapter for MediaMTX -- the exterior camera's media transport.

X Omni no longer runs its own DVR encoder/transcoder/playback pipeline.
MediaMTX (X:\\MediaMTX, an independently-managed process outside this repo;
see mediamtx_config.py for how its camera paths get the current camera
credentials) now owns:
  - the RTSP connection to the camera
  - continuous native recording to E:\\MediaMTX\\recordings
  - RTSP restreaming, HLS, and WebRTC live delivery
  - recorded-time-range playback (its own Playback API builds and serves an
    MP4 for an arbitrary time span -- X Omni's old segment-stitching/
    transcoding code for this is retired, not reimplemented)

This module is the one place X Omni talks to it: bounded HTTP calls to
MediaMTX's Control API (path/health status) and Playback API (recorded time
spans, clip retrieval). Both bind loopback-only, and no camera credential
ever reaches this layer -- MediaMTX already holds them directly.

Two camera paths are configured (see mediamtx_config.py):
  PATH_MAIN -- "exterior": the camera's highest-resolution profile, for the
    primary archive. Its ONVIF metadata claims H.264 but the real bitstream
    is HEVC (a known quirk of this camera), so it is not used for browser
    playback.
  PATH_LIVE -- "exterior_sub": the camera's smallest genuinely H.264
    profile, recorded in parallel specifically so live view and historical
    scrubbing both play natively in a browser with zero transcoding.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("xomni.mediamtx_client")

PATH_MAIN = "exterior"
PATH_LIVE = "exterior_sub"

_HEALTH_TIMEOUT_SECONDS = 5.0
_STATUS_TIMEOUT_SECONDS = 5.0
_LIST_TIMEOUT_SECONDS = 8.0
# A clip fetch's own bound is proportional to its requested duration
# (MediaMTX has to mux the span server-side); this is the floor/ceiling
# around that scaling, not a fixed budget for every request.
_CLIP_MIN_TIMEOUT_SECONDS = 15.0
_CLIP_MAX_TIMEOUT_SECONDS = 90.0
_CLIP_SECONDS_PER_TIMEOUT_SECOND = 4.0
MAX_CLIP_DURATION_SECONDS = 30 * 60
MAX_CLIP_BYTES = 512 * 1024 * 1024


class MediaMTXError(RuntimeError):
    """Base class for safe, operator-facing MediaMTX adapter errors."""


class MediaMTXUnavailable(MediaMTXError):
    """MediaMTX could not be reached or returned a server-side failure."""


class MediaMTXNotFound(MediaMTXError):
    """MediaMTX has no matching path, recording, or time span."""


class MediaMTXInvalidRequest(MediaMTXError):
    """The request itself (bounds, path name) was invalid."""


@dataclass(frozen=True)
class RecordingSpan:
    """One continuous recorded time span, as MediaMTX's Playback API reports it."""

    started_at: datetime
    duration_seconds: float

    @property
    def ended_at(self) -> datetime:
        from datetime import timedelta

        return self.started_at + timedelta(seconds=self.duration_seconds)


def _require_loopback(url: str, *, label: str) -> str:
    """Fail closed if a configured base URL is not loopback/private.

    Defense in depth: this adapter must never be pointed at a public or
    unexpected address by a configuration mistake, since MediaMTX's own
    APIs carry no authentication of their own.
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or "").strip()
    try:
        address = ipaddress.ip_address(host)
        allowed = address.is_loopback or address.is_private
    except ValueError:
        allowed = host.casefold() == "localhost"
    if not allowed:
        raise MediaMTXInvalidRequest(f"{label} must be a loopback or private address, not {host!r}.")
    return url.rstrip("/")


class MediaMTXClient:
    """Bounded, loopback-only adapter over MediaMTX's Control and Playback APIs."""

    def __init__(
        self,
        *,
        control_base_url: str = "http://127.0.0.1:9997",
        playback_base_url: str = "http://127.0.0.1:9996",
        hls_base_url: str = "http://127.0.0.1:8888",
        webrtc_base_url: str = "http://127.0.0.1:8889",
        rtsp_base_url: str = "rtsp://127.0.0.1:8554",
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.control_base_url = _require_loopback(control_base_url, label="MediaMTX control API")
        self.playback_base_url = _require_loopback(playback_base_url, label="MediaMTX playback API")
        # HLS/WebRTC/RTSP restream URLs are for display/proxying, not fetched
        # directly by this adapter -- still validated the same way so a bad
        # config can never quietly point the GUI at a non-local address.
        self.hls_base_url = _require_loopback(hls_base_url, label="MediaMTX HLS")
        self.webrtc_base_url = _require_loopback(webrtc_base_url, label="MediaMTX WebRTC")
        self.rtsp_base_url = rtsp_base_url.rstrip("/")
        # None in production (a real loopback connection); tests pass a
        # MockTransport to exercise this adapter without a socket.
        self._transport = transport

    # ---------- URL builders (no network I/O) ----------

    def hls_playlist_url(self, path: str) -> str:
        return f"{self.hls_base_url}/{path}/index.m3u8"

    def whep_url(self, path: str) -> str:
        return f"{self.webrtc_base_url}/{path}/whep"

    def rtsp_url(self, path: str) -> str:
        return f"{self.rtsp_base_url}/{path}"

    # ---------- Control API ----------

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT_SECONDS, trust_env=False, transport=self._transport) as client:
                response = await client.get(f"{self.control_base_url}/v3/info")
        except httpx.HTTPError as exc:
            raise MediaMTXUnavailable("MediaMTX control API is unreachable.") from exc
        if response.status_code != 200:
            raise MediaMTXUnavailable(f"MediaMTX control API returned {response.status_code}.")
        try:
            return response.json()
        except ValueError as exc:
            raise MediaMTXUnavailable("MediaMTX control API returned an invalid response.") from exc

    async def path_status(self, path: str) -> Optional[dict[str, Any]]:
        """Return MediaMTX's status for one path, or None if it has no source."""
        try:
            async with httpx.AsyncClient(timeout=_STATUS_TIMEOUT_SECONDS, trust_env=False, transport=self._transport) as client:
                response = await client.get(f"{self.control_base_url}/v3/paths/get/{path}")
        except httpx.HTTPError as exc:
            raise MediaMTXUnavailable("MediaMTX control API is unreachable.") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise MediaMTXUnavailable(f"MediaMTX control API returned {response.status_code}.")
        try:
            return response.json()
        except ValueError as exc:
            raise MediaMTXUnavailable("MediaMTX control API returned an invalid response.") from exc

    async def camera_online(self, path: str = PATH_MAIN) -> bool:
        status = await self.path_status(path)
        return bool(status and status.get("source") and status.get("ready"))

    # ---------- Playback API ----------

    @staticmethod
    def _iso(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    async def list_recordings(
        self, path: str, since: datetime, until: datetime,
    ) -> list[RecordingSpan]:
        if until <= since:
            raise MediaMTXInvalidRequest("until must be after since.")
        params = {"path": path, "start": self._iso(since), "end": self._iso(until)}
        try:
            async with httpx.AsyncClient(timeout=_LIST_TIMEOUT_SECONDS, trust_env=False, transport=self._transport) as client:
                response = await client.get(f"{self.playback_base_url}/list", params=params)
        except httpx.HTTPError as exc:
            raise MediaMTXUnavailable("MediaMTX playback API is unreachable.") from exc
        if response.status_code == 404:
            return []
        if response.status_code == 400:
            raise MediaMTXInvalidRequest("MediaMTX rejected the recording list request.")
        if response.status_code != 200:
            raise MediaMTXUnavailable(f"MediaMTX playback API returned {response.status_code}.")
        try:
            entries = response.json()
        except ValueError as exc:
            raise MediaMTXUnavailable("MediaMTX playback API returned an invalid response.") from exc
        if not isinstance(entries, list):
            raise MediaMTXUnavailable("MediaMTX playback API returned an invalid response.")
        spans: list[RecordingSpan] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            started = _parse_iso(entry.get("start"))
            duration = entry.get("duration")
            if started is None or not isinstance(duration, (int, float)) or duration <= 0:
                continue
            spans.append(RecordingSpan(started_at=started, duration_seconds=float(duration)))
        spans.sort(key=lambda span: span.started_at)
        return spans

    async def fetch_clip_bytes(
        self, path: str, since: datetime, duration_seconds: float, *, container: str = "mp4",
    ) -> bytes:
        """Download one bounded recorded time span as an MP4 file.

        MediaMTX builds this server-side from its own recorded segments --
        X Omni does no stitching, remuxing, or transcoding of the archive
        itself. The returned bytes are used internally (on-demand frame
        extraction for temporal analysis, or a human's "save clip" action);
        this method never streams to a browser directly.
        """
        if duration_seconds <= 0:
            raise MediaMTXInvalidRequest("duration must be positive.")
        if duration_seconds > MAX_CLIP_DURATION_SECONDS:
            raise MediaMTXInvalidRequest(
                f"Clip retrieval is limited to {MAX_CLIP_DURATION_SECONDS // 60} minutes."
            )
        if container not in {"fmp4", "mp4"}:
            raise MediaMTXInvalidRequest("container must be 'fmp4' or 'mp4'.")
        timeout = min(
            _CLIP_MAX_TIMEOUT_SECONDS,
            max(_CLIP_MIN_TIMEOUT_SECONDS, duration_seconds / _CLIP_SECONDS_PER_TIMEOUT_SECOND),
        )
        params = {
            "path": path, "start": self._iso(since),
            "duration": f"{duration_seconds:.3f}", "format": container,
        }
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False, transport=self._transport) as client:
                response = await client.get(f"{self.playback_base_url}/get", params=params)
        except httpx.TimeoutException as exc:
            raise MediaMTXUnavailable("MediaMTX clip retrieval timed out.") from exc
        except httpx.HTTPError as exc:
            raise MediaMTXUnavailable("MediaMTX playback API is unreachable.") from exc
        if response.status_code == 404:
            raise MediaMTXNotFound("No recording covers that time span.")
        if response.status_code == 400:
            raise MediaMTXInvalidRequest("MediaMTX rejected the clip request.")
        if response.status_code != 200:
            raise MediaMTXUnavailable(f"MediaMTX playback API returned {response.status_code}.")
        content = response.content
        if not content or len(content) > MAX_CLIP_BYTES:
            raise MediaMTXUnavailable("MediaMTX returned an unusable clip.")
        return content


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
