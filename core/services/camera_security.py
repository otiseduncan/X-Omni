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
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import camera as camera_svc
from . import camera_monitoring as legacy
from . import camera_dvr as camera_dvr_svc
from . import exterior_camera as exterior_camera_svc
from . import push_notifications
log = logging.getLogger("xomni.camera_security")

SECURITY_TOOL_SCHEMAS = {
    # Put media-rendering capabilities before the broad history reader in the
    # model catalog.  The model still owns intent and argument selection, but
    # a request to show media should encounter the exact rendering contracts
    # before the tempting collection read.
    "camera_footage": copy.deepcopy(
        legacy.CAMERA_MONITORING_TOOL_SCHEMAS["camera_motion_clip"]
    ),
    "camera_snapshot_analyze": copy.deepcopy(
        legacy.CAMERA_MONITORING_TOOL_SCHEMAS["camera_snapshot_analyze"]
    ),
    "camera_event_history": copy.deepcopy(
        legacy.CAMERA_MONITORING_TOOL_SCHEMAS["camera_event_history"]
    ),
}
SECURITY_TOOL_SCHEMAS["camera_event_history"]["description"] = (
    "Lists event metadata/thumbnails and returns /dvr. For 'show me the DVR', "
    "present that link. For playable footage/video/time ranges use camera_footage."
)
SECURITY_TOOL_SCHEMAS["camera_event_history"]["parameters"]["properties"][
    "include_recordings"
] = {
    "type": "boolean",
    "description": "Include bounded continuous-DVR segment metadata for the same time range.",
}
SECURITY_TOOL_SCHEMAS["camera_footage"]["description"] = (
    "Sole tool to show/play/watch camera footage, video, recordings, clips, or a clock "
    "time. Returns a playable continuous-DVR card or legacy fallback."
)
SECURITY_TOOL_SCHEMAS["camera_footage"]["parameters"]["properties"].update(
    {
        "since": {
            "type": "string",
            "description": "ISO start with local UTC offset (for EDT use -04:00, never Z).",
        },
        "until": {
            "type": "string",
            "description": "ISO end with the same explicit UTC offset.",
        },
    }
)

_DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
_EVENT_TOPIC_MARKERS = ("motion", "move", "human", "people", "person", "vehicle", "car")
_EVENT_STATE_NAME_MARKERS = ("motion", "move", "alarm", "active", "trigger", "detect")
_EVENT_STATE_NAMES = {"state", "status", "value", "level", "event"}
_EVENT_HEALTH_POLL_SECONDS = 2.0
_EVENT_CONSUMER_RESTART_SECONDS = 1.0
_SECOND_LOOK_DELAY_SECONDS = 2.0


def _security_marker_in(value: object) -> bool:
    """Match security topic/name words without treating ``CardReader`` as ``car``."""

    text = str(value or "")
    folded = text.casefold()
    if any(marker in folded for marker in _EVENT_TOPIC_MARKERS if marker != "car"):
        return True
    words = re.findall(
        r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|\d+",
        text.replace("_", " ").replace("-", " "),
    )
    return "car" in {word.casefold() for word in words}


def _state_name_is_specific(value: object) -> bool:
    name = str(value or "").strip().casefold()
    return any(marker in name for marker in _EVENT_STATE_NAME_MARKERS)


def _state_name_is_generic(value: object) -> bool:
    return str(value or "").strip().casefold() in _EVENT_STATE_NAMES


