"""Owned, bounded connector for the local Xiongmai exterior camera.

The camera password is protected at rest with Windows DPAPI and is passed to
FFmpeg only through an in-memory concat manifest on stdin.  It is never placed
in a process command line, API response, audit record, or log message.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import time
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import httpx
import psutil

from . import camera as camera_svc

log = logging.getLogger("xomni.exterior_camera")

MAX_CREDENTIAL_FILE_BYTES = 64 * 1024
MAX_LABEL_CHARS = 80
MAX_USERNAME_CHARS = 128
MAX_PASSWORD_CHARS = 512
MAX_STDERR_BYTES = 64 * 1024
MAX_ONVIF_RESPONSE_BYTES = 512 * 1024
MAX_ONVIF_URI_CHARS = 4096
MAX_ONVIF_XML_NODES = 4096
MAX_ONVIF_XML_DEPTH = 32
MAX_ONVIF_XML_TEXT_CHARS = 256 * 1024
STREAM_READ_BYTES = 64 * 1024
STREAM_IDLE_TIMEOUT_SECONDS = 20.0
STREAM_START_TIMEOUT_SECONDS = 15.0
MAX_ACTIVE_STREAM_SECONDS = 8 * 60 * 60
PROCESS_STOP_TIMEOUT_SECONDS = 3.0

_PROTOCOL_WHITELIST = "file,pipe,tcp,rtsp"
_BOUNDARY = "xomni"
_DPAPI_ENTROPY = b"X Omni exterior camera credentials v1"
_ONVIF_PORT = 8899
_RTSP_PORT = 554
_SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
_MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
_SCHEMA_NS = "http://www.onvif.org/ver10/schema"
_WSSE_NS = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
_WSU_NS = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-utility-1.0.xsd"
)
_PASSWORD_DIGEST_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
_NONCE_ENCODING_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)
_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


async def _finish_despite_cancellation(awaitable) -> tuple[Any, bool]:
    """Finish an exact ownership cleanup before propagating cancellation."""

    task = asyncio.create_task(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def _await_cleanup(awaitable) -> Any:
    result, cancelled = await _finish_despite_cancellation(awaitable)
    if cancelled:
        raise asyncio.CancelledError
    return result


class ExteriorCameraError(RuntimeError):
    """Base class for safe, operator-facing connector errors."""


class ExteriorCameraNotConfigured(ExteriorCameraError):
    pass


class ExteriorCameraAuthError(ExteriorCameraError):
    pass


class ExteriorCameraUnavailable(ExteriorCameraError):
    pass


class ExteriorCameraConflict(ExteriorCameraError):
    pass


class ExteriorCameraSessionNotFound(ExteriorCameraError):
    pass


class ExteriorCameraFrameUnavailable(ExteriorCameraError):
    pass


@dataclass(frozen=True)
class _Credentials:
    label: str
    host: str
    username: str
    password: str = field(repr=False)
    verified_at: str = ""


@dataclass(frozen=True)
class _OnvifProfile:
    token: str = field(repr=False)
    name: str
    encoding: str
    width: int
    height: int
    ordinal: int


@dataclass
class _OwnedProcess:
    process: Any
    pid: int
    started_at: float
    executable: Path
    argv: tuple[str, ...]
    injected: bool = False
    stderr_task: asyncio.Task[bytes] | None = None


@dataclass
class _Session:
    session_id: str
    conversation_id: int
    owner_id: str
    label: str
    created_at: str
    expires_at: str
    expires_monotonic: float
    active_started_monotonic: float | None = None
    process: _OwnedProcess | None = None
    current_frame: bytes | None = field(default=None, repr=False)
    watchdog_task: asyncio.Task[None] | None = field(default=None, repr=False)


class _MJPEGFrameParser:
    """Extract bounded JPEG frames from FFmpeg's multipart byte stream."""

    def __init__(self, limit: int = camera_svc.MAX_CAMERA_FRAME_BYTES):
        self.limit = int(limit)
        self.buffer = bytearray()
        self.in_frame = False

    def feed(self, chunk: bytes) -> list[bytes]:
        if not chunk:
            return []
        self.buffer.extend(chunk)
        frames: list[bytes] = []
        while True:
            if not self.in_frame:
                start = self.buffer.find(b"\xff\xd8")
                if start < 0:
                    # Retain one byte so a split FF D8 marker is recognized.
                    if len(self.buffer) > 1:
                        del self.buffer[:-1]
                    return frames
                if start:
                    del self.buffer[:start]
                self.in_frame = True

            end = self.buffer.find(b"\xff\xd9", 2)
            if end < 0:
                if len(self.buffer) > self.limit:
                    self.buffer.clear()
                    self.in_frame = False
                    raise ExteriorCameraUnavailable(
                        "Exterior camera MJPEG frame exceeded the size limit."
                    )
                return frames
            end += 2
            frames.append(bytes(self.buffer[:end]))
            del self.buffer[:end]
            self.in_frame = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_text(value: object, *, field_name: str, max_chars: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required.")
    if len(text) > max_chars:
        raise ValueError(f"{field_name} exceeds the {max_chars}-character limit.")
    if any(unicodedata.category(char).startswith("C") for char in text):
        raise ValueError(f"{field_name} contains unsupported control characters.")
    return text


def _private_ipv4(value: object) -> str:
    raw = str(value or "").strip()
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise ValueError("Camera host must be a literal private IPv4 address.") from exc
    if not isinstance(address, ipaddress.IPv4Address) or not any(
        address in network for network in _RFC1918
    ):
        raise ValueError("Camera host must be a literal RFC1918 private IPv4 address.")
    return str(address)


def _password(value: object) -> str:
    text = str(value or "")
    if not text:
        raise ValueError("Camera password is required.")
    if len(text) > MAX_PASSWORD_CHARS:
        raise ValueError(
            f"Camera password exceeds the {MAX_PASSWORD_CHARS}-character limit."
        )
    if any(unicodedata.category(char).startswith("C") for char in text):
        raise ValueError("Camera password contains unsupported control characters.")
    return text


def _xml_name(element: ET.Element) -> str:
    return str(element.tag).rsplit("}", 1)[-1]


def _direct_children(element: ET.Element, local_name: str) -> list[ET.Element]:
    return [child for child in element if _xml_name(child) == local_name]


def _unique_child(element: ET.Element, local_name: str) -> ET.Element:
    matches = _direct_children(element, local_name)
    if len(matches) != 1:
        raise ExteriorCameraUnavailable("Exterior camera ONVIF response was invalid.")
    return matches[0]


def _unique_descendant(element: ET.Element, local_name: str) -> ET.Element:
    matches = [item for item in element.iter() if _xml_name(item) == local_name]
    if len(matches) != 1:
        raise ExteriorCameraUnavailable("Exterior camera ONVIF response was invalid.")
    return matches[0]


def _bounded_xml_text(
    element: ET.Element,
    *,
    field_name: str,
    required: bool = True,
    max_chars: int = 256,
) -> str:
    text = str(element.text or "").strip()
    if required and not text:
        raise ExteriorCameraUnavailable("Exterior camera ONVIF response was invalid.")
    if len(text) > max_chars or any(
        unicodedata.category(char).startswith("C") for char in text
    ):
        raise ExteriorCameraUnavailable("Exterior camera ONVIF response was invalid.")
    return text


def _parse_onvif_xml(raw: bytes) -> ET.Element:
    if not raw or len(raw) > MAX_ONVIF_RESPONSE_BYTES:
        raise ExteriorCameraUnavailable("Exterior camera ONVIF response was invalid.")
    if (
        b"\x00" in raw
        or raw.startswith((b"\xff\xfe", b"\xfe\xff"))
        or raw.startswith((b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00"))
    ):
        raise ExteriorCameraUnavailable("Exterior camera ONVIF response was invalid.")
    declaration = re.match(
        br"\s*<\?xml[^>]*\bencoding\s*=\s*['\"]([^'\"]+)['\"]",
        raw[:256],
        flags=re.IGNORECASE,
    )
    if declaration and declaration.group(1).lower() not in {b"utf-8", b"utf8"}:
        raise ExteriorCameraUnavailable("Exterior camera ONVIF response was invalid.")
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExteriorCameraUnavailable(
            "Exterior camera ONVIF response was invalid."
        ) from exc
    lowered = raw.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ExteriorCameraUnavailable("Exterior camera ONVIF response was invalid.")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ExteriorCameraUnavailable(
            "Exterior camera ONVIF response was invalid."
        ) from exc
    if _xml_name(root) != "Envelope":
        raise ExteriorCameraUnavailable("Exterior camera ONVIF response was invalid.")
    node_count = 0
    text_chars = 0
    stack = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        node_count += 1
        text_chars += len(node.text or "") + len(node.tail or "")
        text_chars += sum(len(str(key)) + len(str(value)) for key, value in node.attrib.items())
        if (
            node_count > MAX_ONVIF_XML_NODES
            or depth > MAX_ONVIF_XML_DEPTH
            or text_chars > MAX_ONVIF_XML_TEXT_CHARS
        ):
            raise ExteriorCameraUnavailable("Exterior camera ONVIF response was invalid.")
        stack.extend((child, depth + 1) for child in node)
    body = _unique_child(root, "Body")
    if _direct_children(body, "Fault"):
        fault_text = " ".join(
            str(item.text or "")
            for item in body.iter()
            if _xml_name(item) in {"Text", "Value", "Subcode"}
        ).lower()
        if "notauthorized" in fault_text or "unauthorized" in fault_text:
            raise ExteriorCameraAuthError("Exterior camera credentials were rejected.")
        raise ExteriorCameraUnavailable("Exterior camera ONVIF request was rejected.")
    return body


def _rtsp_stream_uri(value: str, *, host: str) -> str:
    uri = str(value or "")
    if (
        not uri
        or uri != uri.strip()
        or len(uri) > MAX_ONVIF_URI_CHARS
        or not uri.isascii()
        or "'" in uri
        or "\\" in uri
        or any(unicodedata.category(char).startswith("C") for char in uri)
    ):
        raise ExteriorCameraUnavailable("Exterior camera ONVIF stream URI was invalid.")
    if re.search(r"%(?![0-9A-Fa-f]{2})", uri):
        raise ExteriorCameraUnavailable("Exterior camera ONVIF stream URI was invalid.")
    try:
        parsed = urlsplit(uri)
        _private_ipv4(parsed.hostname)
        port = parsed.port
    except (ValueError, TypeError) as exc:
        raise ExteriorCameraUnavailable(
            "Exterior camera ONVIF stream URI was invalid."
        ) from exc
    if (
        parsed.scheme != "rtsp"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port != _RTSP_PORT
        or parsed.netloc != f"{parsed.hostname}:{_RTSP_PORT}"
        or parsed.query != "real_stream"
        or not re.fullmatch(
            r"/user=[^\s'\\/]+_password=[^\s'\\/]+_channel=0_"
            r"stream=[012]&onvif=0\.sdp",
            parsed.path,
        )
    ):
        raise ExteriorCameraUnavailable("Exterior camera ONVIF stream URI was invalid.")
    # The authenticated camera can advertise a stale alias in the authority.
    # Preserve its opaque credential/token path exactly, but keep every network
    # connection pinned to the literal host the Owner explicitly configured.
    return f"rtsp://{host}:{_RTSP_PORT}{parsed.path}?{parsed.query}"


def _soap_envelope(
    *,
    namespace: str,
    operation: str,
    credentials: _Credentials,
    body_builder: Callable[[ET.Element], None] | None = None,
) -> bytes:
    created_at = _utc_now()
    created = created_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    expires = (created_at + timedelta(seconds=60)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    nonce = secrets.token_bytes(20)
    digest = base64.b64encode(
        hashlib.sha1(
            nonce + created.encode("utf-8") + credentials.password.encode("utf-8")
        ).digest()
    ).decode("ascii")

    envelope = ET.Element(f"{{{_SOAP_NS}}}Envelope")
    header = ET.SubElement(envelope, f"{{{_SOAP_NS}}}Header")
    security = ET.SubElement(
        header,
        f"{{{_WSSE_NS}}}Security",
        {f"{{{_SOAP_NS}}}mustUnderstand": "1"},
    )
    timestamp = ET.SubElement(
        security,
        f"{{{_WSU_NS}}}Timestamp",
        {f"{{{_WSU_NS}}}Id": "Timestamp-1"},
    )
    ET.SubElement(timestamp, f"{{{_WSU_NS}}}Created").text = created
    ET.SubElement(timestamp, f"{{{_WSU_NS}}}Expires").text = expires
    token = ET.SubElement(security, f"{{{_WSSE_NS}}}UsernameToken")
    ET.SubElement(token, f"{{{_WSSE_NS}}}Username").text = credentials.username
    ET.SubElement(
        token,
        f"{{{_WSSE_NS}}}Password",
        {"Type": _PASSWORD_DIGEST_TYPE},
    ).text = digest
    ET.SubElement(
        token,
        f"{{{_WSSE_NS}}}Nonce",
        {"EncodingType": _NONCE_ENCODING_TYPE},
    ).text = base64.b64encode(nonce).decode("ascii")
    ET.SubElement(token, f"{{{_WSU_NS}}}Created").text = created
    body = ET.SubElement(envelope, f"{{{_SOAP_NS}}}Body")
    operation_node = ET.SubElement(body, f"{{{namespace}}}{operation}")
    if body_builder is not None:
        body_builder(operation_node)
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob(raw: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(raw)
    return (
        _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))),
        buffer,
    )


def _dpapi_transform(raw: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise ExteriorCameraUnavailable(
            "Exterior camera credentials require Windows DPAPI."
        )
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_wchar_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = ctypes.c_int
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    input_blob, input_buffer = _blob(raw)
    entropy_blob, entropy_buffer = _blob(_DPAPI_ENTROPY)
    output_blob = _DataBlob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "X Omni exterior camera",
            ctypes.byref(entropy_blob),
            None,
            None,
            flags,
            ctypes.byref(output_blob),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            flags,
            ctypes.byref(output_blob),
        )
    # Keep the backing buffers alive until CryptProtectData returns.
    del input_buffer, entropy_buffer
    if not ok:
        raise ExteriorCameraUnavailable(
            "Exterior camera credentials could not be opened with Windows DPAPI."
        )
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _dpapi_protect(raw: bytes) -> bytes:
    return _dpapi_transform(raw, protect=True)


def _dpapi_unprotect(raw: bytes) -> bytes:
    return _dpapi_transform(raw, protect=False)


class ExteriorCameraService:
    """Manage one Owner-bound, time-limited FFmpeg MJPEG session."""

    def __init__(
        self,
        root: Path,
        *,
        credential_path: Path | None = None,
        ffmpeg_path: Path | str | None = None,
        protect: Callable[[bytes], bytes] | None = None,
        unprotect: Callable[[bytes], bytes] | None = None,
        session_ttl_seconds: float = 300.0,
        probe_timeout_seconds: float = 15.0,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        process_factory: Callable[..., Awaitable[Any]] | None = None,
        onvif_transport: httpx.AsyncBaseTransport | None = None,
        onvif_timeout_seconds: float = 5.0,
    ):
        self.root = Path(root).resolve()
        self.credential_path = (
            Path(credential_path).resolve()
            if credential_path is not None
            else self.root / "data" / "credentials" / "exterior-camera.json"
        )
        discovered = str(ffmpeg_path or shutil.which("ffmpeg") or "").strip()
        self.ffmpeg_path = Path(discovered).resolve() if discovered else None
        self._protect = protect or _dpapi_protect
        self._unprotect = unprotect or _dpapi_unprotect
        self.session_ttl_seconds = max(5.0, float(session_ttl_seconds))
        self.probe_timeout_seconds = max(1.0, float(probe_timeout_seconds))
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._injected_process_factory = process_factory is not None
        self._onvif_transport = onvif_transport
        self.onvif_timeout_seconds = min(
            10.0, max(1.0, float(onvif_timeout_seconds))
        )
        self._lock = asyncio.Lock()
        self._session: _Session | None = None

    # ---------- configuration and safe public projection ----------

    def _read_record(self) -> dict[str, Any] | None:
        if not self.credential_path.is_file():
            return None
        try:
            with self.credential_path.open("rb") as stream:
                raw = stream.read(MAX_CREDENTIAL_FILE_BYTES + 1)
            if len(raw) > MAX_CREDENTIAL_FILE_BYTES:
                raise ExteriorCameraUnavailable(
                    "Exterior camera credential store is invalid."
                )
            record = json.loads(raw.decode("utf-8", errors="strict"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExteriorCameraUnavailable(
                "Exterior camera credential store is invalid."
            ) from exc
        if not isinstance(record, dict) or record.get("version") != 1:
            raise ExteriorCameraUnavailable("Exterior camera credential store is invalid.")
        try:
            label = _safe_text(
                record.get("label"), field_name="Camera label", max_chars=MAX_LABEL_CHARS
            )
            host = _private_ipv4(record.get("host"))
            username = _safe_text(
                record.get("username"),
                field_name="Camera username",
                max_chars=MAX_USERNAME_CHARS,
            )
            ciphertext = str(record.get("password_dpapi") or "").strip()
            if not ciphertext:
                raise ValueError("missing protected password")
            base64.b64decode(ciphertext, validate=True)
        except (ValueError, TypeError) as exc:
            raise ExteriorCameraUnavailable(
                "Exterior camera credential store is invalid."
            ) from exc
        return {
            "label": label,
            "host": host,
            "username": username,
            "password_dpapi": ciphertext,
            "verified_at": str(record.get("verified_at") or ""),
            "updated_at": str(record.get("updated_at") or ""),
        }

    def _load_credentials(self) -> _Credentials:
        record = self._read_record()
        if record is None:
            raise ExteriorCameraNotConfigured("Exterior camera is not configured.")
        try:
            ciphertext = base64.b64decode(record["password_dpapi"], validate=True)
            plaintext = self._unprotect(ciphertext).decode("utf-8", errors="strict")
            password = _password(plaintext)
        except ExteriorCameraError:
            raise
        except (ValueError, UnicodeError, OSError) as exc:
            raise ExteriorCameraUnavailable(
                "Exterior camera credentials could not be opened with Windows DPAPI."
            ) from exc
        finally:
            if "plaintext" in locals():
                plaintext = ""  # best-effort lifetime reduction; Python strings are immutable
        return _Credentials(
            label=record["label"],
            host=record["host"],
            username=record["username"],
            password=password,
            verified_at=record["verified_at"],
        )

    def _write_record(self, credentials: _Credentials) -> None:
        protected = self._protect(credentials.password.encode("utf-8"))
        record = {
            "version": 1,
            "label": credentials.label,
            "host": credentials.host,
            "username": credentials.username,
            "password_dpapi": base64.b64encode(protected).decode("ascii"),
            "verified_at": credentials.verified_at,
            "updated_at": _utc_now().isoformat(),
        }
        self.credential_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.credential_path.with_name(
            f".{self.credential_path.name}.{secrets.token_hex(8)}.tmp"
        )
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        try:
            with temp_path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.credential_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def status(self) -> dict[str, Any]:
        runtime_available = bool(self.ffmpeg_path and self.ffmpeg_path.is_file())
        try:
            record = self._read_record()
        except ExteriorCameraUnavailable:
            return {
                "ok": False,
                "configured": False,
                "status": "credential_store_invalid",
                "runtime_available": runtime_available,
                "streaming": False,
                "message": "Exterior camera credential store is invalid.",
            }
        session = self._session
        active = bool(
            session
            and session.expires_monotonic > self._clock()
            and session.process is not None
            and session.process.process.returncode is None
        )
        if record is None:
            return {
                "ok": True,
                "configured": False,
                "status": "not_configured",
                "runtime_available": runtime_available,
                "streaming": False,
                "message": "Exterior camera credentials have not been configured.",
            }
        return {
            "ok": True,
            "configured": True,
            "status": "streaming" if active else "configured",
            "label": record["label"],
            "host": record["host"],
            "username": record["username"],
            "password_stored": True,
            "runtime_available": runtime_available,
            "streaming": active,
            "verified_at": record["verified_at"] or None,
            "message": (
                "Exterior camera stream is active."
                if active
                else "Exterior camera is configured; no live stream is running."
            ),
        }

    def source_metadata(self) -> dict[str, str]:
        record = self._read_record()
        if record is None:
            raise ExteriorCameraNotConfigured("Exterior camera is not configured.")
        return {
            "source": "exterior_camera_still",
            "camera_source_id": "exterior",
            "camera_label": record["label"],
        }

    async def capture_snapshot(self) -> Optional[camera_svc.CameraFrame]:
        """Public, lock-guarded single-frame capture for the background
        monitoring loop. Returns None (never raises) when the camera isn't
        configured, an Owner-initiated live session is active, or the probe
        itself fails -- a monitoring tick must never crash on a transient
        camera hiccup, and it must never contend with an Owner actively
        viewing the stream."""
        async with self._lock:
            await self._expire_locked()
            if self._session is not None:
                return None
            try:
                credentials = self._load_credentials()
                return await self._probe_frame(credentials)
            except (ExteriorCameraError, OSError, httpx.HTTPError) as exc:
                log.warning(
                    "Background snapshot capture failed (%s): %s",
                    type(exc).__name__, exc,
                )
                return None

    async def configure(
        self, *, label: object, host: object, username: object, password: object
    ) -> dict[str, Any]:
        validated = _Credentials(
            label=_safe_text(
                label, field_name="Camera label", max_chars=MAX_LABEL_CHARS
            ),
            host=_private_ipv4(host),
            username=_safe_text(
                username,
                field_name="Camera username",
                max_chars=MAX_USERNAME_CHARS,
            ),
            password=_password(password),
        )
        async with self._lock:
            await self._expire_locked()
            if self._session is not None:
                raise ExteriorCameraConflict(
                    "Stop the active exterior camera session before changing its configuration."
                )
            frame = await self._probe_frame(validated)
            verified = _Credentials(
                label=validated.label,
                host=validated.host,
                username=validated.username,
                password=validated.password,
                verified_at=_utc_now().isoformat(),
            )
            try:
                _, write_cancelled = await _finish_despite_cancellation(
                    asyncio.to_thread(self._write_record, verified)
                )
            except asyncio.CancelledError:
                raise
            except ExteriorCameraError:
                raise
            except (OSError, ValueError, UnicodeError) as exc:
                raise ExteriorCameraUnavailable(
                    "Exterior camera credentials could not be saved."
                ) from exc
            if write_cancelled:
                raise asyncio.CancelledError
        return {
            **self.status(),
            "verified": True,
            "probe": {
                "mime": frame.mime,
                "bytes": frame.byte_count,
                "width": frame.width,
                "height": frame.height,
            },
        }

    # ---------- ffmpeg contract ----------

    async def _post_onvif(
        self,
        client: httpx.AsyncClient,
        *,
        credentials: _Credentials,
        operation: str,
        body_builder: Callable[[ET.Element], None] | None = None,
    ) -> ET.Element:
        payload = _soap_envelope(
            namespace=_MEDIA_NS,
            operation=operation,
            credentials=credentials,
            body_builder=body_builder,
        )
        url = (
            f"http://{credentials.host}:{_ONVIF_PORT}/onvif/media_service"
        )
        action = f"{_MEDIA_NS}/{operation}"
        try:
            async with client.stream(
                "POST",
                url,
                headers={
                    "Content-Type": (
                        'application/soap+xml; charset=utf-8; action="'
                        f'{action}"'
                    ),
                    "Accept": "application/soap+xml, text/xml",
                },
                content=payload,
            ) as response:
                if response.status_code in {401, 403}:
                    raise ExteriorCameraAuthError(
                        "Exterior camera credentials were rejected."
                    )
                if response.status_code < 200 or response.status_code >= 300:
                    raise ExteriorCameraUnavailable(
                        "Exterior camera ONVIF request failed."
                    )
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > MAX_ONVIF_RESPONSE_BYTES:
                            raise ExteriorCameraUnavailable(
                                "Exterior camera ONVIF response was invalid."
                            )
                    except ValueError as exc:
                        raise ExteriorCameraUnavailable(
                            "Exterior camera ONVIF response was invalid."
                        ) from exc
                media_type = response.headers.get("content-type", "").split(
                    ";", 1
                )[0].strip().lower()
                if media_type not in {
                    "application/soap+xml",
                    "application/xml",
                    "text/xml",
                }:
                    raise ExteriorCameraUnavailable(
                        "Exterior camera ONVIF response was invalid."
                    )
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_ONVIF_RESPONSE_BYTES:
                        raise ExteriorCameraUnavailable(
                            "Exterior camera ONVIF response was invalid."
                        )
        except ExteriorCameraError:
            raise
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise ExteriorCameraUnavailable(
                "Exterior camera ONVIF service was unavailable."
            ) from exc
        finally:
            payload = b""
        return _parse_onvif_xml(bytes(raw))

    @staticmethod
    def _profiles_from_response(body: ET.Element) -> list[_OnvifProfile]:
        response = _unique_child(body, "GetProfilesResponse")
        profile_nodes = _direct_children(response, "Profiles")
        if not profile_nodes or len(profile_nodes) > 16:
            raise ExteriorCameraUnavailable(
                "Exterior camera did not advertise a usable ONVIF profile."
            )
        profiles: list[_OnvifProfile] = []
        tokens: set[str] = set()
        for ordinal, node in enumerate(profile_nodes):
            token = str(node.attrib.get("token") or "").strip()
            if (
                not token
                or len(token) > 256
                or any(unicodedata.category(char).startswith("C") for char in token)
                or token in tokens
            ):
                raise ExteriorCameraUnavailable(
                    "Exterior camera ONVIF profile was invalid."
                )
            tokens.add(token)
            name = _bounded_xml_text(
                _unique_child(node, "Name"), field_name="profile name"
            )
            encoders = _direct_children(node, "VideoEncoderConfiguration")
            if len(encoders) != 1:
                raise ExteriorCameraUnavailable(
                    "Exterior camera ONVIF profile was invalid."
                )
            encoder = encoders[0]
            encoding = _bounded_xml_text(
                _unique_descendant(encoder, "Encoding"),
                field_name="profile encoding",
                max_chars=32,
            ).upper()
            resolution = _unique_descendant(encoder, "Resolution")
            try:
                width = int(
                    _bounded_xml_text(
                        _unique_child(resolution, "Width"),
                        field_name="profile width",
                        max_chars=8,
                    )
                )
                height = int(
                    _bounded_xml_text(
                        _unique_child(resolution, "Height"),
                        field_name="profile height",
                        max_chars=8,
                    )
                )
            except ValueError as exc:
                raise ExteriorCameraUnavailable(
                    "Exterior camera ONVIF profile was invalid."
                ) from exc
            if not (1 <= width <= 16384 and 1 <= height <= 16384):
                raise ExteriorCameraUnavailable(
                    "Exterior camera ONVIF profile was invalid."
                )
            profiles.append(
                _OnvifProfile(
                    token=token,
                    name=name,
                    encoding=encoding,
                    width=width,
                    height=height,
                    ordinal=ordinal,
                )
            )
        return profiles

    @staticmethod
    def _select_profile(profiles: list[_OnvifProfile]) -> _OnvifProfile:
        h264 = [
            profile
            for profile in profiles
            if profile.encoding.replace(".", "") in {"H264", "AVC"}
        ]
        if h264:
            return min(
                h264,
                key=lambda profile: (
                    0
                    if any(
                        marker in f"{profile.name} {profile.token}".lower()
                        for marker in ("sub", "secondary", "extra")
                    )
                    else 1,
                    profile.width * profile.height,
                    profile.ordinal,
                ),
            )
        h265 = [
            profile
            for profile in profiles
            if profile.encoding.replace(".", "") in {"H265", "HEVC"}
        ]
        if not h265:
            raise ExteriorCameraUnavailable(
                "Exterior camera did not advertise an H.264 or H.265 video profile."
            )
        return min(
            h265,
            key=lambda profile: (
                0
                if "main" in f"{profile.name} {profile.token}".lower()
                else 1,
                profile.ordinal,
            ),
        )

    @staticmethod
    def _stream_uri_from_response(body: ET.Element, *, host: str) -> str:
        response = _unique_child(body, "GetStreamUriResponse")
        media_uri = _unique_child(response, "MediaUri")
        uri = str(_unique_child(media_uri, "Uri").text or "")
        for field_name in ("InvalidAfterConnect", "InvalidAfterReboot"):
            values = _direct_children(media_uri, field_name)
            if len(values) > 1:
                raise ExteriorCameraUnavailable(
                    "Exterior camera ONVIF stream URI was invalid."
                )
            if values and str(values[0].text or "").strip().lower() not in {
                "true",
                "false",
                "0",
                "1",
            }:
                raise ExteriorCameraUnavailable(
                    "Exterior camera ONVIF stream URI was invalid."
                )
        timeouts = _direct_children(media_uri, "Timeout")
        if len(timeouts) > 1 or (
            timeouts
            and (
                not str(timeouts[0].text or "").strip()
                or len(str(timeouts[0].text or "").strip()) > 64
            )
        ):
            raise ExteriorCameraUnavailable(
                "Exterior camera ONVIF stream URI was invalid."
            )
        return _rtsp_stream_uri(uri, host=host)

    async def _resolve_stream_uri(self, credentials: _Credentials) -> str:
        timeout = httpx.Timeout(self.onvif_timeout_seconds)
        async with httpx.AsyncClient(
            transport=self._onvif_transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            profiles_body = await self._post_onvif(
                client, credentials=credentials, operation="GetProfiles"
            )
            profile = self._select_profile(
                self._profiles_from_response(profiles_body)
            )

            def stream_setup(operation: ET.Element) -> None:
                setup = ET.SubElement(operation, f"{{{_MEDIA_NS}}}StreamSetup")
                ET.SubElement(setup, f"{{{_SCHEMA_NS}}}Stream").text = "RTP-Unicast"
                transport = ET.SubElement(setup, f"{{{_SCHEMA_NS}}}Transport")
                ET.SubElement(transport, f"{{{_SCHEMA_NS}}}Protocol").text = "RTSP"
                ET.SubElement(
                    operation, f"{{{_MEDIA_NS}}}ProfileToken"
                ).text = profile.token

            uri_body = await self._post_onvif(
                client,
                credentials=credentials,
                operation="GetStreamUri",
                body_builder=stream_setup,
            )
            return self._stream_uri_from_response(
                uri_body, host=credentials.host
            )

    def _require_ffmpeg(self) -> Path:
        if not self.ffmpeg_path or not self.ffmpeg_path.is_file():
            raise ExteriorCameraUnavailable(
                "FFmpeg is not installed or is not available to X Omni."
            )
        return self.ffmpeg_path

    @staticmethod
    def _concat_manifest(stream_uri: str) -> bytes:
        # The URI is strict ASCII and excludes concat quoting metacharacters.
        return (
            f"ffconcat version 1.0\nfile '{stream_uri}'\n"
            "option rtsp_transport tcp\n"
        ).encode("ascii")

    def _base_args(self) -> list[str]:
        return [
            str(self._require_ffmpeg()),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-protocol_whitelist",
            _PROTOCOL_WHITELIST,
            "-i",
            "pipe:0",
        ]

    def _probe_args(self) -> list[str]:
        return self._base_args() + [
            "-an",
            "-frames:v",
            "1",
            "-vf",
            "scale=min(1280\\,iw):-2",
            "-c:v",
            "mjpeg",
            "-f",
            "image2pipe",
            "pipe:1",
        ]

    def _stream_args(self) -> list[str]:
        return self._base_args() + [
            "-an",
            "-vf",
            "fps=6,scale=min(1280\\,iw):-2",
            "-c:v",
            "mjpeg",
            "-q:v",
            "5",
            "-f",
            "mpjpeg",
            "-boundary_tag",
            _BOUNDARY,
            "pipe:1",
        ]

    async def _spawn_process(self, args: list[str], manifest: bytes) -> _OwnedProcess:
        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        try:
            process, spawn_cancelled = await _finish_despite_cancellation(
                self._process_factory(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=creationflags,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # process args contain no RTSP URI or credential
            raise ExteriorCameraUnavailable("FFmpeg could not be started.") from exc
        if spawn_cancelled:
            fallback = _OwnedProcess(
                process=process,
                pid=int(getattr(process, "pid", 0) or 0),
                started_at=0.0,
                executable=Path(args[0]),
                argv=tuple(args),
                injected=self._injected_process_factory,
            )
            await _await_cleanup(self._stop_owned(fallback, require_identity=False))
            raise asyncio.CancelledError
        if process.stdin is None or process.stdout is None or process.stderr is None:
            fallback = _OwnedProcess(
                process=process,
                pid=int(getattr(process, "pid", 0) or 0),
                started_at=0.0,
                executable=Path(args[0]),
                argv=tuple(args),
                injected=self._injected_process_factory,
            )
            await _await_cleanup(self._stop_owned(fallback, require_identity=False))
            raise ExteriorCameraUnavailable("FFmpeg did not expose its media pipes.")
        owned: _OwnedProcess | None = None
        try:
            owned = self._capture_owned(process, args)
            owned.stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))
            process.stdin.write(manifest)
            await process.stdin.drain()
            process.stdin.close()
            wait_closed = getattr(process.stdin, "wait_closed", None)
            if wait_closed is not None:
                await wait_closed()
            return owned
        except BaseException as exc:
            target = owned or _OwnedProcess(
                process=process,
                pid=int(getattr(process, "pid", 0) or 0),
                started_at=0.0,
                executable=Path(args[0]),
                argv=tuple(args),
                injected=self._injected_process_factory,
            )
            await _await_cleanup(self._stop_owned(target, require_identity=False))
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, ExteriorCameraError):
                raise
            raise ExteriorCameraUnavailable("FFmpeg media pipes could not be opened.") from exc

    def _capture_owned(self, process: Any, args: list[str]) -> _OwnedProcess:
        pid = int(getattr(process, "pid", 0) or 0)
        if pid <= 0:
            raise ExteriorCameraUnavailable("FFmpeg process identity was unavailable.")
        executable = Path(args[0]).resolve()
        if self._injected_process_factory:
            return _OwnedProcess(
                process=process,
                pid=pid,
                started_at=float(getattr(process, "started_at", self._clock())),
                executable=executable,
                argv=tuple(args),
                injected=True,
            )
        try:
            inspected = psutil.Process(pid)
            observed_exe = Path(inspected.exe()).resolve()
            observed_args = inspected.cmdline()
            started_at = float(inspected.create_time())
        except (psutil.Error, OSError) as exc:
            raise ExteriorCameraUnavailable(
                "FFmpeg process identity could not be verified."
            ) from exc
        if os.path.normcase(str(observed_exe)) != os.path.normcase(str(executable)):
            raise ExteriorCameraUnavailable("FFmpeg executable identity did not match.")
        if not observed_args or tuple(observed_args[1:]) != tuple(args[1:]):
            raise ExteriorCameraUnavailable("FFmpeg command identity did not match.")
        return _OwnedProcess(
            process=process,
            pid=pid,
            started_at=started_at,
            executable=executable,
            argv=tuple(args),
        )

    async def _drain_stderr(self, reader: Any) -> bytes:
        captured = bytearray()
        while True:
            chunk = await reader.read(STREAM_READ_BYTES)
            if not chunk:
                return bytes(captured)
            remaining = MAX_STDERR_BYTES - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])

    @staticmethod
    def _classify_failure(stderr: bytes) -> ExteriorCameraError:
        lowered = stderr.lower()
        if b"401" in lowered or b"unauthorized" in lowered:
            return ExteriorCameraAuthError("Exterior camera credentials were rejected.")
        return ExteriorCameraUnavailable(
            "Exterior camera did not return a decodable frame."
        )

    async def _read_bounded(self, reader: Any, limit: int) -> bytes:
        output = bytearray()
        while True:
            chunk = await reader.read(min(STREAM_READ_BYTES, limit + 1 - len(output)))
            if not chunk:
                return bytes(output)
            output.extend(chunk)
            if len(output) > limit:
                raise ExteriorCameraUnavailable("Exterior camera frame exceeded the size limit.")

    async def _probe_frame(self, credentials: _Credentials) -> camera_svc.CameraFrame:
        stream_uri = await self._resolve_stream_uri(credentials)
        manifest = self._concat_manifest(stream_uri)
        stream_uri = ""
        owned = await self._spawn_process(self._probe_args(), manifest)
        try:
            try:
                raw = await asyncio.wait_for(
                    self._read_bounded(
                        owned.process.stdout, camera_svc.MAX_CAMERA_FRAME_BYTES
                    ),
                    timeout=self.probe_timeout_seconds,
                )
                return_code = await asyncio.wait_for(
                    owned.process.wait(), timeout=PROCESS_STOP_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError as exc:
                raise ExteriorCameraUnavailable(
                    "Exterior camera probe timed out."
                ) from exc
            stderr = await self._stderr_result(owned)
            if return_code != 0:
                raise self._classify_failure(stderr)
            try:
                return camera_svc.validate_camera_frame(raw, "image/jpeg")
            except ValueError as exc:
                raise ExteriorCameraUnavailable(
                    "Exterior camera did not return a decodable frame."
                ) from exc
        finally:
            await _await_cleanup(self._stop_owned(owned))
            manifest = b""

    async def _stderr_result(self, owned: _OwnedProcess) -> bytes:
        if owned.stderr_task is None:
            return b""
        try:
            return await owned.stderr_task
        except (asyncio.CancelledError, OSError):
            return b""

    def _identity_matches(self, owned: _OwnedProcess) -> bool:
        if owned.process.returncode is not None:
            return True
        if owned.injected:
            return int(getattr(owned.process, "pid", 0) or 0) == owned.pid
        try:
            process = psutil.Process(owned.pid)
            observed_exe = Path(process.exe()).resolve()
            observed_args = process.cmdline()
            return (
                abs(float(process.create_time()) - owned.started_at) < 0.01
                and os.path.normcase(str(observed_exe))
                == os.path.normcase(str(owned.executable))
                and bool(observed_args)
                and tuple(observed_args[1:]) == owned.argv[1:]
            )
        except (psutil.Error, OSError):
            return False

    async def _stop_owned(
        self, owned: _OwnedProcess, *, require_identity: bool = True
    ) -> None:
        process = owned.process
        if process.returncode is None:
            if require_identity and not self._identity_matches(owned):
                if owned.stderr_task is not None:
                    owned.stderr_task.cancel()
                return
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), PROCESS_STOP_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                if not require_identity or self._identity_matches(owned):
                    process.kill()
                    try:
                        await asyncio.wait_for(
                            process.wait(), PROCESS_STOP_TIMEOUT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        pass
            except (ProcessLookupError, OSError):
                pass
        if owned.stderr_task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(owned.stderr_task), PROCESS_STOP_TIMEOUT_SECONDS
                )
            except (asyncio.TimeoutError, asyncio.CancelledError, OSError):
                owned.stderr_task.cancel()

    # ---------- Owner-bound session lifecycle ----------

    async def _expire_locked(self) -> None:
        session = self._session
        now = self._clock()
        hard_expired = bool(
            session
            and session.active_started_monotonic is not None
            and now - session.active_started_monotonic >= MAX_ACTIVE_STREAM_SECONDS
        )
        if session is None or (session.expires_monotonic > now and not hard_expired):
            return
        expired = session
        self._session = None
        expired.current_frame = None
        self._cancel_watchdog(expired)
        if expired.process is not None:
            await _await_cleanup(self._stop_owned(expired.process))

    @staticmethod
    def _cancel_watchdog(session: _Session) -> None:
        task = session.watchdog_task
        session.watchdog_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _watch_session(self, session: _Session) -> None:
        """Enforce pending/active deadlines independently of HTTP backpressure."""

        try:
            while True:
                now = self._clock()
                deadline = session.expires_monotonic
                if session.active_started_monotonic is not None:
                    deadline = min(
                        deadline,
                        session.active_started_monotonic + MAX_ACTIVE_STREAM_SECONDS,
                    )
                delay = max(0.05, min(30.0, deadline - now))
                await self._sleep(delay)

                owned: _OwnedProcess | None = None
                async with self._lock:
                    if self._session is not session:
                        return
                    now = self._clock()
                    hard_expired = bool(
                        session.active_started_monotonic is not None
                        and now - session.active_started_monotonic
                        >= MAX_ACTIVE_STREAM_SECONDS
                    )
                    if now < session.expires_monotonic and not hard_expired:
                        continue
                    self._session = None
                    session.current_frame = None
                    session.watchdog_task = None
                    owned = session.process
                if owned is not None:
                    await _await_cleanup(self._stop_owned(owned))
                return
        except asyncio.CancelledError:
            return

    async def create_session(
        self, *, conversation_id: int, owner_id: str
    ) -> dict[str, Any]:
        if int(conversation_id) <= 0:
            raise ValueError("conversation_id must be a positive integer.")
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("Camera session owner is required.")
        async with self._lock:
            await self._expire_locked()
            if self._session is not None:
                raise ExteriorCameraConflict(
                    "Only one exterior camera session can be active at a time."
                )
            credentials = self._load_credentials()
            await self._probe_frame(credentials)
            now = _utc_now()
            session = _Session(
                session_id=secrets.token_urlsafe(24),
                conversation_id=int(conversation_id),
                owner_id=owner,
                label=credentials.label,
                created_at=now.isoformat(),
                expires_at=(now + timedelta(seconds=self.session_ttl_seconds)).isoformat(),
                expires_monotonic=self._clock() + self.session_ttl_seconds,
            )
            self._session = session
            session.watchdog_task = asyncio.create_task(self._watch_session(session))
        return {
            "ok": True,
            "status": "ready",
            "session_id": session.session_id,
            "stream_url": (
                f"/api/cameras/exterior/sessions/{session.session_id}/stream.mjpg"
            ),
            "label": session.label,
            "conversation_id": session.conversation_id,
            "expires_at": session.expires_at,
            "streaming": False,
        }

    async def stream(
        self, *, session_id: str, owner_id: str
    ) -> AsyncIterator[bytes]:
        async with self._lock:
            await self._expire_locked()
            session = self._session
            if (
                session is None
                or not secrets.compare_digest(session.session_id, str(session_id or ""))
                or not secrets.compare_digest(session.owner_id, str(owner_id or ""))
            ):
                raise ExteriorCameraSessionNotFound(
                    "Exterior camera session was not found or has expired."
                )
            if session.process is not None:
                raise ExteriorCameraConflict("Exterior camera stream is already open.")
            try:
                credentials = self._load_credentials()
                stream_uri = await self._resolve_stream_uri(credentials)
                manifest = self._concat_manifest(stream_uri)
                stream_uri = ""
                owned = await self._spawn_process(self._stream_args(), manifest)
                manifest = b""
                try:
                    first = await asyncio.wait_for(
                        owned.process.stdout.read(STREAM_READ_BYTES),
                        timeout=STREAM_START_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError as exc:
                    await _await_cleanup(self._stop_owned(owned))
                    raise ExteriorCameraUnavailable(
                        "Exterior camera stream did not start in time."
                    ) from exc
                except asyncio.CancelledError:
                    await _await_cleanup(self._stop_owned(owned))
                    raise
                except OSError as exc:
                    await _await_cleanup(self._stop_owned(owned))
                    raise ExteriorCameraUnavailable(
                        "Exterior camera stream could not be opened."
                    ) from exc
                if not first:
                    try:
                        try:
                            await asyncio.wait_for(
                                owned.process.wait(), PROCESS_STOP_TIMEOUT_SECONDS
                            )
                        except asyncio.TimeoutError:
                            pass
                        stderr = await self._stderr_result(owned)
                    finally:
                        await _await_cleanup(self._stop_owned(owned))
                    raise self._classify_failure(stderr)
            except BaseException:
                # Opening failures invalidate only the exact pending URL.
                # The lock prevents a replacement session from racing here.
                if self._session is session:
                    self._session = None
                    session.current_frame = None
                    self._cancel_watchdog(session)
                raise
            session.process = owned
            session.active_started_monotonic = self._clock()
        return self._stream_bytes(session, owned, first)

    async def _stream_bytes(
        self, session: _Session, owned: _OwnedProcess, first: bytes
    ) -> AsyncIterator[bytes]:
        parser = _MJPEGFrameParser()
        try:
            chunk = first
            while chunk:
                if self._clock() >= session.expires_monotonic:
                    return
                if (
                    session.active_started_monotonic is not None
                    and self._clock() - session.active_started_monotonic
                    >= MAX_ACTIVE_STREAM_SECONDS
                ):
                    return
                # The opaque, not-yet-opened stream URL remains short-lived,
                # while a connected viewer renews its lease as frames arrive.
                session.expires_monotonic = self._clock() + self.session_ttl_seconds
                session.expires_at = (
                    _utc_now() + timedelta(seconds=self.session_ttl_seconds)
                ).isoformat()
                for raw_frame in parser.feed(chunk):
                    session.current_frame = raw_frame
                yield chunk
                try:
                    chunk = await asyncio.wait_for(
                        owned.process.stdout.read(STREAM_READ_BYTES),
                        timeout=STREAM_IDLE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    return
        finally:
            await _await_cleanup(
                self._finish_session(session.session_id, owned=owned)
            )

    async def current_frame(
        self,
        *,
        session_id: str,
        owner_id: str,
        conversation_id: int,
    ) -> camera_svc.CameraFrame:
        """Return only the latest frame actually proxied by this owned session."""

        async with self._lock:
            await self._expire_locked()
            session = self._session
            if (
                session is None
                or not secrets.compare_digest(session.session_id, str(session_id or ""))
                or not secrets.compare_digest(session.owner_id, str(owner_id or ""))
                or session.conversation_id != int(conversation_id)
            ):
                raise ExteriorCameraSessionNotFound(
                    "Exterior camera session was not found or has expired."
                )
            owned = session.process
            if (
                owned is None
                or owned.process.returncode is not None
                or not self._identity_matches(owned)
            ):
                raise ExteriorCameraFrameUnavailable(
                    "Exterior camera stream is not active."
                )
            if session.current_frame is None:
                raise ExteriorCameraFrameUnavailable(
                    "Exterior camera has not delivered a complete frame yet."
                )
            try:
                return await asyncio.to_thread(
                    camera_svc.validate_camera_frame,
                    session.current_frame,
                    "image/jpeg",
                )
            except ValueError as exc:
                session.current_frame = None
                raise ExteriorCameraFrameUnavailable(
                    "Exterior camera did not deliver a valid JPEG frame."
                ) from exc

    async def _finish_session(
        self, session_id: str, *, owned: _OwnedProcess | None = None
    ) -> None:
        to_stop = owned
        async with self._lock:
            session = self._session
            if session is not None and secrets.compare_digest(
                session.session_id, session_id
            ):
                self._session = None
                session.current_frame = None
                self._cancel_watchdog(session)
                to_stop = to_stop or session.process
        if to_stop is not None:
            await self._stop_owned(to_stop)

    async def delete_session(self, *, session_id: str, owner_id: str) -> dict[str, Any]:
        async with self._lock:
            await self._expire_locked()
            session = self._session
            if (
                session is None
                or not secrets.compare_digest(session.session_id, str(session_id or ""))
                or not secrets.compare_digest(session.owner_id, str(owner_id or ""))
            ):
                raise ExteriorCameraSessionNotFound(
                    "Exterior camera session was not found or has expired."
                )
            self._session = None
            session.current_frame = None
            self._cancel_watchdog(session)
            owned = session.process
        if owned is not None:
            await _await_cleanup(self._stop_owned(owned))
        return {
            "ok": True,
            "status": "stopped",
            "session_id": session.session_id,
            "streaming": False,
        }

    async def shutdown(self) -> None:
        async with self._lock:
            session = self._session
            self._session = None
            if session is not None:
                session.current_frame = None
                self._cancel_watchdog(session)
        if session is not None and session.process is not None:
            await _await_cleanup(self._stop_owned(session.process))


def make_exterior_camera_request(service: ExteriorCameraService):
    """Return a model tool that requests UI streaming without opening it."""

    def exterior_camera_request(args: dict) -> dict[str, Any]:
        prompt = camera_svc.camera_prompt(args.get("prompt"))
        status = service.status()
        configured = status.get("configured") is True
        return {
            "ok": True,
            "status": "awaiting_stream_start" if configured else "not_configured",
            "configured": configured,
            "camera_source_id": "exterior",
            "label": status.get("label"),
            "prompt": prompt,
            "stream_started": False,
            "capture_received": False,
            "message": (
                "Use the inline controls to start the exterior camera stream and "
                "explicitly submit a current frame for analysis."
                if configured
                else "Exterior camera credentials must be configured before streaming."
            ),
        }

    return exterior_camera_request
