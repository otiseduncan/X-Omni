"""Prove Calibration IQ's repair-order data plane, not just its HTTP shell.

Calibration IQ can keep Caddy/backend answering while PostgreSQL is unavailable.
That happened live on 2026-09-14 and downstream SI research misdiagnosed an
empty/unreachable RO collection as a VIN problem.  The VIN guard itself is
correct; this layer makes the upstream fact trustworthy before X interprets it.

No vehicle identity is invented here.  A verified RO with an empty VIN remains
an actual missing-VIN case.  A missing RO is reclassified only when the
authenticated collection proves the entire data plane is unavailable or empty.
"""

from __future__ import annotations

from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_ciq_data_plane_guard_installed__"


async def probe_data_plane(module: Any, settings: Any) -> dict[str, Any]:
    """Read one authenticated collection page as proof the RO data plane works."""
    token = module._service_token(settings.calibration_iq_project_path)  # noqa: SLF001
    if not token:
        return {"status": "not_configured", "verified": False, "count": None}

    base = await module.resolve_base(settings)
    try:
        async with module.httpx.AsyncClient(
            timeout=module.HEALTH_TIMEOUT,
            trust_env=False,
        ) as client:
            response = await client.get(
                f"{base}/collection/ros",
                params={"limit": 1, "offset": 0},
                headers=module._auth(token),  # noqa: SLF001
            )
    except module.httpx.HTTPError as exc:
        return {
            "status": "offline",
            "verified": False,
            "count": None,
            "error": type(exc).__name__,
        }

    if response.status_code == 401:
        return {
            "status": "authentication_failed",
            "verified": False,
            "count": None,
            "http_status": 401,
        }
    if response.status_code >= 400:
        return {
            "status": "unavailable",
            "verified": False,
            "count": None,
            "http_status": response.status_code,
        }

    try:
        body = response.json()
    except ValueError:
        return {"status": "invalid_response", "verified": False, "count": None}
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        return {"status": "invalid_response", "verified": False, "count": None}

    raw_count = body.get("count")
    count: int | None = None
    if raw_count not in (None, ""):
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            return {"status": "invalid_response", "verified": False, "count": None}
        if count < 0:
            return {"status": "invalid_response", "verified": False, "count": None}

    return {
        "status": "available",
        "verified": True,
        "count": count,
        "sampled_rows": len(body["items"]),
    }


def install(module: Any) -> None:
    if getattr(module, _INSTALLED_ATTR, False):
        return

    original_health = module.health
    original_get_repair_order = module.get_repair_order

    @wraps(original_health)
    async def health_with_data_plane(settings: Any):
        result = await original_health(settings)
        if not isinstance(result, dict):
            return result
        if result.get("status") != "available" or result.get("token_present") is not True:
            return result

        probe = await probe_data_plane(module, settings)
        output = {**result, "data_plane": probe}
        if probe.get("verified") is True:
            return output
        output["status"] = "degraded"
        output["message"] = (
            "Calibration IQ's HTTP service is answering, but its authenticated repair-order "
            "data plane is unavailable. Do not interpret missing repair orders or VINs until "
            "the database/data plane is healthy."
        )
        return output

    @wraps(original_get_repair_order)
    async def get_repair_order_with_data_plane_proof(settings: Any, args: dict):
        result = await original_get_repair_order(settings, args)
        if not isinstance(result, dict) or result.get("status") != "no_result":
            return result

        probe = await probe_data_plane(module, settings)
        if probe.get("verified") is not True:
            return {
                **result,
                "status": "data_plane_unavailable",
                "data_plane": probe,
                "message": (
                    "Calibration IQ could not prove its repair-order data plane while looking "
                    "up this RO. The RO is not proven missing, and this must not be reported "
                    "as a missing VIN."
                ),
            }
        if probe.get("count") == 0:
            return {
                **result,
                "status": "data_plane_empty",
                "data_plane": probe,
                "message": (
                    "Calibration IQ's authenticated collection currently reports zero repair "
                    "orders. The requested RO is not proven to be a VIN-missing record; verify "
                    "the Calibration IQ database before SI research."
                ),
            }
        return {**result, "data_plane": probe}

    module.health = health_with_data_plane
    module.get_repair_order = get_repair_order_with_data_plane_proof
    setattr(module, _INSTALLED_ATTR, True)
