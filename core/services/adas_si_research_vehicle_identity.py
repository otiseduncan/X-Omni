from __future__ import annotations

from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_vehicle_identity_fallback_installed__"
_ACTIVE = frozenset({"required", "likely_required", "needs_research"})


def _fallback_target(module: Any, read: Any) -> dict[str, Any] | None:
    if not isinstance(read, dict) or read.get("status") != "verified":
        return None
    raw = read.get("raw") if isinstance(read.get("raw"), dict) else {}
    repair_order = read.get("repair_order") if isinstance(read.get("repair_order"), dict) else {}

    ro_number = module._clean(
        module._dig(repair_order, "RO", "ro_number")
        or module._dig(raw, "ro_number", "repair_order.ro_number", "number"),
        40,
    )
    ro_id = module._clean(
        module._dig(repair_order, "id") or module._dig(raw, "id", "repair_order.id"), 80
    )
    vehicle_raw = module._dig(raw, "vehicle", "repair_order.vehicle") or {}
    if not isinstance(vehicle_raw, dict):
        vehicle_raw = {}
    year = vehicle_raw.get("year") or module._dig(raw, "year", "repair_order.year")
    try:
        year_value = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        year_value = None
    make = module.normalize_make(
        vehicle_raw.get("make") or module._dig(raw, "make", "repair_order.make")
    )
    trim = module._clean(
        vehicle_raw.get("trim") or module._dig(raw, "trim", "repair_order.trim"), 80
    )
    model = module.normalize_model(
        vehicle_raw.get("model") or module._dig(raw, "model", "repair_order.model"), trim
    )
    if not ro_number or not ro_id or not year_value or not make or not model:
        return None

    calibrations: list[dict[str, Any]] = []
    for item in module._dig(raw, "calibrations", "calibration_items", "repair_order.calibrations") or []:
        if not isinstance(item, dict):
            continue
        determination = module._clean(item.get("determination") or item.get("status"), 40).casefold()
        if determination and determination not in _ACTIVE:
            continue
        title = module._clean(item.get("title") or item.get("name") or item.get("calibration_type"), 160)
        if not title:
            continue
        calibrations.append(
            {
                "id": module._clean(item.get("id"), 80) or None,
                "title": title,
                "determination": determination or None,
                "version": item.get("version"),
            }
        )

    vehicle: dict[str, Any] = {"year": year_value, "make": make, "model": model}
    if trim:
        vehicle["trim"] = trim
    return {
        "ro_number": ro_number,
        "repair_order_id": ro_id,
        "vehicle": vehicle,
        "vehicle_label": " ".join(str(part) for part in (year_value, make, model, trim) if part),
        "vin": "",
        "identity_mode": "year_make_model",
        "phase": module._dig(repair_order, "Phase") or module._dig(raw, "phase", "workflow.phase"),
        "shop": module._clean(
            module._dig(repair_order, "Shop") or module._dig(raw, "shop.name", "shop"), 60
        ) or None,
        "calibrations": calibrations,
    }


def install(module: Any) -> None:
    """Prefer exact VIN identity, but keep verified Y/M/M work usable when VIN is absent."""
    if getattr(module, _INSTALLED_ATTR, False):
        return
    original = module.target_from_read

    @wraps(original)
    def target_from_read_with_ymm_fallback(read: dict[str, Any]):
        target = original(read)
        if target is not None:
            target = dict(target)
            target.setdefault("identity_mode", "vin")
            return target
        return _fallback_target(module, read)

    module.target_from_read = target_from_read_with_ymm_fallback
    setattr(module, _INSTALLED_ATTR, True)
