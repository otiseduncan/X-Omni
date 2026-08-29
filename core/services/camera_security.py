"""ONVIF-driven security monitoring layered over the proven camera monitor.

The legacy frame-difference monitor remains intact as a fallback. When the
Xiongmai PullPoint subscription is healthy, ONVIF motion is authoritative:
it opens/extends the existing burst, captures an immediate frame, and runs the
same person/vehicle vision contract. Continuous DVR footage is preferred for
playback while the existing still-frame timelapse remains a fallback.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import camera as camera_svc
from . import camera_monitoring as legacy
from . import camera_dvr as camera_dvr_svc
from . import exterior_camera as exterior_camera_svc
from . import push_notifications
from ..models.router import WorkerSwapError

log = logging.getLogger("xomni.camera_security")

SECURITY_TOOL_SCHEMAS = copy.deepcopy(legacy.CAMERA_MONITORING_TOOL_SCHEMAS)
SECURITY_TOOL_SCHEMAS["camera_event_history"]["description"] = (
    "Read stored exterior-camera snapshots and motion events, plus continuous-DVR "
    "status and (when requested) recording segments. Use for security history."
)
SECURITY_TOOL_SCHEMAS["camera_event_history"]["parameters"]["properties"][
    "include_recordings"
] = {
    "type": "boolean",
    "description": "Include bounded continuous-DVR segment metadata for the same time range.",
}
SECURITY_TOOL_SCHEMAS["camera_motion_clip"]["description"] = (
    "Show real continuous DVR footage for a motion event or explicit time range. "
    "If continuous footage is unavailable, fall back to the stored-frame timelapse."
)
SECURITY_TOOL_SCHEMAS["camera_motion_clip"]["parameters"]["properties"].update(
    {
        "since": {
            "type": "string",
            "description": "Optional ISO start time for continuous DVR playback.",
        },
        "until": {
            "type": "string",
            "description": "Optional ISO end time for continuous DVR playback.",
        },
    }
)

_DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
_EVENT_TOPIC_MARKERS = ("motion", "move", "human", "people", "person", "vehicle", "car")


class XiongmaiDVR(camera_dvr_svc.CameraDVR):
    """DVR with Xiongmai-tolerant ONVIF event discovery and topic parsing."""

    async def _discover_event_url(self, client, credentials) -> Optional[str]:
        def body_builder(operation):
            import xml.etree.ElementTree as ET
            ET.SubElement(operation, f"{{{_DEVICE_NS}}}Category").text = "Events"

        payload = exterior_camera_svc._soap_envelope(
            namespace=_DEVICE_NS, operation="GetCapabilities",
            credentials=credentials, body_builder=body_builder,
        )
        url = f"http://{credentials.host}:{exterior_camera_svc._ONVIF_PORT}/onvif/device_service"
        action = f"{_DEVICE_NS}/GetCapabilities"
        try:
            async with client.stream(
                "POST", url,
                headers={
                    "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"',
                    "Accept": "application/soap+xml, text/xml",
                },
                content=payload,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    return None
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > exterior_camera_svc.MAX_ONVIF_RESPONSE_BYTES:
                        return None
            body = exterior_camera_svc._parse_onvif_xml(bytes(raw))
            for node in body.iter():
                if exterior_camera_svc._xml_name(node) != "Events":
                    continue
                for child in node.iter():
                    if exterior_camera_svc._xml_name(child) == "XAddr" and str(child.text or "").strip():
                        return self._pinned_subscription_url(str(child.text).strip(), host=credentials.host)
        except Exception:
            return None
        finally:
            payload = b""
        return None

    async def _create_subscription(self, client, credentials) -> str:
        discovered = await self._discover_event_url(client, credentials)
        candidates = [
            discovered,
            f"http://{credentials.host}:{exterior_camera_svc._ONVIF_PORT}/onvif/event_service",
            f"http://{credentials.host}:{exterior_camera_svc._ONVIF_PORT}/onvif/events_service",
        ]
        seen = set()
        last_error = None
        for event_url in candidates:
            if not event_url or event_url in seen:
                continue
            seen.add(event_url)
            try:
                def body_builder(operation):
                    import xml.etree.ElementTree as ET
                    ET.SubElement(operation, f"{{{camera_dvr_svc._WSN_NS}}}InitialTerminationTime").text = "PT10M"

                body = await self._post_event(
                    client, credentials=credentials, url=event_url,
                    operation="CreatePullPointSubscription", body_builder=body_builder,
                )
                addresses = [
                    str(node.text or "").strip()
                    for node in body.iter()
                    if exterior_camera_svc._xml_name(node) == "Address" and str(node.text or "").strip()
                ]
                return (
                    self._pinned_subscription_url(addresses[0], host=credentials.host)
                    if addresses else event_url
                )
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise exterior_camera_svc.ExteriorCameraUnavailable("Exterior camera event service was not found.")

    @classmethod
    def motion_states_from_body(cls, body):
        states = super().motion_states_from_body(body)
        if states:
            return states
        for notification in [
            node for node in body.iter()
            if exterior_camera_svc._xml_name(node) == "NotificationMessage"
        ]:
            topic = " ".join(
                str(node.text or "") for node in notification.iter()
                if exterior_camera_svc._xml_name(node) == "Topic"
            ).casefold()
            if not any(marker in topic for marker in _EVENT_TOPIC_MARKERS):
                continue
            for node in notification.iter():
                if exterior_camera_svc._xml_name(node) != "SimpleItem":
                    continue
                value = cls._parse_bool(node.attrib.get("Value"))
                if value is not None:
                    states.append(value)
                    break
        return states


_SECURITY_ANALYSIS_PROMPT = (
    "Reply in exactly this three-line format:\n"
    "PERSON: yes or no\n"
    "VEHICLE: yes or no\n"
    "DESCRIPTION: one plain sentence describing exactly what is visible\n"
    "VEHICLE means any moving or present road/off-road vehicle including car, "
    "truck, SUV, van, motorcycle, ATV, tractor, trailer, or similar vehicle. "
    "Base every line only on the pixels. If neither a person nor vehicle is "
    "visible, report both as no and describe the scene."
)


def _parse_iso(value: object) -> Optional[datetime]:
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


class OnvifCameraMonitor(legacy.CameraMonitor):
    """Keep legacy behavior as fallback; use camera-native motion when healthy."""

    def __init__(self, settings, exterior_camera, router, store, *, dvr):
        super().__init__(settings, exterior_camera, router, store)
        self.dvr = dvr
        self._onvif_motion_active = False

    async def run_forever(self) -> None:
        event_task = asyncio.create_task(self._run_onvif_events())
        try:
            while not self._stopped:
                try:
                    if self.dvr.events_healthy:
                        await self._onvif_tick()
                    else:
                        # Proven fallback for camera firmware/network event failures.
                        await super()._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("camera security monitoring tick failed")

                if self.dvr.events_healthy:
                    interval = (
                        self.settings.camera_motion_burst_interval_seconds
                        if self._burst_until is not None
                        else min(60, self.settings.camera_baseline_interval_seconds)
                    )
                else:
                    interval = (
                        self.settings.camera_motion_burst_interval_seconds
                        if self._burst_until is not None
                        else self.settings.camera_monitor_interval_seconds
                    )
                await asyncio.sleep(max(1, interval))
        finally:
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)

    async def _run_onvif_events(self) -> None:
        async for active in self.dvr.motion_states():
            if self._stopped:
                return
            now = time.monotonic()
            if active:
                starting = not self._onvif_motion_active and not (
                    self._burst_until is not None and now < self._burst_until
                )
                self._onvif_motion_active = True
                if starting:
                    self._current_burst_id = self._next_burst_id
                    self._next_burst_id += 1
                    await self._capture_onvif_opening_frame()
                self._burst_until = now + self.settings.camera_motion_burst_seconds
            else:
                self._onvif_motion_active = False
                # Keep a short tail after the camera says motion cleared so a
                # departing person/vehicle is still documented.
                if self._burst_until is not None:
                    self._burst_until = min(self._burst_until, now + 20)

    async def _capture_onvif_opening_frame(self) -> None:
        frame = await self.exterior_camera.capture_snapshot()
        if frame is None or self._current_burst_id is None:
            return
        filename = self._write_snapshot(frame.raw, "motion")
        event_id = self.store.add_camera_event(
            trigger="motion",
            snapshot_filename=filename,
            motion_score=None,
            burst_id=self._current_burst_id,
        )
        person, vehicle = await self._analyze_security_frame(event_id, frame)
        # One quick second look protects against the camera firing just before
        # a fast vehicle/person fully enters the frame. It is bounded to one
        # extra vision call and only when the opening image contained neither.
        if person is False and vehicle is False:
            await asyncio.sleep(2)
            follow = await self.exterior_camera.capture_snapshot()
            if follow is not None:
                filename = self._write_snapshot(follow.raw, "motion")
                follow_id = self.store.add_camera_event(
                    trigger="motion",
                    snapshot_filename=filename,
                    burst_id=self._current_burst_id,
                )
                await self._analyze_security_frame(follow_id, follow)

    async def _analyze_security_frame(self, event_id: int, frame) -> tuple[Optional[bool], Optional[bool]]:
        if not self.router.supports_vision():
            try:
                await self.router.ensure_capability(vision=True)
            except WorkerSwapError:
                log.warning("could not switch to a vision worker for an ONVIF motion event")
                return None, None
        try:
            raw_caption = await camera_svc.caption_frame(
                self.router, frame, _SECURITY_ANALYSIS_PROMPT
            )
        except Exception:
            log.warning("ONVIF motion-event captioning failed", exc_info=True)
            return None, None
        person, vehicle, description = legacy._parse_structured_caption(raw_caption)
        self.store.update_camera_event_caption(
            event_id,
            caption=description,
            person_detected=person,
            vehicle_detected=vehicle,
        )
        if person or vehicle:
            await self._notify_security(description)
            self.store.mark_camera_event_notified(event_id)
        return person, vehicle

    async def _notify_security(self, description: str) -> None:
        owner = self.store.get_owner()
        if not owner:
            return
        user = self.store.get_user_by_google_sub(owner["google_sub"])
        if not user:
            return
        try:
            await push_notifications.send_push_async(
                self.store,
                self.settings,
                user["id"],
                "X noticed something",
                description,
            )
        except Exception:
            log.warning("push notification failed", exc_info=True)

    async def _onvif_tick(self) -> None:
        frame = await self.exterior_camera.capture_snapshot()
        if frame is None:
            self._sweep_retention()
            return
        now = time.monotonic()
        is_baseline = (
            self._last_baseline_at is None
            or now - self._last_baseline_at >= self.settings.camera_baseline_interval_seconds
        )
        if is_baseline:
            self._last_baseline_at = now
            filename = self._write_snapshot(frame.raw, "interval")
            self.store.add_camera_event(trigger="interval", snapshot_filename=filename)

        in_burst = self._burst_until is not None and now < self._burst_until
        if in_burst and self._current_burst_id is not None:
            filename = self._write_snapshot(frame.raw, "motion")
            self.store.add_camera_event(
                trigger="motion",
                snapshot_filename=filename,
                burst_id=self._current_burst_id,
            )
        elif self._burst_until is not None:
            self._burst_until = None
            self._current_burst_id = None
        self._sweep_retention()


async def camera_event_history(store, args: dict, *, dvr) -> dict[str, Any]:
    result = await legacy.camera_event_history(store, args)
    try:
        result["dvr_status"] = await dvr.status()
        if args.get("include_recordings"):
            result["recordings"] = await dvr.list_segments(
                since=args.get("since"), until=args.get("until"), limit=40
            )
    except Exception as exc:
        result["dvr_status"] = {
            "ok": False,
            "recording": False,
            "last_error": str(exc),
        }
    return result


async def camera_motion_clip(store, settings, ffmpeg_path, args: dict, *, dvr) -> dict[str, Any]:
    since = _parse_iso(args.get("since"))
    until = _parse_iso(args.get("until"))
    if since or until:
        if since is None or until is None:
            return {"ok": False, "error": "Both since and until are required for DVR time-range playback."}
        try:
            path = await dvr.range_clip(since, until, cache_name=f"range-{int(since.timestamp())}-{int(until.timestamp())}")
            return {
                "ok": True,
                "clip_url": f"/dvr/api/clips/{path.name}",
                "started_at_local": since.astimezone().strftime("%Y-%m-%d %I:%M:%S %p %Z"),
                "ended_at_local": until.astimezone().strftime("%Y-%m-%d %I:%M:%S %p %Z"),
                "source": "continuous_dvr",
                "cached": True,
            }
        except Exception as exc:
            return {"ok": False, "error": f"Continuous DVR footage is unavailable: {exc}"}

    raw_event_id = args.get("event_id")
    if raw_event_id is not None:
        try:
            event = store.get_camera_event(int(raw_event_id))
        except (TypeError, ValueError):
            event = None
        burst_id = event.get("burst_id") if event else None
    else:
        burst_id = store.get_latest_motion_burst_id()

    if burst_id is not None:
        try:
            path = await dvr.event_clip(store, int(burst_id))
            events = store.list_camera_events_by_burst(int(burst_id))
            caption = next((row.get("caption") for row in events if row.get("caption")), None)
            return {
                "ok": True,
                "burst_id": int(burst_id),
                "clip_url": f"/dvr/api/clips/{path.name}",
                "frame_count": len(events),
                "started_at_local": legacy._local_time_str(events[0]["captured_at"]) if events else None,
                "ended_at_local": legacy._local_time_str(events[-1]["captured_at"]) if events else None,
                "caption": caption,
                "source": "continuous_dvr",
                "cached": True,
            }
        except Exception:
            # Preserve the existing chat behavior even if E: was unplugged or
            # the requested event predates continuous recording.
            pass

    return await legacy.camera_motion_clip(store, settings, ffmpeg_path, args)
