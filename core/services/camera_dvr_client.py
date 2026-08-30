"""HTTP client X Omni Core uses to reach the independent X DVR service.

Core no longer owns continuous recording or the E:\\XOmni-DVR archive -- the
DVR service (`core.dvr_service`) does, as its own OS process, so that
recording keeps running across a Core restart, a model swap, or the DVR GUI
being closed. This client presents the same narrow async surface the
existing `camera_security.py` tool handlers already call on a `CameraDVR`
instance (`status`, `list_segments`, `range_clip`, `event_clip`,
`footage_analysis_samples`), so those handlers did not need to change --
only what gets passed in as `dvr=`.

Core has no browser session/cookie when a tool handler runs, so calls
authenticate with a loopback-only shared token instead
(`settings.internal_dvr_token`, see `camera_dvr.create_router`'s
`require_owner_or_internal`). That token is never logged, never returned to
the model, and never sent anywhere but this one local process.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Optional

import httpx

from . import camera_dvr as camera_dvr_svc

log = logging.getLogger("xomni.camera_dvr_client")

_REQUEST_TIMEOUT_SECONDS = 60.0


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class DVRServiceClient:
    """Bounded async adapter over the DVR service's HTTP API."""

    def __init__(
        self,
        base_url: str,
        internal_token: str,
        *,
        timeout: float = _REQUEST_TIMEOUT_SECONDS,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-XOmni-Internal-Token": internal_token}
        self._timeout = timeout
        # None in production (a real loopback connection); tests pass an
        # ASGI transport to exercise the real DVR router without a socket.
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout,
            transport=self._transport,
            trust_env=False,
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            detail = str(response.json().get("detail") or "").strip()
        except Exception:
            detail = ""
        detail = detail or f"DVR service request failed ({response.status_code})."
        if response.status_code == 404:
            raise FileNotFoundError(detail)
        if response.status_code == 400:
            raise ValueError(detail)
        if response.status_code in (408, 503, 504):
            raise camera_dvr_svc.PlaybackPreparationError(detail)
        raise RuntimeError(detail)

    async def status(self) -> dict[str, Any]:
        async with self._client() as client:
            try:
                response = await client.get("/dvr/api/status")
            except httpx.HTTPError as exc:
                raise RuntimeError("The DVR service is unreachable.") from exc
        self._raise_for_status(response)
        return response.json()

    async def list_segments(
        self,
        *,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 40,
        complete_only: bool = False,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        async with self._client() as client:
            try:
                response = await client.get("/dvr/api/segments", params=params)
            except httpx.HTTPError as exc:
                raise RuntimeError("The DVR service is unreachable.") from exc
        self._raise_for_status(response)
        rows = list(response.json().get("items") or [])[: max(1, int(limit))]
        if complete_only:
            rows = [row for row in rows if bool(row.get("complete"))]
        # Never hand the model a raw archive filename -- id/time/codec
        # metadata is the intended public shape; the segment path stays a
        # DVR-service-internal detail.
        for row in rows:
            row.pop("filename", None)
        return rows

    async def range_clip(self, since: datetime, until: datetime, *, cache_name: str) -> PurePosixPath:
        async with self._client() as client:
            try:
                response = await client.post(
                    "/dvr/api/clips/range",
                    json={"since": _iso(since), "until": _iso(until)},
                )
            except httpx.HTTPError as exc:
                raise camera_dvr_svc.PlaybackPreparationError(
                    "The DVR service is unreachable."
                ) from exc
        self._raise_for_status(response)
        return PurePosixPath(str(response.json()["filename"]))

    async def event_clip(self, _store, burst_id: int) -> PurePosixPath:
        async with self._client() as client:
            try:
                response = await client.post(f"/dvr/api/events/{int(burst_id)}/clip")
            except httpx.HTTPError as exc:
                raise camera_dvr_svc.PlaybackPreparationError(
                    "The DVR service is unreachable."
                ) from exc
        self._raise_for_status(response)
        return PurePosixPath(str(response.json()["filename"]))

    async def footage_analysis_samples(self, since: datetime, until: datetime) -> dict[str, Any]:
        async with self._client() as client:
            try:
                response = await client.post(
                    "/dvr/api/analysis/samples",
                    json={"since": _iso(since), "until": _iso(until)},
                )
            except httpx.HTTPError as exc:
                raise camera_dvr_svc.PlaybackPreparationError(
                    "The DVR service is unreachable."
                ) from exc
        self._raise_for_status(response)
        payload = dict(response.json())
        payload["contact_sheet"] = base64.b64decode(payload.pop("contact_sheet_base64"))
        return payload
