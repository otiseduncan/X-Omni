"""The one place X Omni talks to Frigate.

Frigate is an appliance on another machine: it owns the exterior camera,
continuous recording, detection, and playback. X consumes its authenticated
HTTP API and nothing else -- it does not start Frigate, supervise it, read
its filesystem, scrape its web UI, or touch the camera directly.

Two boundaries matter here and are enforced rather than documented:

* **Secrets never travel outward.** The password lives DPAPI-sealed on disk
  (see windows_secrets), the JWT lives in memory only, and no exception,
  log line, or repr on this module carries either -- including the URL,
  which is logged path-only because a token can ride in a query string.
* **Failure is a state, not a crash.** Frigate's laptop can be asleep,
  rebooting, or off the network at any moment. Every call returns or raises
  something the caller can turn into one of the structured states in
  ``FrigateState``; nothing here blocks X's startup or retries in a loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from .windows_secrets import (
    SecretNotConfigured,
    SecretStore,
    SecretStoreError,
    non_secret_summary,
)

log = logging.getLogger("xomni.frigate")

# Frigate's authenticated port. Port 5000 is its unauthenticated internal
# API and is deliberately not supported as a LAN endpoint for X.
DEFAULT_FRIGATE_PORT = 8971
UNAUTHENTICATED_FRIGATE_PORT = 5000

CREDENTIAL_ENTROPY = b"X Omni Frigate API credentials v1"
CREDENTIAL_DESCRIPTION = "X Omni Frigate API"
SECRET_KEYS = ("password",)

MAX_CAMERA_NAME_CHARS = 64
_CAMERA_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# A DNS name, an mDNS ".local" name, or a literal IPv4/IPv6 address.
_HOST_RE = re.compile(
    r"^(?:"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
    r"|\[[0-9A-Fa-f:.]{2,45}\]"
    r"|[0-9A-Fa-f:]{2,45}"
    r")$"
)

# Response ceilings. Frigate is trusted but it is still a network peer, and
# a bounded read is what keeps one oversized answer from becoming X's
# memory problem.
MAX_JSON_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_CLIP_BYTES = 192 * 1024 * 1024
MAX_REVIEW_ITEMS = 200

DEFAULT_TIMEOUT_SECONDS = 10.0
CLIP_TIMEOUT_SECONDS = 120.0
# A JWT is re-minted this many seconds before it actually expires, so a call
# never races the boundary. Frigate's default token life is long; this only
# matters for the session that has been idle across one.
TOKEN_REFRESH_MARGIN_SECONDS = 120.0
DEFAULT_TOKEN_LIFETIME_SECONDS = 12 * 60 * 60

# Authentication is attempted at most once per failed call. A wrong password
# must surface as a clean, distinguishable error rather than a login loop.
MAX_LOGIN_ATTEMPTS_PER_CALL = 1


class FrigateState:
    """The structured outcomes every Frigate-backed capability reports."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    AUTHENTICATION_REQUIRED = "authentication_required"
    NOT_CONFIGURED = "not_configured"
    CAMERA_UNAVAILABLE = "camera_unavailable"
    NO_RECORDING = "no_recording"
    NO_EVENT_DATA = "no_event_data"
    INVALID_REQUEST = "invalid_request"


class FrigateError(RuntimeError):
    """Base class. Carries a structured state and a short operator-safe message."""

    state = FrigateState.UNAVAILABLE

    def __init__(self, message: str):
        super().__init__(str(message))

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "detail": str(self)}


class FrigateNotConfigured(FrigateError):
    state = FrigateState.NOT_CONFIGURED


class FrigateUnavailable(FrigateError):
    """Frigate could not be reached: host down, network gone, container stopped."""

    state = FrigateState.UNAVAILABLE


class FrigateAuthError(FrigateError):
    """Frigate answered, and refused the credential. Distinct from unreachable."""

    state = FrigateState.AUTHENTICATION_REQUIRED


class FrigateCameraNotFound(FrigateError):
    state = FrigateState.CAMERA_UNAVAILABLE


class FrigateNoRecording(FrigateError):
    """Frigate is healthy and simply has no recording for that span."""

    state = FrigateState.NO_RECORDING


