"""Structured active-subject tracking from authoritative tool results.

The tracker deliberately does not inspect user prose, pronouns, or keywords.
It accepts only result shapes that prove one exact Calibration IQ repair order,
then asks the state store to persist a compact subject for later prompt context.
"""

from __future__ import annotations

from typing import Any, Optional


_DIRECT_RO_TOOLS = frozenset({"calibration_iq_ro"})
_OPERATOR_TOOLS = frozenset(
    {
        "calibration_iq_operator",
        "calibration_iq_destructive",
        "calibration_iq_update",
    }
)


def _dig(item: Any, *paths: str) -> Any:
    if not isinstance(item, dict):
        return None
    for path in paths:
        cursor: Any = item
        for part in path.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                cursor = None
                break
            cursor = cursor[part]
        if cursor not in (None, "", [], {}):
            return cursor
    return None


def _scalar(value: Any, *, limit: int = 240) -> Any:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return " ".join(value.split())[:limit] or None
    return str(value)[:limit]


def _subject_from_snapshot(snapshot: dict[str, Any]) -> Optional[dict[str, Any]]:
    ro = snapshot.get("repair_order")
    if not isinstance(ro, dict):
        return None
    ro_id = _scalar(_dig(ro, "id", "repair_order_id", "uuid"), limit=300)
    ro_number = _scalar(_dig(ro, "ro_number", "roNumber", "number", "ro", "RO"))
    resource_id = ro_id or (f"ro-number:{ro_number}" if ro_number else None)
    if not resource_id:
        return None

    workflow = snapshot.get("workflow") if isinstance(snapshot.get("workflow"), dict) else {}
    repair_order: dict[str, Any] = {
        "id": ro_id,
        "ro_number": ro_number,
        "status": _scalar(_dig(workflow, "status") or _dig(ro, "status", "Status")),
        "phase": _scalar(_dig(workflow, "phase", "phase.name") or _dig(ro, "phase", "Phase")),
        "version": _scalar(_dig(workflow, "version") or _dig(ro, "version")),
    }
    repair_order = {key: value for key, value in repair_order.items() if value is not None}

    vehicle_source = ro.get("vehicle") if isinstance(ro.get("vehicle"), dict) else ro
    vehicle: dict[str, Any] = {
        "year": _scalar(_dig(vehicle_source, "year", "vehicle_year")),
        "make": _scalar(_dig(vehicle_source, "make", "manufacturer", "vehicle_make")),
        "model": _scalar(_dig(vehicle_source, "model", "vehicle_model")),
        "trim": _scalar(_dig(vehicle_source, "trim", "vehicle_trim")),
        "vin": _scalar(_dig(vehicle_source, "vin", "vehicle_vin")),
        "label": _scalar(
            _dig(ro, "vehicle_description", "vehicle_display", "Vehicle", "description")
        ),
    }
    vehicle = {key: value for key, value in vehicle.items() if value is not None}
    if "label" not in vehicle:
        label_parts = [
            vehicle.get("year"),
            vehicle.get("make"),
            vehicle.get("model"),
            vehicle.get("trim"),
        ]
        label = " ".join(str(value) for value in label_parts if value not in (None, ""))
        if label:
            vehicle["label"] = label[:240]

    shop_source = snapshot.get("shop", ro.get("shop"))
    shop: dict[str, Any] = {}
    if isinstance(shop_source, dict):
        shop = {
            "id": _scalar(_dig(shop_source, "id", "uuid")),
            "name": _scalar(_dig(shop_source, "name", "display_name")),
        }
        shop = {key: value for key, value in shop.items() if value is not None}
    elif shop_source not in (None, "", [], {}):
        shop = {"name": _scalar(shop_source)}
    elif _dig(ro, "Shop", "shop_name") is not None:
        shop = {"name": _scalar(_dig(ro, "Shop", "shop_name"))}

    subject: dict[str, Any] = {
        "type": "calibration_iq.repair_order",
        "resource_id": str(resource_id),
        "repair_order_id": ro_id,
        "ro_number": ro_number,
        "subject_scope": "identity_and_workflow_context_only",
        "current_calibration_detail_included": False,
        "next_capability_for_current_ro_detail": "calibration_iq_ro",
        "repair_order": repair_order,
    }
    subject = {key: value for key, value in subject.items() if value is not None}
    if vehicle:
        subject["vehicle"] = vehicle
    if shop:
        subject["shop"] = shop
    return subject


def subject_from_tool_result(
    tool_name: str,
    result: Any,
) -> Optional[dict[str, Any]]:
    """Extract one proven repair-order subject, or return ``None``.

    A multi-RO operator action is intentionally ambiguous and leaves the prior
    subject unchanged. Failed, partial, offline, and source-miss results never
    change conversation state.
    """
    name = str(tool_name or "").strip()
    if not isinstance(result, dict):
        return None

    if name in _DIRECT_RO_TOOLS:
        if result.get("status") != "verified":
            return None
        ro = result.get("repair_order")
        if not isinstance(ro, dict):
            return None
        raw = result.get("raw") if isinstance(result.get("raw"), dict) else {}
        snapshot = dict(raw)
        # The normalized summary has stable display fields while raw carries
        # vehicle/shop detail. Merge them without allowing raw to erase it.
        merged_ro = dict(raw.get("repair_order") or {}) if isinstance(raw.get("repair_order"), dict) else {}
        merged_ro.update(ro)
        snapshot["repair_order"] = merged_ro
        if "shop" not in snapshot and ro.get("Shop") not in (None, "", "-"):
            snapshot["shop"] = ro.get("Shop")
        return _subject_from_snapshot(snapshot)

    if name in _OPERATOR_TOOLS:
        if not (
            result.get("status") == "success"
            and result.get("success") is True
            and result.get("verified") is True
            and result.get("partial") is not True
        ):
            return None
        final = result.get("final_snapshots")
        if not isinstance(final, dict):
            return None
        verified_snapshots: list[dict[str, Any]] = []
        for item in final.values():
            if not isinstance(item, dict) or item.get("status") != "verified":
                continue
            snapshot = item.get("snapshot")
            if isinstance(snapshot, dict):
                verified_snapshots.append(snapshot)
        if len(verified_snapshots) != 1:
            return None
        return _subject_from_snapshot(verified_snapshots[0])

    return None


def track_active_subject_from_tool_result(
    store: Any,
    *,
    conversation_id: int,
    tool_name: str,
    result: Any,
    tool_call_id: Optional[str] = None,
    message_id: Optional[int] = None,
    user_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Persist the subject proven by one completed tool result.

    This is the single post-tool hook for the orchestrator. Returning ``None``
    means the result was not an authoritative, unambiguous subject update.
    """
    subject = subject_from_tool_result(tool_name, result)
    if subject is None:
        return None
    return store.set_conversation_subject(
        conversation_id,
        subject,
        source_tool_name=tool_name,
        source_tool_call_id=tool_call_id,
        source_message_id=message_id,
        user_id=user_id,
    )
