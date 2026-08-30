"""Independent ONVIF PullPoint motion-event watcher for the Xiongmai exterior camera.

Extracted from the retired custom DVR (core/services/camera_dvr.py's
CameraDVR/XiongmaiDVR) as part of the MediaMTX migration. MediaMTX now owns
recording and playback entirely; this module owns exactly one thing --
discovering and maintaining an ONVIF PullPoint subscription and yielding
motion states from it -- with no coupling to any storage path. It never
touches a recording, never runs FFmpeg, and never depends on which media
server (if any) is recording the camera.

camera_security.OnvifCameraMonitor consumes OnvifMotionWatcher.motion_states()
as its primary motion trigger, exactly as it consumed CameraDVR's before.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import AsyncIterator, Callable, Optional
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import httpx

from . import exterior_camera as exterior_camera_svc

log = logging.getLogger("xomni.onvif_motion")

RESTART_DELAY_SECONDS = 5
SUBSCRIPTION_RECREATE_SECONDS = 4 * 60
ONVIF_MIN_PULL_CYCLE_SECONDS = 1.0

_DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
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
_BOOL_TRUE = {"true", "1", "on", "active", "yes", "trigger", "triggered", "start", "started", "detected"}
_BOOL_FALSE = {"false", "0", "off", "inactive", "no", "clear", "cleared", "stop", "stopped", "ended", "idle"}
_XIONGMAI_PULLPOINT_PATH_RE = re.compile(
    r"^/(?:event_service|events_service)(?:/[A-Za-z0-9._~-]{1,128}){1,4}/?$",
    re.IGNORECASE,
)
_EVENT_TOPIC_MARKERS = ("motion", "move", "human", "people", "person", "vehicle", "car")
_EVENT_STATE_NAME_MARKERS = ("motion", "move", "alarm", "active", "trigger", "detect")
_EVENT_STATE_NAMES = {"state", "status", "value", "level", "event"}


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
    return any(marker in str(value or "").strip().casefold() for marker in _EVENT_STATE_NAME_MARKERS)


def _state_name_is_generic(value: object) -> bool:
    return str(value or "").strip().casefold() in _EVENT_STATE_NAMES


def _parse_time(value: object) -> Optional[datetime]:
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


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class OnvifMotionWatcher:
    """Owns one Xiongmai-tolerant ONVIF PullPoint subscription and nothing else."""

    def __init__(self, exterior_camera):
        self.camera = exterior_camera
        self._stopped = False
        self._events_healthy = False
        self._subscription_renew_at = 0.0
        self._last_motion_at: Optional[str] = None

    @property
    def events_healthy(self) -> bool:
        return bool(self._events_healthy)

    def mark_events_unhealthy(self) -> None:
        self._events_healthy = False

    @property
    def last_motion_at(self) -> Optional[str]:
        return self._last_motion_at

    def stop(self) -> None:
        self._stopped = True

    # ---------- Xiongmai-tolerant boolean/topic parsing ----------

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
        """Return one state per relevant notification, preserving camera order.

        Xiongmai messages commonly put source metadata such as ``Channel=1``
        before the actual ``State=false`` item. Only explicitly state-like
        fields are eligible; arbitrary parseable values are never promoted to
        motion. Parsing notification-by-notification also keeps a normal
        MotionAlarm clear from hiding a later vehicle/person trigger in the
        same PullMessages response.
        """
        states: list[bool] = []
        notifications = [
            node for node in body.iter()
            if exterior_camera_svc._xml_name(node) == "NotificationMessage"
        ]
        for notification in notifications:
            topic = " ".join(
                str(node.text or "")
                for node in notification.iter()
                if exterior_camera_svc._xml_name(node) == "Topic"
            )
            simple_items = [
                node for node in notification.iter()
                if exterior_camera_svc._xml_name(node) == "SimpleItem"
            ]
            security_named = any(
                _security_marker_in(node.attrib.get("Name")) for node in simple_items
            )
            if not _security_marker_in(topic) and not security_named:
                continue

            candidate: Optional[bool] = None
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
                    if not (_state_name_is_specific(local_name) or _state_name_is_generic(local_name)):
                        continue
                    candidate = cls._parse_bool(node.text)
                    if candidate is not None:
                        break
            if candidate is not None:
                states.append(candidate)
        return states

    # ---------- subscription discovery/lifecycle ----------

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

    async def _discover_event_url(self, client, credentials) -> Optional[str]:
        def body_builder(operation):
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
            namespace=namespace, operation=operation,
            credentials=credentials, body_builder=body_builder,
        )
        action = _EVENT_ACTIONS[operation]
        try:
            async with client.stream(
                "POST", url,
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
        if any(exterior_camera_svc._xml_name(node) == "Fault" for node in body.iter()):
            raise exterior_camera_svc.ExteriorCameraUnavailable(
                "Exterior camera ONVIF event request was rejected."
            )
        expected_namespace = _WSN_NS if expected in {"RenewResponse", "UnsubscribeResponse"} else _EVENTS_NS
        responses = [child for child in body if child.tag == f"{{{expected_namespace}}}{expected}"]
        if len(responses) != 1:
            raise exterior_camera_svc.ExteriorCameraUnavailable(
                "Exterior camera ONVIF event response was invalid."
            )
        return responses[0]

    def _set_subscription_renewal(
        self, response: ET.Element, *, default_seconds: float = SUBSCRIPTION_RECREATE_SECONDS,
        require_times: bool = False,
    ) -> None:
        current_nodes = [n for n in response.iter() if exterior_camera_svc._xml_name(n) == "CurrentTime"]
        termination_nodes = [n for n in response.iter() if exterior_camera_svc._xml_name(n) == "TerminationTime"]
        renew_in = float(default_seconds)
        parsed_times = False
        if len(current_nodes) == 1 and len(termination_nodes) == 1:
            current = _parse_time(current_nodes[0].text)
            termination = _parse_time(termination_nodes[0].text)
            if current is not None and termination is not None:
                parsed_times = True
                lease_seconds = max(0.0, (termination - current).total_seconds())
                renew_in = max(0.0, min(lease_seconds * 0.5, max(0.0, lease_seconds - 30.0)))
        if require_times and not parsed_times:
            raise exterior_camera_svc.ExteriorCameraUnavailable(
                "Exterior camera ONVIF event lease was invalid."
            )
        self._subscription_renew_at = time.monotonic() + renew_in

    async def _renew_subscription(self, client, credentials, url: str) -> None:
        def body_builder(operation: ET.Element) -> None:
            ET.SubElement(operation, f"{{{_WSN_NS}}}TerminationTime").text = "PT10M"

        body = await self._post_event(
            client, credentials=credentials, url=url, operation="Renew",
            body_builder=body_builder, namespace=_WSN_NS, allow_empty_response=True,
        )
        if body.attrib.get("xomni-vendor-empty") == "1":
            self._subscription_renew_at = time.monotonic() + SUBSCRIPTION_RECREATE_SECONDS
            return
        response = self._event_response(body, "RenewResponse")
        self._set_subscription_renewal(response, require_times=True)

    async def _unsubscribe_subscription(self, client, credentials, url: str) -> None:
        body = await self._post_event(
            client, credentials=credentials, url=url, operation="Unsubscribe",
            namespace=_WSN_NS, allow_empty_response=True,
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
        discovered = await self._discover_event_url(client, credentials)
        candidates = [
            discovered,
            f"http://{credentials.host}:{exterior_camera_svc._ONVIF_PORT}/onvif/event_service",
            f"http://{credentials.host}:{exterior_camera_svc._ONVIF_PORT}/onvif/events_service",
        ]
        seen: set[str] = set()
        last_error: Optional[Exception] = None
        for event_url in candidates:
            if not event_url or event_url in seen:
                continue
            seen.add(event_url)
            try:
                def body_builder(operation):
                    ET.SubElement(operation, f"{{{_EVENTS_NS}}}InitialTerminationTime").text = "PT10M"

                body = await self._post_event(
                    client, credentials=credentials, url=event_url,
                    operation="CreatePullPointSubscription", body_builder=body_builder,
                )
                response = self._event_response(body, "CreatePullPointSubscriptionResponse")
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

    @staticmethod
    def _pull_cycle_delay(started_at: float, *, now: Optional[float] = None) -> float:
        elapsed = (time.monotonic() if now is None else now) - started_at
        return max(0.0, ONVIF_MIN_PULL_CYCLE_SECONDS - max(0.0, elapsed))

    async def motion_states(self) -> AsyncIterator[bool]:
        """Yield one bool per relevant ONVIF notification, forever, until stop().

        Never touches disk, a recording, or any media path -- purely a
        motion-event stream independent of whatever records the camera.
        """
        try:
            while not self._stopped:
                try:
                    credentials = self.camera._load_credentials()
                    timeout = httpx.Timeout(connect=5, read=30, write=5, pool=5)
                    async with httpx.AsyncClient(
                        transport=self.camera._onvif_transport,
                        timeout=timeout, follow_redirects=False, trust_env=False,
                    ) as client:
                        subscription_url = await self._create_subscription(client, credentials)
                        self._events_healthy = False
                        try:
                            while not self._stopped:
                                if time.monotonic() >= self._subscription_renew_at:
                                    await self._renew_subscription(client, credentials, subscription_url)

                                def pull_builder(operation: ET.Element) -> None:
                                    ET.SubElement(operation, f"{{{_EVENTS_NS}}}Timeout").text = "PT5S"
                                    ET.SubElement(operation, f"{{{_EVENTS_NS}}}MessageLimit").text = "32"

                                pull_started_at = time.monotonic()
                                body = await self._post_event(
                                    client, credentials=credentials, url=subscription_url,
                                    operation="PullMessages", body_builder=pull_builder,
                                )
                                response = self._event_response(body, "PullMessagesResponse")
                                self._set_subscription_renewal(response, require_times=True)
                                # Only a correctly shaped PullMessages response
                                # grants ONVIF authority over any fallback.
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
                            await self._unsubscribe_best_effort(client, credentials, subscription_url)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._events_healthy = False
                    log.warning("ONVIF motion-event subscription unavailable: %s", exc)
                    await asyncio.sleep(RESTART_DELAY_SECONDS)
        finally:
            self._events_healthy = False