class XiongmaiDVR(camera_dvr_svc.CameraDVR):
    """DVR with Xiongmai-tolerant ONVIF event discovery and topic parsing."""

    @staticmethod
    def _parse_bool(value: object) -> Optional[bool]:
        text = str(value or "").strip().casefold()
        if text in {"trigger", "triggered", "start", "started", "detected"}:
            return True
        if text in {"clear", "cleared", "stop", "stopped", "ended", "idle"}:
            return False
        return camera_dvr_svc.CameraDVR._parse_bool(value)

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
                    ET.SubElement(
                        operation,
                        f"{{{camera_dvr_svc._EVENTS_NS}}}InitialTerminationTime",
                    ).text = "PT10M"

                body = await self._post_event(
                    client, credentials=credentials, url=event_url,
                    operation="CreatePullPointSubscription", body_builder=body_builder,
                )
                response = self._event_response(
                    body, "CreatePullPointSubscriptionResponse"
                )
                addresses = [
                    str(node.text or "").strip()
                    for node in response.iter()
                    if exterior_camera_svc._xml_name(node) == "Address" and str(node.text or "").strip()
                ]
                self._set_subscription_renewal(response, require_times=True)
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
        """Return one state per relevant notification, preserving camera order.

        Xiongmai messages commonly put source metadata such as ``Channel=1``
        before the actual ``State=false`` item.  Only explicitly state-like
        fields are eligible; arbitrary parseable values are never promoted to
        motion.  Parsing notification-by-notification also keeps a normal
        MotionAlarm clear from hiding a later vehicle/person trigger in the
        same PullMessages response.
        """

        states: list[bool] = []
        notifications = [
            node
            for node in body.iter()
            if exterior_camera_svc._xml_name(node) == "NotificationMessage"
        ]
        for notification in notifications:
            topic = " ".join(
                str(node.text or "")
                for node in notification.iter()
                if exterior_camera_svc._xml_name(node) == "Topic"
            )
            simple_items = [
                node
                for node in notification.iter()
                if exterior_camera_svc._xml_name(node) == "SimpleItem"
            ]
            security_named = any(
                _security_marker_in(node.attrib.get("Name")) for node in simple_items
            )
            if not _security_marker_in(topic) and not security_named:
                continue

            candidate: Optional[bool] = None
            # Prefer a motion/alarm-specific field over a generic State/Level
            # item, while preserving the order among equally specific fields.
            for predicate in (_state_name_is_specific, _state_name_is_generic):
                for node in simple_items:
                    if not predicate(node.attrib.get("Name")):
                        continue
                    candidate = cls._parse_bool(node.attrib.get("Value"))
                    if candidate is not None:
                        break
                if candidate is not None:
                    break

            if candidate is None:
                for node in notification.iter():
                    local_name = exterior_camera_svc._xml_name(node)
                    if not (
                        _state_name_is_specific(local_name)
                        or _state_name_is_generic(local_name)
                    ):
                        continue
                    candidate = cls._parse_bool(node.text)
                    if candidate is not None:
                        break
            if candidate is not None:
                states.append(candidate)
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


def _dvr_iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _preferred_burst_caption(events: list[dict[str, Any]]) -> Optional[str]:
    positive = [
        row
        for row in events
        if row.get("caption")
        and (bool(row.get("person_detected")) or bool(row.get("vehicle_detected")))
    ]
    if positive:
        return str(positive[-1]["caption"])
    return next((str(row["caption"]) for row in events if row.get("caption")), None)


def _parse_security_caption(
    text: str,
) -> tuple[Optional[bool], Optional[bool], str]:
    """Parse only the exact security response contract.

    The legacy parser intentionally tolerates ordinary prose, but substring
    matching is unsafe at the notification boundary (for example,
    ``PERSON: yes or no`` or ``PERSON: yesterday``).  A security decision is
    authoritative only when all three required lines occur exactly once.
    """

    decisions: dict[str, bool] = {}
    descriptions: list[str] = []
    invalid = False
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        decision = re.fullmatch(r"(PERSON|VEHICLE)\s*:\s*(yes|no)", line, re.IGNORECASE)
        if decision:
            name = decision.group(1).upper()
            if name in decisions:
                invalid = True
            decisions[name] = decision.group(2).casefold() == "yes"
            continue
        description = re.fullmatch(r"DESCRIPTION\s*:\s*(.+)", line, re.IGNORECASE)
        if description:
            value = description.group(1).strip()
            if descriptions or not value:
                invalid = True
            if value:
                descriptions.append(value)
            continue
        if line:
            invalid = True
    fallback = descriptions[0] if descriptions else str(text or "").strip()
    if invalid or set(decisions) != {"PERSON", "VEHICLE"} or len(descriptions) != 1:
        return None, None, fallback
    return decisions["PERSON"], decisions["VEHICLE"], descriptions[0]


