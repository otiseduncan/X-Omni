"""
X Omni -- background exterior-camera monitoring.

Every camera_monitor_interval_seconds (default 60s): capture one frame,
diff it against the previous frame for motion, and independently check
whether camera_baseline_interval_seconds (default 600s) has elapsed since
the last stored documentation baseline. A capture/diff failure is logged
and the loop continues -- this must never crash Core.

Baseline events are cheap: stored, never captioned automatically. Motion
events are captioned immediately (a real but rare model-swap cost -- see
HANDOFF's single-GPU-resident-worker constraint) so a push notification can
be sent only when the caption actually confirms a person or vehicle, never
on a bare motion score (avoids spamming on wind/lighting changes).

The retention sweep runs every tick too (cheap) and deletes rows/files
older than camera_snapshot_retention_days.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from PIL import Image

from . import camera as camera_svc
from . import push_notifications
from ..models.router import WorkerSwapError

log = logging.getLogger("xomni.camera_monitoring")

MOTION_DIFF_SIZE = (64, 48)
MAX_HISTORY_ITEMS = 50
DEFAULT_HISTORY_ITEMS = 20

_MOTION_ANALYSIS_PROMPT = (
    "Reply in exactly this three-line format:\n"
    "PERSON: yes or no\n"
    "VEHICLE: yes or no\n"
    "DESCRIPTION: one plain sentence describing exactly what is visible\n"
    "Base every line only on what the pixels actually show. If nothing "
    "notable is visible, say PERSON: no, VEHICLE: no, and describe the "
    "empty scene."
)

CAMERA_MONITORING_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "camera_event_history": {
        "description": (
            "List stored exterior-camera snapshots (documentation baseline "
            "and motion events), most recent first. Use to find when "
            "something showed up or left."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "ISO datetime, inclusive lower bound."},
                "until": {"type": "string", "description": "ISO datetime, inclusive upper bound."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_HISTORY_ITEMS},
            },
            "additionalProperties": False,
        },
    },
    "camera_snapshot_analyze": {
        "description": (
            "Analyze one stored camera snapshot by event id and cache the "
            "result. Use after camera_event_history to answer what was in "
            "a specific frame."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "integer"},
                "prompt": {"type": "string", "maxLength": 1000},
            },
            "required": ["event_id"],
            "additionalProperties": False,
        },
    },
}


def _downscaled_grayscale(raw: bytes) -> list[int]:
    with Image.open(io.BytesIO(raw)) as image:
        return list(image.convert("L").resize(MOTION_DIFF_SIZE).getdata())


def motion_score(previous_raw: bytes, current_raw: bytes) -> float:
    """Mean absolute pixel difference (0-255) between two downscaled
    grayscale frames. Not a real CV model -- just enough to flag "something
    changed" between two stills roughly a minute apart."""
    previous = _downscaled_grayscale(previous_raw)
    current = _downscaled_grayscale(current_raw)
    if len(previous) != len(current) or not previous:
        return 255.0
    total = sum(abs(a - b) for a, b in zip(previous, current))
    return total / len(previous)


def _parse_structured_caption(text: str) -> tuple[Optional[bool], Optional[bool], str]:
    person: Optional[bool] = None
    vehicle: Optional[bool] = None
    description = text.strip()
    for line in text.splitlines():
        lowered = line.strip().casefold()
        if lowered.startswith("person:"):
            person = "yes" in lowered
        elif lowered.startswith("vehicle:"):
            vehicle = "yes" in lowered
        elif lowered.startswith("description:"):
            description = line.split(":", 1)[1].strip()
    return person, vehicle, description


