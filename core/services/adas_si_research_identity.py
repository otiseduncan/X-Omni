"""Calibration IQ identity adapter for ADAS SI research.

Calibration IQ's exact-RO read intentionally returns two views:

* ``repair_order`` -- the normalized authoritative summary used by X Omni;
* ``raw`` -- the upstream operator snapshot/detail payload.

The SI research service originally looked for VIN only inside ``raw``.  That
made a valid CIQ response fail closed whenever the operator snapshot omitted or
rearranged VIN even though ``get_repair_order`` had already normalized it into
``repair_order.vin``.  Keep the exact-VIN guard, but consume the contract X Omni
itself publishes before falling back to raw shapes.
"""

from __future__ import annotations

import json
import re
from typing import Any

_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_VIN_IN_TEXT = re.compile(r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])")


def _dig(item: Any, *paths: str) -> Any:
    for path in paths:
        current = item
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            else:
                current = None
                break
        if current not in (None, "", [], {}):
            return current
    return None


def _clean(value: Any, limit: int = 32) -> str:
    return " ".join(str(value or "").split())[:limit]


def vin_from_read(read: dict[str, Any]) -> str:
    """Return the exact 17-character VIN from a verified CIQ read shape.

    Prefer the normalized contract produced by ``calibration_iq.get_repair_order``.
    Raw operator/legacy shapes remain fallbacks so older CIQ revisions continue
    to work.  The final bounded scan covers a VIN nested in a future response
    shape without relaxing VIN syntax.
    """
    if not isinstance(read, dict):
        return ""

    candidate = _dig(
        read,
        "repair_order.vin",
        "vin",
        "vehicle.vin",
        "raw.vin",
        "raw.vehicle.vin",
        "raw.vehicle_vin",
        "raw.repair_order.vin",
        "raw.repair_order.vehicle.vin",
    )
    text = _clean(candidate).upper()
    if _VIN_RE.fullmatch(text):
        return text

    match = _VIN_IN_TEXT.search(json.dumps(read, default=str).upper())
    return match.group(0) if match else ""


def install(module: Any) -> None:
    """Install the corrected identity adapter into the SI research module."""
    module.vin_from_read = vin_from_read