def _motion_event_in_range(store, since: datetime, until: datetime) -> Optional[dict[str, Any]]:
    try:
        rows = store.list_camera_events(
            since=since.strftime("%Y-%m-%d %H:%M:%S"),
            until=until.strftime("%Y-%m-%d %H:%M:%S"),
            limit=500,
        )
    except Exception:
        log.warning("could not search stored motion events for historical fallback", exc_info=True)
        return None
    candidates = [
        row
        for row in rows
        if row.get("trigger") == "motion" and row.get("burst_id") is not None
    ]
    if not candidates:
        return None
    midpoint = since + (until - since) / 2

    def distance(row: dict[str, Any]) -> float:
        captured = _parse_iso(row.get("captured_at"))
        return abs((captured - midpoint).total_seconds()) if captured else float("inf")

    return min(candidates, key=distance)


def _decorate_legacy_clip(store, result: dict[str, Any], burst_id: Optional[int]) -> dict[str, Any]:
    if not result.get("ok") or burst_id is None:
        return result
    try:
        events = store.list_camera_events_by_burst(int(burst_id))
    except Exception:
        return result
    caption = _preferred_burst_caption(events)
    if caption:
        result = dict(result)
        result["caption"] = caption
    result.setdefault("source", "stored_frame_timelapse")
    return result