class CameraMonitor:
    def __init__(self, settings, exterior_camera, router, store):
        self.settings = settings
        self.exterior_camera = exterior_camera
        self.router = router
        self.store = store
        self._previous_raw: Optional[bytes] = None
        self._last_baseline_at: Optional[float] = None
        self._burst_until: Optional[float] = None
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    async def run_forever(self) -> None:
        while not self._stopped:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("camera monitoring tick failed")
            interval = (
                self.settings.camera_motion_burst_interval_seconds
                if self._burst_until is not None
                else self.settings.camera_monitor_interval_seconds
            )
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    async def _tick(self) -> None:
        if self.exterior_camera is None:
            return
        frame = await self.exterior_camera.capture_snapshot()
        if frame is None:
            log.info("camera monitoring tick skipped: no frame captured")
            self._sweep_retention()
            return

        now = time.monotonic()
        already_in_burst = self._burst_until is not None and now < self._burst_until
        score: Optional[float] = None
        motion_now = False
        if self._previous_raw is not None:
            score = motion_score(self._previous_raw, frame.raw)
            motion_now = score >= self.settings.camera_motion_threshold
        self._previous_raw = frame.raw

        starting_new_burst = motion_now and not already_in_burst
        if motion_now:
            # Continued activity re-arms the rolling window instead of
            # letting it lapse -- floodlights staying on, someone still
            # moving, etc. all keep documentation going for as long as
            # they last, not just one snapshot.
            self._burst_until = now + self.settings.camera_motion_burst_seconds
        in_burst = self._burst_until is not None and now < self._burst_until
        if not in_burst:
            # Clear a stale/expired window outright -- run_forever only
            # checks "is this set at all" to pick its sleep interval, so a
            # window left dangling in the past would wrongly pin it to the
            # fast burst cadence forever after the first-ever motion event.
            self._burst_until = None

        is_baseline = (
            self._last_baseline_at is None
            or now - self._last_baseline_at >= self.settings.camera_baseline_interval_seconds
        )
        if is_baseline:
            self._last_baseline_at = now
            filename = self._write_snapshot(frame.raw, "interval")
            self.store.add_camera_event(trigger="interval", snapshot_filename=filename)

        if in_burst:
            filename = self._write_snapshot(frame.raw, "motion")
            event_id = self.store.add_camera_event(
                trigger="motion", snapshot_filename=filename, motion_score=score,
            )
            if starting_new_burst:
                # Caption/notify only once per burst, on the frame that
                # opened it -- every frame documents the event, but only
                # one vision call and one notification per event, not one
                # per five-second frame.
                await self._handle_motion_event(event_id, frame)

        self._sweep_retention()

    def _write_snapshot(self, raw: bytes, trigger: str) -> str:
        directory = Path(self.settings.camera_snapshot_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        filename = f"{stamp}-{trigger}.jpg"
        path = directory / filename
        counter = 1
        while path.exists():
            filename = f"{stamp}-{trigger}-{counter}.jpg"
            path = directory / filename
            counter += 1
        path.write_bytes(raw)
        return filename

    async def _handle_motion_event(self, event_id: int, frame) -> None:
        if not self.router.supports_vision():
            try:
                await self.router.ensure_capability(vision=True)
            except WorkerSwapError:
                log.warning("could not switch to a vision worker for a motion event")
                return
        try:
            raw_caption = await camera_svc.caption_frame(
                self.router, frame, _MOTION_ANALYSIS_PROMPT
            )
        except Exception:  # noqa: BLE001
            log.warning("motion-event captioning failed", exc_info=True)
            return
        person, vehicle, description = _parse_structured_caption(raw_caption)
        self.store.update_camera_event_caption(
            event_id, caption=description, person_detected=person, vehicle_detected=vehicle,
        )
        if person or vehicle:
            await self._notify(description)
            self.store.mark_camera_event_notified(event_id)

    async def _notify(self, description: str) -> None:
        owner = self.store.get_owner()
        if not owner:
            return
        user = self.store.get_user_by_google_sub(owner["google_sub"])
        if not user:
            return
        try:
            await push_notifications.send_push_async(
                self.store, self.settings, user["id"],
                "X noticed something", description,
            )
        except Exception:  # noqa: BLE001
            log.warning("push notification failed", exc_info=True)

    def _sweep_retention(self) -> None:
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=self.settings.camera_snapshot_retention_days)
        )
        cutoff_iso = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        filenames = self.store.delete_camera_events_older_than(cutoff_iso)
        directory = Path(self.settings.camera_snapshot_dir)
        for filename in filenames:
            try:
                (directory / filename).unlink(missing_ok=True)
            except OSError:
                log.warning("could not remove expired snapshot %s", filename)


