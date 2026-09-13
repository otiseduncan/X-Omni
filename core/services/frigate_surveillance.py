"""The surveillance capabilities X offers, answered from Frigate.

This sits between Frigate's API (frigate_client) and the camera tools the
model already knows (camera_security). It exists so tool handlers never
speak HTTP, and so one place decides the bounds that keep a surveillance
question cheap: how long a range may be, how much video may move, how many
frames an analysis samples.

Nothing here is an archive. A clip is fetched, used, and dropped; the only
bytes that outlive a call are the contact sheet handed to the vision worker
and whatever the caller chooses to keep. Omega does not become a recorder.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

from . import footage_frames
from .frigate_client import (
    FrigateAuthError,
    FrigateCameraNotFound,
    FrigateClient,
    FrigateError,
    FrigateInvalidRequest,
    FrigateNoRecording,
    FrigateNotConfigured,
    FrigateState,
    FrigateUnavailable,
)

log = logging.getLogger("xomni.frigate_surveillance")

# The same bounds the previous recorder enforced, kept so the model's
# existing sense of what it may ask for stays true.
MAX_TOOL_PLAYBACK_DURATION_SECONDS = 300
MAX_FOOTAGE_ANALYSIS_DURATION_SECONDS = 180
MAX_PLAYBACK_DURATION_SECONDS = 30 * 60
MAX_RECORDING_SPANS = 40
MAX_EVENT_ITEMS = 50

# Frigate's clip export can trail the wall clock by a few seconds while the
# current segment is still being written; asking for footage that recent
# returns nothing useful rather than an error worth surfacing.
RECORDING_SETTLE_SECONDS = 10.0


class FootagePreparationError(RuntimeError):
    """Footage was reachable but could not be turned into evidence promptly."""


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _local(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %I:%M:%S %p %Z")


def _from_epoch(value: object) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def footage_url(since: datetime, until: datetime) -> str:
    """Where the browser fetches this exact range from, streamed via Core.

    Core proxies the bytes from Frigate on demand instead of writing a file
    Omega would then own. The range is in the URL, so the same question
    always addresses the same footage without a cache to keep coherent.
    """
    query = urlencode({"since": _iso(since), "until": _iso(until)})
    return f"/api/camera/footage.mp4?{query}"


def state_message(state: str, detail: Optional[str] = None) -> str:
    """One short, honest sentence per structured state, for the model to use."""
    base = {
        FrigateState.UNAVAILABLE: (
            "The Frigate recorder is not reachable right now, so there is no camera "
            "evidence to look at."
        ),
        FrigateState.AUTHENTICATION_REQUIRED: (
            "Frigate refused X's credential, so camera evidence cannot be read until "
            "it is registered again."
        ),
        FrigateState.NOT_CONFIGURED: (
            "Frigate is not configured on this machine yet, so there is no camera "
            "evidence available."
        ),
        FrigateState.CAMERA_UNAVAILABLE: (
            "Frigate is running but the exterior camera is not currently available to it."
        ),
        FrigateState.NO_RECORDING: (
            "Frigate has no recording covering that time."
        ),
        FrigateState.NO_EVENT_DATA: (
            "Frigate recorded no detections for that period. That is not the same as "
            "nothing having happened -- it means nothing crossed its detector."
        ),
        FrigateState.INVALID_REQUEST: "That request is outside the bounds X can ask Frigate for.",
    }.get(state, "Camera evidence is unavailable.")
    return f"{base} {detail}".strip() if detail else base


def error_result(exc: FrigateError, **extra: Any) -> dict[str, Any]:
    """Every Frigate failure reaches a tool in one predictable shape."""
    result: dict[str, Any] = {
        "ok": False,
        "source": "frigate",
        "state": exc.state,
        "error": state_message(exc.state),
        "detail": str(exc),
    }
    result.update(extra)
    return result


def resolve_ffmpeg(explicit: Optional[Path] = None) -> Optional[Path]:
    """Where FFmpeg lives, if it does.

    FFmpeg is needed only to cut stills out of a clip Frigate already
    produced -- never to talk to a camera, and never to record. Its absence
    disables footage analysis and nothing else, so it is discovered rather
    than required.
    """
    discovered = str(explicit or shutil.which("ffmpeg") or "").strip()
    if not discovered:
        return None
    candidate = Path(discovered)
    try:
        return candidate.resolve()
    except OSError:
        return candidate


class FrigateSurveillance:
    """Frigate-backed answers for X's existing camera capabilities."""

    def __init__(self, client: FrigateClient, *, ffmpeg_path: Optional[Path] = None):
        self.client = client
        self.ffmpeg_path = Path(ffmpeg_path) if ffmpeg_path else None

    # --------------------------------------------------------------- health

    async def status(self) -> dict[str, Any]:
        """Never raises: a status call is exactly where outages are reported."""
        if not self.client.configured():
            summary = self.client.configuration_summary()
            return {
                "ok": False,
                "state": FrigateState.NOT_CONFIGURED,
                "recording": False,
                "source": "frigate",
                "detail": state_message(FrigateState.NOT_CONFIGURED),
                **summary,
            }
        try:
            health = await self.client.health()
        except FrigateError as exc:
            return {
                "ok": False,
                "state": exc.state,
                "recording": False,
                "source": "frigate",
                "base_url": self.client.base_url,
                "camera": self.client.camera,
                "detail": state_message(exc.state, str(exc)),
            }
        available = health.get("state") == FrigateState.AVAILABLE
        return {
            "ok": available,
            "state": health.get("state"),
            # Frigate owns continuous recording whenever its camera is live;
            # X never records, so this reports Frigate's state, not its own.
            "recording": bool(health.get("camera_running")),
            "source": "frigate",
            "base_url": self.client.base_url,
            "camera": health.get("camera"),
            "camera_configured": health.get("camera_configured"),
            "camera_running": health.get("camera_running"),
            "cameras": health.get("cameras"),
            "version": health.get("version"),
            "detail": health.get("detail"),
        }

    # ------------------------------------------------------------ recordings

    async def recordings(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = MAX_RECORDING_SPANS,
    ) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        since_value = _utc(since) if since else now - timedelta(days=1)
        until_value = _utc(until) if until else now
        if until_value <= since_value:
            return []
        spans = await self.client.recording_spans(since_value, until_value)
        rows = [
            {
                "started_at": _iso(span["started_at"]),
                "ended_at": _iso(span["ended_at"]),
                "duration_seconds": span["duration_seconds"],
                "complete": True,
            }
            for span in spans
        ]
        return rows[-int(limit):] if limit else rows

    async def clip_bytes(self, since: datetime, until: datetime) -> bytes:
        """Raw MP4 bytes for a bounded range, straight from Frigate."""
        return await self.client.clip_bytes(since, until)

    async def playback(self, since: datetime, until: datetime) -> dict[str, Any]:
        """Confirm Frigate actually holds this range, then hand back its URL.

        The check matters: returning a link to footage that does not exist
        would put a broken player in the chat and let the model imply there
        was something to see.
        """
        since_value, until_value = _utc(since), _utc(until)
        if until_value <= since_value:
            raise FrigateInvalidRequest("The end of a playback range must follow its start.")
        duration = (until_value - since_value).total_seconds()
        if duration > MAX_TOOL_PLAYBACK_DURATION_SECONDS:
            raise FrigateInvalidRequest(
                f"Playback is limited to {MAX_TOOL_PLAYBACK_DURATION_SECONDS // 60} minutes."
            )
        spans = await self.client.recording_spans(since_value, until_value)
        if not spans:
            raise FrigateNoRecording("Frigate holds no recording for that range.")
        covered_from = max(since_value, min(span["started_at"] for span in spans))
        covered_to = min(until_value, max(span["ended_at"] for span in spans))
        if covered_to <= covered_from:
            raise FrigateNoRecording("Frigate holds no recording for that range.")
        partial = covered_from > since_value or covered_to < until_value
        return {
            "clip_url": footage_url(covered_from, covered_to),
            "started_at": _iso(covered_from),
            "ended_at": _iso(covered_to),
            "started_at_local": _local(covered_from),
            "ended_at_local": _local(covered_to),
            "partial": partial,
        }

    # -------------------------------------------------------------- analysis

    async def analysis_samples(
        self,
        since: datetime,
        until: datetime,
        *,
        sample_count: Optional[int] = None,
    ) -> dict[str, Any]:
        """A chronological contact sheet built from Frigate's own recording.

        The clip exists only inside this call: bytes in, frames out, nothing
        written where it would survive the answer.
        """
        since_value, until_value = _utc(since), _utc(until)
        if until_value <= since_value:
            raise FrigateInvalidRequest("Analysis end time must be after its start time.")
        duration = (until_value - since_value).total_seconds()
        if duration > MAX_FOOTAGE_ANALYSIS_DURATION_SECONDS:
            raise FrigateInvalidRequest(
                f"Bounded footage analysis covers at most "
                f"{MAX_FOOTAGE_ANALYSIS_DURATION_SECONDS // 60} minutes at a time."
            )
        if self.ffmpeg_path is None:
            raise FootagePreparationError(
                "FFmpeg is not available on this machine, so recorded frames cannot be sampled."
            )
        clip = await self.client.clip_bytes(since_value, until_value)
        try:
            built = await footage_frames.build_contact_sheet(
                clip,
                since_value,
                since_value,
                until_value,
                ffmpeg_path=self.ffmpeg_path,
                sample_count=sample_count,
            )
        except footage_frames.FrameExtractionError as exc:
            raise FootagePreparationError(str(exc)) from exc
        finally:
            del clip
        return {
            "analyzed_started_at": _iso(since_value),
            "analyzed_ended_at": _iso(until_value),
            "sample_count": built["sample_count"],
            "sampled_at": [_iso(value) for value in built["sampled_at"]],
            "contact_sheet": built["contact_sheet"],
            "source_segments": [str(self.client.camera)],
        }

    # ---------------------------------------------------------------- events

    async def events(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Frigate's review records, normalized into X's event shape.

        Only what Frigate actually reported is represented. Nothing is
        inferred, merged, or invented to fill a quiet period.
        """
        records = await self.client.review(
            since=since, until=until, limit=max(1, min(int(limit), MAX_EVENT_ITEMS))
        )
        rows: list[dict[str, Any]] = []
        for record in records:
            started = _from_epoch(record.get("start_time"))
            if started is None:
                continue
            ended = _from_epoch(record.get("end_time"))
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            labels = [str(label) for label in (data.get("objects") or []) if label]
            zones = [str(zone) for zone in (data.get("zones") or []) if zone]
            detections = [str(value) for value in (data.get("detections") or []) if value]
            rows.append(
                {
                    "id": str(record.get("id") or ""),
                    "severity": str(record.get("severity") or ""),
                    "started_at": _iso(started),
                    "ended_at": _iso(ended) if ended else None,
                    "started_at_local": _local(started),
                    "ended_at_local": _local(ended) if ended else None,
                    "labels": labels,
                    "zones": zones,
                    "detection_ids": detections,
                    "reviewed": bool(record.get("has_been_reviewed")),
                    "person_detected": "person" in labels,
                    "vehicle_detected": any(
                        label in labels
                        for label in ("car", "truck", "bus", "motorcycle", "bicycle")
                    ),
                    "clip_url": (
                        footage_url(started, ended) if ended and ended > started else None
                    ),
                }
            )
        rows.sort(key=lambda row: row["started_at"], reverse=True)
        return rows

    async def event_window(self, event_id: str) -> tuple[datetime, datetime, dict[str, Any]]:
        """The bounded time span one Frigate review record covers."""
        records = await self.client.review(limit=MAX_EVENT_ITEMS)
        for record in records:
            if str(record.get("id") or "") == str(event_id):
                started = _from_epoch(record.get("start_time"))
                ended = _from_epoch(record.get("end_time")) or (
                    started + timedelta(seconds=30) if started else None
                )
                if started is None or ended is None or ended <= started:
                    raise FrigateNoRecording("That Frigate event has no usable time span.")
                return started, ended, record
        raise FrigateNoRecording("Frigate has no review record with that id.")

    async def latest_frame(self, *, height: Optional[int] = None) -> bytes:
        return await self.client.latest_frame(height=height)

    async def event_snapshot(self, event_id: str) -> bytes:
        return await self.client.event_snapshot(event_id)


__all__ = [
    "FootagePreparationError",
    "FrigateAuthError",
    "FrigateCameraNotFound",
    "FrigateError",
    "FrigateInvalidRequest",
    "FrigateNoRecording",
    "FrigateNotConfigured",
    "FrigateState",
    "FrigateSurveillance",
    "FrigateUnavailable",
    "MAX_FOOTAGE_ANALYSIS_DURATION_SECONDS",
    "MAX_PLAYBACK_DURATION_SECONDS",
    "MAX_TOOL_PLAYBACK_DURATION_SECONDS",
    "error_result",
    "footage_url",
    "state_message",
]