class OnvifCameraMonitor(legacy.CameraMonitor):
    """Keep legacy behavior as fallback; use camera-native motion when healthy."""

    def __init__(self, settings, exterior_camera, router, store, *, dvr):
        super().__init__(settings, exterior_camera, router, store)
        self.dvr = dvr
        self._onvif_motion_active = False
        self._monitor_wake = asyncio.Event()

    def stop(self) -> None:
        super().stop()
        self._monitor_wake.set()

    def _mark_dvr_events_unhealthy(self) -> None:
        """Fail closed across old and new CameraDVR implementations."""

        marker = getattr(self.dvr, "mark_events_unhealthy", None)
        if callable(marker):
            try:
                marker()
                return
            except Exception:
                log.warning("could not mark DVR event health through its public API", exc_info=True)
        for setter_name in ("set_events_healthy", "set_event_health"):
            setter = getattr(self.dvr, setter_name, None)
            if not callable(setter):
                continue
            try:
                setter(False)
                return
            except Exception:
                log.warning("could not clear DVR event health through its public API", exc_info=True)
        if hasattr(self.dvr, "_events_healthy"):
            # Compatibility for the feature branch's original CameraDVR.  The
            # guarded public methods above are preferred when available.
            self.dvr._events_healthy = False

    def _lose_onvif_authority(self) -> None:
        self._onvif_motion_active = False
        self._mark_dvr_events_unhealthy()
        self._monitor_wake.set()

    async def run_forever(self) -> None:
        event_task = asyncio.create_task(self._supervise_onvif_events())
        next_tick_at = 0.0
        last_health: Optional[bool] = None
        try:
            while not self._stopped:
                now = time.monotonic()
                healthy = bool(self.dvr.events_healthy)
                health_changed = last_health is not None and healthy != last_health
                if last_health is True and not healthy:
                    # A reconnect must not inherit an active latch from the
                    # dead subscription.  The current documentation burst may
                    # continue under frame-difference fallback.
                    self._onvif_motion_active = False

                if now >= next_tick_at or health_changed or self._monitor_wake.is_set():
                    self._monitor_wake.clear()
                    try:
                        if healthy:
                            await self._onvif_tick()
                        else:
                            await super()._tick()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("camera security monitoring tick failed")

                    healthy = bool(self.dvr.events_healthy)
                    if healthy:
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
                    next_tick_at = time.monotonic() + max(1, interval)
                    last_health = healthy

                wait_seconds = min(
                    _EVENT_HEALTH_POLL_SECONDS,
                    max(0.05, next_tick_at - time.monotonic()),
                )
                try:
                    await asyncio.wait_for(self._monitor_wake.wait(), timeout=wait_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)

    async def _supervise_onvif_events(self) -> None:
        while not self._stopped:
            try:
                await self._run_onvif_events()
                if not self._stopped:
                    log.warning("ONVIF event consumer ended unexpectedly; restarting")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ONVIF event consumer failed; restarting")
            if self._stopped:
                return
            self._lose_onvif_authority()
            await asyncio.sleep(_EVENT_CONSUMER_RESTART_SECONDS)

    async def _run_onvif_events(self) -> None:
        async for active in self.dvr.motion_states():
            if self._stopped:
                return
            now = time.monotonic()
            if active:
                continuing_burst = bool(
                    self._current_burst_id is not None
                    and self._burst_until is not None
                    and now < self._burst_until
                )
                starting = not continuing_burst
                self._onvif_motion_active = True
                if starting:
                    self._current_burst_id = self._next_burst_id
                    self._next_burst_id += 1
                self._burst_until = now + self.settings.camera_motion_burst_seconds
                self._monitor_wake.set()
                if starting:
                    await self._capture_onvif_opening_frame()
            else:
                self._onvif_motion_active = False
                if self._burst_until is not None:
                    self._burst_until = min(self._burst_until, now + 20)
                self._monitor_wake.set()

    async def _capture_onvif_opening_frame(self) -> None:
        for attempt in range(2):
            if self._current_burst_id is None:
                return
            try:
                frame = await self.exterior_camera.capture_snapshot()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("ONVIF motion snapshot capture failed", exc_info=True)
                frame = None
            if frame is not None:
                filename = self._write_snapshot(frame.raw, "motion")
                event_id = self.store.add_camera_event(
                    trigger="motion",
                    snapshot_filename=filename,
                    motion_score=None,
                    burst_id=self._current_burst_id,
                )
                person, vehicle = await self._analyze_security_frame(event_id, frame)
                if person is True or vehicle is True:
                    return
            if attempt == 0:
                await asyncio.sleep(_SECOND_LOOK_DELAY_SECONDS)

    async def _analyze_security_frame(self, event_id: int, frame) -> tuple[Optional[bool], Optional[bool]]:
        if not self.router.supports_vision():
            try:
                await self.router.ensure_capability(vision=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("could not switch to a vision worker for an ONVIF motion event")
                return None, None
        try:
            raw_caption = await camera_svc.caption_frame(
                self.router, frame, _SECURITY_ANALYSIS_PROMPT
            )
        except Exception:
            log.warning("ONVIF motion-event captioning failed", exc_info=True)
            return None, None
        person, vehicle, description = _parse_security_caption(raw_caption)
        self.store.update_camera_event_caption(
            event_id,
            caption=description,
            person_detected=person,
            vehicle_detected=vehicle,
        )
        if person or vehicle:
            delivered = await self._notify_security(description)
            if delivered > 0:
                self.store.mark_camera_event_notified(event_id)
        return person, vehicle

    async def _notify_security(self, description: str) -> int:
        owner = self.store.get_owner()
        if not owner:
            return 0
        user = self.store.get_user_by_google_sub(owner["google_sub"])
        if not user:
            return 0
        try:
            return int(await push_notifications.send_push_async(
                self.store,
                self.settings,
                user["id"],
                "X noticed something",
                description,
            ) or 0)
        except Exception:
            log.warning("push notification failed", exc_info=True)
            return 0

    async def _onvif_tick(self) -> None:
        frame = await self.exterior_camera.capture_snapshot()
        if frame is None:
            self._sweep_retention()
            return
        # Keep the legacy detector's comparison frame current while ONVIF is
        # authoritative.  If the subscription degrades, fallback compares a
        # normal monitoring interval instead of an hours-old pre-ONVIF frame.
        self._previous_raw = frame.raw
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
    history_args = dict(args)
    dvr_bounds: dict[str, Optional[str]] = {"since": None, "until": None}
    for key in ("since", "until"):
        parsed = _parse_iso(history_args.get(key))
        if parsed is not None:
            history_args[key] = parsed.strftime("%Y-%m-%d %H:%M:%S")
            dvr_bounds[key] = _dvr_iso(parsed)
        elif args.get(key):
            dvr_bounds[key] = str(args[key])
    result = await legacy.camera_event_history(store, history_args)
    result["dvr_url"] = "/dvr"
    try:
        result["dvr_status"] = await dvr.status()
        if args.get("include_recordings"):
            result["recordings"] = await dvr.list_segments(
                since=dvr_bounds["since"], until=dvr_bounds["until"], limit=40
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
        playback_since = since
        playback_until = until
        partial = False
        try:
            path = await dvr.range_clip(
                playback_since,
                playback_until,
                cache_name=(
                    f"range-{int(playback_since.timestamp())}-"
                    f"{int(playback_until.timestamp())}"
                ),
            )
        except Exception:
            # "Around 5:33" commonly expands to a window whose first minute
            # predates the first retained segment.  Return the truthful
            # overlapping completed portion instead of discarding available
            # footage.  range_clip still enforces continuity and source truth.
            try:
                overlapping = await dvr.list_segments(
                    since=_dvr_iso(since),
                    until=_dvr_iso(until),
                    limit=camera_dvr_svc.MAX_PLAYBACK_SEGMENTS,
                    complete_only=True,
                )
            except Exception:
                overlapping = []
            starts = [
                value
                for value in (_parse_iso(row.get("started_at")) for row in overlapping)
                if value is not None
            ]
            ends = [
                value
                for value in (_parse_iso(row.get("ended_at")) for row in overlapping)
                if value is not None
            ]
            if starts and ends:
                playback_since = max(since, min(starts))
                playback_until = min(until, max(ends))
            if playback_until <= playback_since or (
                playback_since == since and playback_until == until
            ):
                path = None
            else:
                try:
                    path = await dvr.range_clip(
                        playback_since,
                        playback_until,
                        cache_name=(
                            f"range-{int(playback_since.timestamp())}-"
                            f"{int(playback_until.timestamp())}"
                        ),
                    )
                    partial = True
                except Exception:
                    path = None
        if path is not None:
            return {
                "ok": True,
                "clip_url": f"/dvr/api/clips/{path.name}",
                "started_at_local": playback_since.astimezone().strftime("%Y-%m-%d %I:%M:%S %p %Z"),
                "ended_at_local": playback_until.astimezone().strftime("%Y-%m-%d %I:%M:%S %p %Z"),
                "requested_started_at_local": since.astimezone().strftime("%Y-%m-%d %I:%M:%S %p %Z"),
                "requested_ended_at_local": until.astimezone().strftime("%Y-%m-%d %I:%M:%S %p %Z"),
                "partial": partial,
                "source": "continuous_dvr",
                "cached": True,
            }
        historical = _motion_event_in_range(store, since, until)
        if historical is not None:
            fallback = await legacy.camera_motion_clip(
                store,
                settings,
                ffmpeg_path,
                {"event_id": historical["id"]},
            )
            fallback = _decorate_legacy_clip(
                store, fallback, int(historical["burst_id"])
            )
            if fallback.get("ok"):
                return fallback
        return {
            "ok": False,
            "error": "Continuous DVR footage is unavailable for that time range.",
        }

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
            caption = _preferred_burst_caption(events)
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
            pass

    fallback = await legacy.camera_motion_clip(store, settings, ffmpeg_path, args)
    fallback_burst_id = fallback.get("burst_id") if isinstance(fallback, dict) else None
    return _decorate_legacy_clip(store, fallback, fallback_burst_id or burst_id)
