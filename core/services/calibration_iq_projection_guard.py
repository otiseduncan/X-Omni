"""Normalize model-facing Calibration IQ projections without changing CIQ truth.

Two live conversation failures on 2026-09-15 came from projection ambiguity rather
than bad upstream state:

* exact-RO cards showed ``Phase: null`` while the same authoritative snapshot
  carried ``repair_order.shop_stage_number`` (for example 5 = Refinish and
  6 = Reassemble).  Board rows already exposed those phases, so X incorrectly
  diagnosed otherwise-valid ROs as having broken phase configuration.
* the service health/data-plane probe returned the collection's unfiltered
  ``count``.  That number proves the data plane is populated; it is not an
  active-RO count, calibration count, phase count, or shop count.  Exposing it
  on a status card invited the model to call it "active calibration records".

This adapter fixes only the presentation contract.  Raw Calibration IQ snapshots,
operator versions, mutations, and server-side filtering remain authoritative and
untouched.
"""

from __future__ import annotations

from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_ciq_projection_guard_installed__"


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _present(value: Any) -> bool:
    return value not in (None, "", [], {})


def _phase_from_payload(module: Any, value: Any) -> Any:
    """Return one canonical phase from known exact-snapshot stage fields."""
    root = _mapping(value)
    raw_ro = _mapping(root.get("repair_order"))
    workflow = _mapping(root.get("workflow"))
    workflow_phase = workflow.get("phase")
    workflow_phase = _mapping(workflow_phase) if isinstance(workflow_phase, dict) else workflow_phase

    candidates = [
        workflow_phase.get("number") if isinstance(workflow_phase, dict) else workflow_phase,
        workflow.get("phase_number"),
        workflow.get("shop_stage_number"),
        raw_ro.get("phase"),
        raw_ro.get("phase_number"),
        raw_ro.get("shop_stage_number"),
        root.get("phase"),
        root.get("phase_number"),
        root.get("shop_stage_number"),
    ]
    normalize = getattr(module, "normalize_phase", None)
    for candidate in candidates:
        if not _present(candidate):
            continue
        try:
            normalized = normalize(candidate) if callable(normalize) else str(candidate).strip()
        except (TypeError, ValueError):
            continue
        if not _present(normalized):
            continue
        text = str(normalized).strip()
        if text.isdigit():
            return int(text)
        return normalized
    return None


def install(module: Any) -> None:
    if getattr(module, _INSTALLED_ATTR, False):
        return

    original_phase_of = getattr(module, "_phase_of", None)
    if callable(original_phase_of):
        @wraps(original_phase_of)
        def phase_of_with_shop_stage(item: dict):
            phase = original_phase_of(item)
            if _present(phase):
                return phase
            return _phase_from_payload(module, item)

        module._phase_of = phase_of_with_shop_stage

    original_get_repair_order = module.get_repair_order

    @wraps(original_get_repair_order)
    async def get_repair_order_with_phase_projection(settings: Any, args: dict):
        result = await original_get_repair_order(settings, args)
        if not isinstance(result, dict) or result.get("status") != "verified":
            return result
        repair_order = result.get("repair_order")
        raw = result.get("raw")
        if not isinstance(repair_order, dict) or not isinstance(raw, dict):
            return result
        if _present(repair_order.get("Phase")):
            return result
        phase = _phase_from_payload(module, raw)
        if not _present(phase):
            return result
        projected = dict(result)
        projected_ro = dict(repair_order)
        projected_ro["Phase"] = phase
        projected["repair_order"] = projected_ro
        return projected

    module.get_repair_order = get_repair_order_with_phase_projection

    # Install after calibration_iq_data_plane_guard so this wraps the final
    # status result.  The probe's count remains available inside that guard for
    # its zero-row integrity check; only the model-facing health result is
    # stripped of business-looking numbers.
    original_health = module.health

    @wraps(original_health)
    async def health_without_business_count(settings: Any):
        result = await original_health(settings)
        if not isinstance(result, dict):
            return result
        data_plane = result.get("data_plane")
        if not isinstance(data_plane, dict):
            return result
        projected = dict(result)
        probe = {
            key: item
            for key, item in data_plane.items()
            if key not in {"count", "sampled_rows"}
        }
        probe["purpose"] = "repair_order_data_plane_health_probe"
        probe["business_counts_included"] = False
        projected["data_plane"] = probe
        return projected

    module.health = health_without_business_count
    setattr(module, _INSTALLED_ATTR, True)