class FrigateInvalidRequest(FrigateError):
    state = FrigateState.INVALID_REQUEST


class FrigateProtocolError(FrigateError):
    """Frigate answered with something this client cannot trust."""

    state = FrigateState.UNAVAILABLE


def normalize_base_url(value: object) -> str:
    """A scheme://host:port origin, with no path, query, or credentials.

    Credentials embedded in a URL would end up in logs and config dumps, so
    they are refused outright rather than stripped silently.
    """
    text = str(value or "").strip().rstrip("/")
    if not text:
        raise FrigateNotConfigured("No Frigate base URL is configured.")
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise FrigateInvalidRequest("The Frigate base URL must be http or https.")
    if parts.username or parts.password:
        raise FrigateInvalidRequest(
            "The Frigate base URL must not embed credentials; register them separately."
        )
    if not parts.hostname:
        raise FrigateInvalidRequest("The Frigate base URL has no host.")
    host = parts.hostname
    # urlsplit is happy to call "not a url at all" a hostname. A host is a
    # name or an address, so anything that is neither is a typo worth
    # catching here rather than as a confusing connection error later.
    if not _HOST_RE.fullmatch(host):
        raise FrigateInvalidRequest("The Frigate base URL host is not a valid name or address.")
    try:
        port = parts.port
    except ValueError as exc:
        raise FrigateInvalidRequest("The Frigate base URL has an invalid port.") from exc
    port = port or DEFAULT_FRIGATE_PORT
    # Port 5000 is Frigate's unauthenticated internal API and is refused
    # outright. Any other port is allowed, because the endpoint is meant to
    # move to a different route without a code change -- a reverse proxy or
    # a Tailscale name terminates TLS on 443, not on 8971.
    if port == UNAUTHENTICATED_FRIGATE_PORT:
        raise FrigateInvalidRequest(
            f"Port {UNAUTHENTICATED_FRIGATE_PORT} is Frigate's unauthenticated internal API; "
            f"X Omni uses its authenticated API (port {DEFAULT_FRIGATE_PORT} by default)."
        )
    netloc = f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, "", "", ""))


def validate_camera_name(value: object) -> str:
    """Frigate camera names become URL path segments; keep them boring."""
    text = str(value or "").strip()
    if not text:
        raise FrigateInvalidRequest("No Frigate camera name is configured.")
    if len(text) > MAX_CAMERA_NAME_CHARS or not _CAMERA_NAME_RE.fullmatch(text):
        raise FrigateInvalidRequest(
            "A Frigate camera name may only contain letters, digits, underscore, and hyphen."
        )
    return text


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _epoch(value: datetime) -> float:
    return _utc(value).timestamp()


def _safe_path(url: str) -> str:
    """What may be logged about a request: its path, never its query or host."""
    try:
        return urlsplit(str(url)).path or "/"
    except ValueError:
        return "/"


@dataclass(frozen=True)
class FrigateCredential:
    username: str
    password: str

    def __repr__(self) -> str:  # never let a traceback print the password
        return f"FrigateCredential(username={self.username!r}, password=<redacted>)"

    __str__ = __repr__


def credential_store(path: Path, **overrides) -> SecretStore:
    return SecretStore(
        path,
        entropy=CREDENTIAL_ENTROPY,
        description=CREDENTIAL_DESCRIPTION,
        **overrides,
    )