# --------------------------------------------------------------------------
# read-only tools
# --------------------------------------------------------------------------

def _snapshot_url(filename: str) -> str:
    return f"/api/camera-snapshots/{filename}"


async def camera_event_history(store, args: dict) -> dict:
    since = args.get("since")
    until = args.get("until")
    limit = min(max(int(args.get("limit") or DEFAULT_HISTORY_ITEMS), 1), MAX_HISTORY_ITEMS)
    total = store.count_camera_events(since=since, until=until)
    rows = store.list_camera_events(since=since, until=until, limit=limit)
    items = [
        {
            "id": row["id"],
            "captured_at": row["captured_at"],
            "trigger": row["trigger"],
            "caption": row["caption"],
            "person_detected": (
                bool(row["person_detected"]) if row["person_detected"] is not None else None
            ),
            "vehicle_detected": (
                bool(row["vehicle_detected"]) if row["vehicle_detected"] is not None else None
            ),
            "snapshot_url": _snapshot_url(row["snapshot_filename"]),
        }
        for row in rows
    ]
    return {
        "ok": True,
        "total_count": total,
        "shown_count": len(items),
        "truncated": total > len(items),
        "items": items,
    }


async def camera_snapshot_analyze(store, router, settings, args: dict) -> dict:
    try:
        event_id = int(args.get("event_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "event_id must be an integer."}
    event = store.get_camera_event(event_id)
    if event is None:
        return {"ok": False, "error": f"No stored camera event with id {event_id}."}
    if event.get("caption"):
        return {
            "ok": True,
            "id": event["id"],
            "captured_at": event["captured_at"],
            "trigger": event["trigger"],
            "caption": event["caption"],
            "person_detected": (
                bool(event["person_detected"]) if event["person_detected"] is not None else None
            ),
            "vehicle_detected": (
                bool(event["vehicle_detected"]) if event["vehicle_detected"] is not None else None
            ),
            "snapshot_url": _snapshot_url(event["snapshot_filename"]),
            "cached": True,
        }

    path = Path(settings.camera_snapshot_dir) / event["snapshot_filename"]
    try:
        raw = path.read_bytes()
    except OSError:
        return {"ok": False, "error": "The stored snapshot file is missing."}
    try:
        frame = camera_svc.validate_camera_frame(raw, "image/jpeg")
    except ValueError as exc:
        return {"ok": False, "error": f"Stored snapshot could not be decoded: {exc}"}

    custom_prompt = args.get("prompt")
    prompt = camera_svc.camera_prompt(custom_prompt) if custom_prompt else _MOTION_ANALYSIS_PROMPT

    if not router.supports_vision():
        try:
            await router.ensure_capability(vision=True)
        except WorkerSwapError as exc:
            return {"ok": False, "error": f"Could not switch to a vision-capable worker: {exc}"}
    try:
        raw_caption = await camera_svc.caption_frame(router, frame, prompt)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Analysis failed: {type(exc).__name__}."}

    if custom_prompt:
        description, person, vehicle = raw_caption.strip(), None, None
    else:
        person, vehicle, description = _parse_structured_caption(raw_caption)

    store.update_camera_event_caption(
        event_id, caption=description, person_detected=person, vehicle_detected=vehicle,
    )
    return {
        "ok": True,
        "id": event["id"],
        "captured_at": event["captured_at"],
        "trigger": event["trigger"],
        "caption": description,
        "person_detected": person,
        "vehicle_detected": vehicle,
        "snapshot_url": _snapshot_url(event["snapshot_filename"]),
        "cached": False,
    }
