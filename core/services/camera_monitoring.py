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
import shutil
import tempfile
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

CLIP_SUBDIR = "clips"
CLIP_FRAMERATE_FPS = 2

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
    "camera_motion_clip": {
        "description": (
            "Assemble the full documented motion window into one playable "
            "timelapse clip, most recent event first if event_id is omitted. "
            "Use whenever Otis asks to see or play the video/footage of a "
            "motion event, not just one still."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "integer",
                    "description": "Any motion event id from the burst to assemble; omit for the most recent burst.",
                },
            },
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
        self._current_burst_id: Optional[int] = None
        # Seeded from the DB, not zero, so a burst opened right after a Core
        # restart never reuses an id from before the restart.
        self._next_burst_id = store.get_max_camera_burst_id() + 1
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
        if starting_new_burst:
            self._current_burst_id = self._next_burst_id
            self._next_burst_id += 1
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
            self._current_burst_id = None

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
                burst_id=self._current_burst_id,
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
        clip_filenames = self.store.delete_camera_motion_clips_without_events()
        clip_dir = directory / CLIP_SUBDIR
        for filename in clip_filenames:
            try:
                (clip_dir / filename).unlink(missing_ok=True)
            except OSError:
                log.warning("could not remove orphaned motion clip %s", filename)


# --------------------------------------------------------------------------
# read-only tools
# --------------------------------------------------------------------------

def _snapshot_url(filename: str) -> str:
    return f"/api/camera-snapshots/{filename}"


def _local_time_str(captured_at_utc: str) -> str:
    """captured_at is SQLite's naive datetime('now') -- UTC with no offset
    marker. Give the model an unambiguous local string to quote directly
    instead of a bare "10:31:35" that reads as if it were already local
    time (this machine's local timezone, same convention as the "Right
    now" prompt section)."""
    try:
        parsed = datetime.strptime(captured_at_utc, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return captured_at_utc
    return parsed.astimezone().strftime("%Y-%m-%d %I:%M:%S %p %Z")


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
            "captured_at_local": _local_time_str(row["captured_at"]),
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
            "captured_at_local": _local_time_str(event["captured_at"]),
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
        "captured_at_local": _local_time_str(event["captured_at"]),
        "trigger": event["trigger"],
        "caption": description,
        "person_detected": person,
        "vehicle_detected": vehicle,
        "snapshot_url": _snapshot_url(event["snapshot_filename"]),
        "cached": False,
    }


def _clip_url(filename: str) -> str:
    return f"/api/camera-clips/{filename}"


async def _run_ffmpeg(ffmpeg_path, args, process_factory=None) -> tuple[int, bytes]:
    factory = process_factory or asyncio.create_subprocess_exec
    proc = await factory(
        str(ffmpeg_path), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    returncode = proc.returncode
    return (returncode if returncode is not None else -1), (stderr or b"")


async def _encode_motion_clip(
    settings, ffmpeg_path, events: list[dict], burst_id: int, *, process_factory=None,
) -> dict:
    """Stitch a burst's frames into a small constant-rate timelapse mp4. Not
    real editing -- just enough for Otis to watch the whole documented
    window play back instead of stepping through stills one at a time."""
    if not ffmpeg_path or not Path(ffmpeg_path).is_file():
        return {"ok": False, "error": "ffmpeg is not available; cannot assemble a clip."}

    source_dir = Path(settings.camera_snapshot_dir)
    clip_dir = source_dir / CLIP_SUBDIR
    clip_dir.mkdir(parents=True, exist_ok=True)
    filename = f"motion-{burst_id}.mp4"
    output_path = clip_dir / filename

    with tempfile.TemporaryDirectory(prefix="xomni-motion-clip-") as tmp:
        tmp_path = Path(tmp)
        frame_count = 0
        for event in events:
            source = source_dir / event["snapshot_filename"]
            try:
                shutil.copy(source, tmp_path / f"frame{frame_count + 1:04d}.jpg")
                frame_count += 1
            except OSError:
                log.warning("motion clip source frame missing: %s", event["snapshot_filename"])
        if frame_count == 0:
            return {"ok": False, "error": "None of this event's stored frames could be read."}

        ffmpeg_args = [
            "-y",
            "-framerate", str(CLIP_FRAMERATE_FPS),
            "-i", str(tmp_path / "frame%04d.jpg"),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output_path),
        ]
        returncode, stderr = await _run_ffmpeg(ffmpeg_path, ffmpeg_args, process_factory)
        if returncode != 0:
            output_path.unlink(missing_ok=True)
            log.warning(
                "ffmpeg motion clip encode failed (rc=%s): %s",
                returncode, stderr.decode("utf-8", "replace")[-500:],
            )
            return {"ok": False, "error": "The clip could not be encoded."}

    return {"ok": True, "filename": filename, "frame_count": frame_count}


async def camera_motion_clip(
    store, settings, ffmpeg_path, args: dict, *, process_factory=None,
) -> dict:
    raw_event_id = args.get("event_id")
    if raw_event_id is not None:
        try:
            requested_event_id = int(raw_event_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "event_id must be an integer."}
        event = store.get_camera_event(requested_event_id)
        if event is None:
            return {"ok": False, "error": f"No stored camera event with id {requested_event_id}."}
        burst_id = event.get("burst_id")
        if burst_id is None:
            return {
                "ok": False,
                "error": (
                    "That event is a documentation baseline, not a motion "
                    "event -- there is no clip for it."
                ),
            }
    else:
        burst_id = store.get_latest_motion_burst_id()
        if burst_id is None:
            return {"ok": False, "error": "No motion event has been recorded yet."}

    events = store.list_camera_events_by_burst(burst_id)
    if not events:
        return {"ok": False, "error": f"No stored frames for motion event {burst_id}."}

    cached = store.get_camera_motion_clip(burst_id)
    if cached is not None:
        filename, frame_count, is_cached = cached["filename"], cached["frame_count"], True
    else:
        encoded = await _encode_motion_clip(
            settings, ffmpeg_path, events, burst_id, process_factory=process_factory,
        )
        if not encoded.get("ok"):
            return encoded
        filename, frame_count, is_cached = encoded["filename"], encoded["frame_count"], False
        store.add_camera_motion_clip(
            burst_id=burst_id, filename=filename, frame_count=frame_count,
            first_event_id=events[0]["id"], last_event_id=events[-1]["id"],
        )

    caption = next((e["caption"] for e in events if e.get("caption")), None)
    return {
        "ok": True,
        "burst_id": burst_id,
        "clip_url": _clip_url(filename),
        "frame_count": frame_count,
        "started_at_local": _local_time_str(events[0]["captured_at"]),
        "ended_at_local": _local_time_str(events[-1]["captured_at"]),
        "caption": caption,
        "cached": is_cached,
    }