class FrigateClient:
    """Authenticated, bounded, non-blocking access to one Frigate instance.

    One instance is built at startup whether or not Frigate is reachable --
    construction performs no I/O, so a sleeping Ubuntu laptop can never
    delay or fail X Omni's boot.
    """

    def __init__(
        self,
        *,
        base_url: str,
        camera: str,
        credential_path: Path,
        verify_tls: bool = False,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        clip_timeout_seconds: float = CLIP_TIMEOUT_SECONDS,
        max_clip_seconds: int = 300,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        store: Optional[SecretStore] = None,
        clock=None,
    ):
        # Deliberately tolerant at construction: a misconfigured base URL
        # must degrade to "not configured" at call time, not explode at
        # import/startup time.
        self._base_url_raw = str(base_url or "")
        try:
            self.base_url: Optional[str] = normalize_base_url(base_url)
            self._config_error: Optional[FrigateError] = None
        except FrigateError as exc:
            self.base_url = None
            self._config_error = exc
        try:
            self.camera: Optional[str] = validate_camera_name(camera)
        except FrigateError as exc:
            self.camera = None
            if self._config_error is None:
                self._config_error = exc
        self.verify_tls = bool(verify_tls)
        self.timeout_seconds = float(timeout_seconds)
        self.clip_timeout_seconds = float(clip_timeout_seconds)
        self.max_clip_seconds = int(max_clip_seconds)
        self._transport = transport
        self._secrets = store if store is not None else credential_store(Path(credential_path))
        self._clock = clock or time.monotonic
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._login_lock = asyncio.Lock()
        self._credential_update_lock = asyncio.Lock()

    # ---------------------------------------------------------------- config

    def configured(self) -> bool:
        """Whether the endpoint and logical camera are valid configuration.

        Credential presence is deliberately separate. A configured Frigate
        endpoint with no registered secret is an authentication boundary, not
        an absent configuration.
        """
        return self.base_url is not None and self.camera is not None

    def configuration_summary(self) -> dict[str, Any]:
        """Safe to return over the API and to put in front of the model."""
        summary: dict[str, Any] = {
            "base_url": self.base_url,
            "camera": self.camera,
            "verify_tls": self.verify_tls,
            "credential_registered": self._secrets.configured(),
            "authenticated_port": DEFAULT_FRIGATE_PORT,
        }
        if self._config_error is not None:
            summary["configuration_error"] = str(self._config_error)
        return summary

    def _credential(self) -> FrigateCredential:
        try:
            document = self._secrets.load()
        except SecretNotConfigured as exc:
            raise FrigateAuthError(
                "No Frigate credential is registered on this machine."
            ) from exc
        except SecretStoreError as exc:
            raise FrigateAuthError(str(exc)) from exc
        username = str(document.get("username") or "").strip()
        password = str(document.get("password") or "")
        if not username or not password:
            raise FrigateAuthError("The stored Frigate credential is incomplete.")
        return FrigateCredential(username=username, password=password)

    def save_credential(self, *, username: str, password: str) -> dict[str, Any]:
        username = str(username or "").strip()
        if not username or not str(password or ""):
            raise FrigateInvalidRequest("A Frigate username and password are both required.")
        document = {"username": username, "password": str(password)}
        self._secrets.save(document)
        self._token = None
        self._token_expires_at = 0.0
        return non_secret_summary(document, secret_keys=SECRET_KEYS)

    async def save_credential_verified(
        self, *, username: str, password: str
    ) -> dict[str, Any]:
        """Replace the secret only if login and camera verification succeed.

        The previous DPAPI document is restored on every failed or cancelled
        verification, so an operator typo cannot destroy a working setup.
        """
        async with self._credential_update_lock:
            previous: Optional[dict[str, Any]] = None
            had_previous = False
            try:
                previous = self._secrets.load()
                had_previous = True
            except SecretNotConfigured:
                pass
            except SecretStoreError as exc:
                raise FrigateAuthError(str(exc)) from exc

            summary = self.save_credential(username=username, password=password)
            try:
                health = await self.health()
                if health.get("state") != FrigateState.AVAILABLE:
                    raise FrigateCameraNotFound(
                        str(health.get("detail") or "The configured Frigate camera is unavailable.")
                    )
            except BaseException:
                self._token = None
                self._token_expires_at = 0.0
                if had_previous and previous is not None:
                    self._secrets.save(previous)
                else:
                    self._secrets.clear()
                raise
            return {"credential": summary, "health": health}

    def forget_credential(self) -> bool:
        self._token = None
        self._token_expires_at = 0.0
        return self._secrets.clear()

    def __repr__(self) -> str:
        return (
            f"<FrigateClient base_url={self.base_url!r} camera={self.camera!r} "
            f"credential_registered={self._secrets.configured()}>"
        )

    # ----------------------------------------------------------------- HTTP

    def _client(self, *, timeout_seconds: float) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "base_url": self.base_url or "",
            "timeout": httpx.Timeout(timeout_seconds),
            "follow_redirects": False,
            # A machine-level proxy must never receive Frigate credentials or
            # redirect this private-LAN integration away from the configured host.
            "trust_env": False,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        else:
            # Frigate ships a self-signed certificate on 8971. Verification
            # is a setting rather than a constant so an operator who installs
            # a real certificate can turn it on without a code change.
            kwargs["verify"] = self.verify_tls
        return httpx.AsyncClient(**kwargs)

    def _require_configured(self) -> None:
        if self._config_error is not None:
            raise self._config_error
        if self.base_url is None or self.camera is None:
            raise FrigateNotConfigured("Frigate is not configured.")

    def _token_valid(self) -> bool:
        return bool(self._token) and self._clock() < self._token_expires_at

    async def _login(self) -> str:
        """Exchange the stored credential for a JWT. Never logs either."""
        credential = self._credential()
        async with self._client(timeout_seconds=self.timeout_seconds) as client:
            try:
                response = await client.post(
                    "/api/login",
                    json={"user": credential.username, "password": credential.password},
                )
            except httpx.TimeoutException as exc:
                raise FrigateUnavailable("Frigate did not answer the login request in time.") from exc
            except httpx.HTTPError as exc:
                raise FrigateUnavailable("Frigate could not be reached to authenticate.") from exc
        if response.status_code in (401, 403):
            raise FrigateAuthError(
                "Frigate rejected the stored credential. Register the current password for X's Frigate account."
            )
        if response.status_code >= 500:
            raise FrigateUnavailable("Frigate returned a server error while authenticating.")
        if response.status_code not in (200, 201, 204):
            raise FrigateAuthError(
                f"Frigate refused the login request (HTTP {response.status_code})."
            )
        token = self._extract_token(response)
        if not token:
            raise FrigateProtocolError("Frigate's login response carried no usable session token.")
        self._token = token
        self._token_expires_at = self._clock() + DEFAULT_TOKEN_LIFETIME_SECONDS - TOKEN_REFRESH_MARGIN_SECONDS
        return token

    @staticmethod
    def _extract_token(response: httpx.Response) -> Optional[str]:
        """Frigate hands the JWT back as a cookie; some builds also body it."""
        cookie = response.cookies.get("frigate_token")
        if cookie:
            return str(cookie)
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            return None
        if isinstance(payload, dict):
            for key in ("access_token", "token", "jwt"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    async def _authorized_headers(self) -> dict[str, str]:
        if not self._token_valid():
            async with self._login_lock:
                if not self._token_valid():
                    await self._login()
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
        max_bytes: int = MAX_JSON_RESPONSE_BYTES,
        expect: str = "json",
    ) -> Any:
        """One authenticated call, with exactly one re-login on a 401."""
        self._require_configured()
        timeout = self.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        attempts = 0
        while True:
            headers = await self._authorized_headers()
            async with self._client(timeout_seconds=timeout) as client:
                try:
                    request = client.build_request(method, path, params=params, headers=headers)
                    response = await client.send(request, stream=True)
                except httpx.TimeoutException as exc:
                    raise FrigateUnavailable(
                        f"Frigate did not answer {_safe_path(path)} within {timeout:.0f}s."
                    ) from exc
                except httpx.HTTPError as exc:
                    raise FrigateUnavailable("Frigate could not be reached.") from exc
                try:
                    if response.status_code in (401, 403):
                        await response.aclose()
                        # The session expired or the account changed. Try a
                        # fresh login exactly once; a second refusal is a
                        # real authentication failure, not a retry loop.
                        if attempts >= MAX_LOGIN_ATTEMPTS_PER_CALL:
                            raise FrigateAuthError(
                                "Frigate rejected X's session. The Frigate account or password may have changed."
                            )
                        attempts += 1
                        self._token = None
                        self._token_expires_at = 0.0
                        continue
                    return await self._read(response, path, max_bytes=max_bytes, expect=expect)
                finally:
                    await response.aclose()

    async def _read(self, response: httpx.Response, path: str, *, max_bytes: int, expect: str) -> Any:
        if response.status_code == 404:
            raise FrigateNoRecording(f"Frigate has nothing at {_safe_path(path)}.")
        if response.status_code >= 500:
            raise FrigateUnavailable(
                f"Frigate returned HTTP {response.status_code} for {_safe_path(path)}."
            )
        if response.status_code >= 400:
            raise FrigateInvalidRequest(
                f"Frigate refused {_safe_path(path)} (HTTP {response.status_code})."
            )
        payload = bytearray()
        async for chunk in response.aiter_bytes():
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise FrigateProtocolError(
                    f"Frigate's response for {_safe_path(path)} exceeded {max_bytes} bytes."
                )
        raw = bytes(payload)
        if expect == "bytes":
            if not raw:
                raise FrigateNoRecording(f"Frigate returned no data for {_safe_path(path)}.")
            return raw
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise FrigateProtocolError(
                f"Frigate's response for {_safe_path(path)} was not valid JSON."
            ) from exc

    # ----------------------------------------------------------- capabilities

    async def version(self) -> str:
        payload = await self._request("GET", "/api/version", max_bytes=4096, expect="bytes")
        return payload.decode("utf-8", "replace").strip()[:64]

    async def config(self) -> dict[str, Any]:
        payload = await self._request("GET", "/api/config")
        if not isinstance(payload, dict):
            raise FrigateProtocolError("Frigate's configuration response was not an object.")
        return payload

    async def stats(self) -> dict[str, Any]:
        payload = await self._request("GET", "/api/stats")
        if not isinstance(payload, dict):
            raise FrigateProtocolError("Frigate's statistics response was not an object.")
        return payload

    async def health(self) -> dict[str, Any]:
        """Is Frigate up, is X's camera configured, and is it actually running?

        A TCP connect is not health: the check is that the configured camera
        appears in Frigate's own configuration and that Frigate reports a
        live camera process for it.
        """
        self._require_configured()
        camera = str(self.camera)
        result: dict[str, Any] = {
            "state": FrigateState.AVAILABLE,
            "base_url": self.base_url,
            "camera": camera,
            "camera_configured": False,
            "camera_running": False,
            "cameras": [],
            "version": None,
            "detail": None,
        }
        configuration = await self.config()
        cameras = configuration.get("cameras")
        names = sorted(str(name) for name in cameras) if isinstance(cameras, dict) else []
        result["cameras"] = names
        result["camera_configured"] = camera in names
        if not result["camera_configured"]:
            result["state"] = FrigateState.CAMERA_UNAVAILABLE
            result["detail"] = f"Frigate has no camera named {camera!r}."
            return result
        try:
            statistics = await self.stats()
        except FrigateError as exc:
            # Config proved the camera exists; stats are a bonus, not a gate.
            result["detail"] = str(exc)
            return result
        cameras_stats = statistics.get("cameras")
        if isinstance(cameras_stats, dict) and camera in cameras_stats:
            entry = cameras_stats.get(camera)
            if isinstance(entry, dict):
                pid = entry.get("pid") or entry.get("capture_pid")
                fps = entry.get("camera_fps")
                result["camera_running"] = bool(pid) or bool(fps)
                result["camera_fps"] = fps
        if not result["camera_running"]:
            result["state"] = FrigateState.CAMERA_UNAVAILABLE
            result["detail"] = (
                f"Frigate is running but reports no live capture for {camera!r}."
            )
        service = statistics.get("service")
        if isinstance(service, dict) and service.get("version"):
            result["version"] = str(service["version"])[:64]
        return result

    async def latest_frame(self, *, height: Optional[int] = None) -> bytes:
        """X's eyes on the exterior: Frigate's current frame for the camera."""
        self._require_configured()
        params: dict[str, Any] = {}
        if height:
            params["h"] = int(height)
        try:
            return await self._request(
                "GET",
                f"/api/{self.camera}/latest.jpg",
                params=params or None,
                max_bytes=MAX_IMAGE_BYTES,
                expect="bytes",
            )
        except FrigateNoRecording as exc:
            raise FrigateCameraNotFound(
                f"Frigate has no current frame for {self.camera!r}."
            ) from exc

    async def clip_bytes(self, since: datetime, until: datetime) -> bytes:
        """A bounded recording export for one time range, as MP4 bytes.

        Nothing is written to disk here. The caller keeps the bytes only as
        long as it needs them; Omega never becomes a second archive.
        """
        self._require_configured()
        since_utc, until_utc = _utc(since), _utc(until)
        if until_utc <= since_utc:
            raise FrigateInvalidRequest("The clip end time must be after its start time.")
        duration = (until_utc - since_utc).total_seconds()
        if duration > self.max_clip_seconds:
            raise FrigateInvalidRequest(
                f"Recording requests are limited to {self.max_clip_seconds} seconds; "
                f"{int(duration)} were requested."
            )
        start = int(_epoch(since_utc))
        end = int(_epoch(until_utc))
        return await self._request(
            "GET",
            f"/api/{self.camera}/start/{start}/end/{end}/clip.mp4",
            timeout_seconds=self.clip_timeout_seconds,
            max_bytes=MAX_CLIP_BYTES,
            expect="bytes",
        )

    async def recording_spans(self, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """What Frigate actually holds for a range -- summary, not footage."""
        self._require_configured()
        since_utc, until_utc = _utc(since), _utc(until)
        if until_utc <= since_utc:
            return []
        payload = await self._request(
            "GET",
            f"/api/{self.camera}/recordings",
            params={"after": _epoch(since_utc), "before": _epoch(until_utc)},
        )
        if not isinstance(payload, list):
            raise FrigateProtocolError("Frigate's recordings response was not a list.")
        spans: list[dict[str, Any]] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            start = entry.get("start_time")
            end = entry.get("end_time")
            try:
                start_value = float(start)
                end_value = float(end)
            except (TypeError, ValueError):
                continue
            if end_value <= start_value:
                continue
            spans.append(
                {
                    "started_at": datetime.fromtimestamp(start_value, tz=timezone.utc),
                    "ended_at": datetime.fromtimestamp(end_value, tz=timezone.utc),
                    "duration_seconds": round(end_value - start_value, 3),
                }
            )
        spans.sort(key=lambda row: row["started_at"])
        return spans

    async def review(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 40,
        severity: Optional[str] = None,
        labels: Optional[list[str]] = None,
        reviewed: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        """Frigate's review records: what it decided was worth a human's time."""
        self._require_configured()
        params: dict[str, Any] = {
            "cameras": self.camera,
            "limit": max(1, min(int(limit), MAX_REVIEW_ITEMS)),
        }
        if since is not None:
            params["after"] = _epoch(since)
        if until is not None:
            params["before"] = _epoch(until)
        if severity:
            params["severity"] = str(severity)[:32]
        if labels:
            params["labels"] = ",".join(str(label)[:32] for label in labels[:12])
        if reviewed is not None:
            params["reviewed"] = 1 if reviewed else 0
        payload = await self._request("GET", "/api/review", params=params)
        if not isinstance(payload, list):
            raise FrigateProtocolError("Frigate's review response was not a list.")
        return [entry for entry in payload if isinstance(entry, dict)]

    async def events(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 40,
        labels: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        self._require_configured()
        params: dict[str, Any] = {
            "cameras": self.camera,
            "limit": max(1, min(int(limit), MAX_REVIEW_ITEMS)),
        }
        if since is not None:
            params["after"] = _epoch(since)
        if until is not None:
            params["before"] = _epoch(until)
        if labels:
            params["labels"] = ",".join(str(label)[:32] for label in labels[:12])
        payload = await self._request("GET", "/api/events", params=params)
        if not isinstance(payload, list):
            raise FrigateProtocolError("Frigate's events response was not a list.")
        return [entry for entry in payload if isinstance(entry, dict)]

    async def event_snapshot(self, event_id: str) -> bytes:
        self._require_configured()
        identifier = str(event_id or "").strip()
        if not identifier or len(identifier) > 128 or not re.fullmatch(r"[A-Za-z0-9._-]+", identifier):
            raise FrigateInvalidRequest("That Frigate event id is not usable.")
        return await self._request(
            "GET",
            f"/api/events/{identifier}/snapshot.jpg",
            max_bytes=MAX_IMAGE_BYTES,
            expect="bytes",
        )
