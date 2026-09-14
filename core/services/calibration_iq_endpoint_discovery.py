"""Resilient local endpoint discovery for Calibration IQ.

Calibration IQ's native Windows stack exposes the operator UI through Caddy on
127.0.0.1:8084 and the FastAPI backend directly on 127.0.0.1:18000.  X Omni
historically trusted one configured tool-API URL and varied only the loopback
hostname.  That makes the UI visibly healthy while X reports ``offline`` when
a local env file carries an old port/path.

Keep configuration authoritative when it answers, but for loopback-only CIQ
addresses recover to the known native endpoints and canonical tool path.  A
candidate is accepted only when ``/health`` answers 200 or 401, so discovery
never guesses success from an open port or the frontend shell alone.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

log = logging.getLogger("xomni.calibration_iq.endpoint_discovery")

TOOL_PATH = "/api/v1/tools/v1/calibration-iq"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
# Production native public/Caddy, direct native backend, and the historical
# local Docker/public port. Configured address is always tried first.
LOCAL_PORTS = (8084, 18000, 8080)


def _netloc(host: str, port: int | None) -> str:
    rendered = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{rendered}:{port}" if port is not None else rendered


def candidate_bases(base: str) -> list[str]:
    """Return bounded Calibration IQ tool-API candidates in preference order."""
    configured = str(base or "").strip().rstrip("/")
    if not configured:
        return []

    out: list[str] = []

    def add(value: str) -> None:
        value = value.rstrip("/")
        if value and value not in out:
            out.append(value)

    add(configured)
    parsed = urlsplit(configured)
    host = (parsed.hostname or "").casefold()
    if host not in LOOPBACK_HOSTS:
        return out

    scheme = parsed.scheme or "http"
    configured_port = parsed.port
    configured_path = parsed.path.rstrip("/") or TOOL_PATH

    # First recover hostname/path only while staying on the configured port.
    for loopback in LOOPBACK_HOSTS:
        if configured_port is not None:
            add(urlunsplit((scheme, _netloc(loopback, configured_port), configured_path, "", "")))
            add(urlunsplit((scheme, _netloc(loopback, configured_port), TOOL_PATH, "", "")))

    # Then recover the known local runtime ports using the canonical tool path.
    for port in LOCAL_PORTS:
        for loopback in LOOPBACK_HOSTS:
            add(urlunsplit((scheme, _netloc(loopback, port), TOOL_PATH, "", "")))
    return out


async def _answers_health(base: str, timeout: float = 3.0) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(f"{base}/health")
    except httpx.HTTPError:
        return False
    return response.status_code in {200, 401}


def install(module: Any) -> None:
    """Install endpoint discovery into ``core.services.calibration_iq``."""

    module._candidate_bases = candidate_bases

    async def resolve_base(settings: Any) -> str:
        configured = str(settings.calibration_iq_base_url or "").strip().rstrip("/")
        if not configured:
            return configured

        cache = module._RESOLVED_BASE
        cached = cache.get(configured)
        if cached:
            if await _answers_health(cached):
                return cached
            cache.pop(configured, None)

        for candidate in candidate_bases(configured):
            if not await _answers_health(candidate):
                continue
            if candidate != configured:
                log.warning(
                    "Calibration IQ answered at %s, not configured %s; using proven local endpoint.",
                    candidate,
                    configured,
                )
            cache[configured] = candidate
            return candidate
        return configured

    module.resolve_base = resolve_base
