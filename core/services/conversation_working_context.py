"""Bounded, source-owned working context for one conversation.

This module consumes structured tool results only. It does not inspect user
text, resolve pronouns, classify intent, or rewrite messages.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = 1
MAX_BYTES = 15_000
MAX_ITEMS = 8
_MISSING = object()

WORK_PREP_TOOL = "calibration_iq_work_prep"
WORK_LIST_TOOLS = frozenset({"calibration_iq_read", "calibration_iq_summary"})
SCRAPEX_TOOLS = frozenset({"scrapex_read", "scrapex_adas_map"})
ADAS_SI_TOOLS = frozenset({"adas_si_search", "adas_si_open"})
KNOWLEDGE_TOOLS = frozenset(
    {
        "automotive_knowledge_search",
        "automotive_knowledge_read",
        "automotive_knowledge_lifecycle",
    }
)
CIQ_TOOLS = frozenset(
    {
        "calibration_iq_ro",
        "calibration_iq_operator",
        "calibration_iq_destructive",
        "calibration_iq_update",
    }
)


def _dig(value: Any, *paths: str) -> Any:
    if not isinstance(value, dict):
        return None
    for path in paths:
        cursor: Any = value
        for part in path.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                cursor = None
                break
            cursor = cursor[part]
        if cursor not in (None, "", [], {}):
            return cursor
    return None


def _path(value: Any, path: str) -> Any:
    cursor = value
    for part in path.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return _MISSING
        cursor = cursor[part]
    return cursor


def _scalar(value: Any, limit: int = 240) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:limit] or None


def _safe_url(value: Any) -> Optional[str]:
    text = _scalar(value, 1_200)
    if not isinstance(text, str):
        return None
    parsed = urlsplit(text)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return None
    if parsed.scheme:
        host = parsed.hostname or ""
        if not host:
            return None
        port = f":{parsed.port}" if parsed.port is not None else ""
        # Credentials, query tokens, and fragments are never durable context.
        return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))
    if not text.startswith("/") or text.startswith("//"):
        return None
    return urlunsplit(("", "", parsed.path, "", ""))


def _record(value: Any, fields: dict[str, tuple[str, ...]]) -> Optional[dict[str, Any]]:
    if isinstance(value, str):
        label = _scalar(value, 180)
        return {"label": label} if label else None
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for target, paths in fields.items():
        raw = _dig(value, *paths)
        item = (
            _safe_url(raw)
            if target in {"url", "download_url", "source_url"}
            else _scalar(raw, 320 if target in {"reason", "requirement"} else 180)
        )
        if item is not None:
            result[target] = item
    return result or None


def _records(
    source: dict[str, Any],
    paths: tuple[str, ...],
    fields: dict[str, tuple[str, ...]],
) -> tuple[bool, list[dict[str, Any]], int]:
    present = False
    values: list[Any] = []
    for path in paths:
        raw = _path(source, path)
        if raw is _MISSING:
            continue
        present = True
        if isinstance(raw, list):
            values.extend(raw)
        elif isinstance(raw, (dict, str)):
            values.append(raw)
    compact = [item for value in values if (item := _record(value, fields))]
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in compact:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return present, unique[:MAX_ITEMS], len(unique)


CALIBRATION_FIELDS = {
    "id": ("id", "uuid", "calibration_id"),
    "label": ("label", "name", "title", "calibration", "calibration_type"),
    "status": ("status", "state"),
    "determination": ("determination", "requirement", "required"),
    "method": ("method", "calibration_method"),
    "reason": ("reason", "description"),
    "active": ("active", "is_active"),
    "version": ("version", "revision"),
}
BLOCKER_FIELDS = {
    "id": ("id", "uuid", "blocker_id"),
    "title": ("title", "name", "label"),
    "status": ("status", "state"),
    "reason": ("reason", "description", "message"),
    "blocking": ("blocking", "is_blocking"),
    "resolved": ("resolved", "is_resolved"),
    "version": ("version", "revision"),
}
DOCUMENT_FIELDS = {
    "id": ("id", "uuid", "document_id"),
    "title": ("title", "name", "filename"),
    "document_type": ("document_type", "type", "kind"),
    "status": ("status", "state"),
    "relative_path": ("relative_path", "path"),
    "download_url": ("download_url", "url"),
    "sha256": ("sha256", "content_sha256"),
    "version": ("version",),
}
WORKSPACE_FIELDS = {
    "id": ("id", "uuid"),
    "kind": ("kind", "type"),
    "title": ("title", "name"),
    "relative_path": ("relative_path", "path"),
    "download_url": ("download_url", "url"),
    "sha256": ("sha256", "content_sha256"),
    "version": ("version",),
}


def _section(
    owner: str,
    items: list[dict[str, Any]],
    count: int,
    *,
    version: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source_owner": owner,
        "authoritative": True,
        "count": count,
        "items": items,
        "items_truncated": count > len(items),
    }
    if (compact_version := _scalar(version, 80)) is not None:
        result["resource_version"] = compact_version
    result.update({key: item for key, item in extra.items() if item is not None})
    return result


def snapshot_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Extract only fields whose owner is the verified CIQ snapshot."""
    workflow = snapshot.get("workflow") if isinstance(snapshot.get("workflow"), dict) else {}
    repair_order = (
        snapshot.get("repair_order")
        if isinstance(snapshot.get("repair_order"), dict)
        else {}
    )
    version = _dig(workflow, "version") or _dig(repair_order, "version", "revision")
    sections: dict[str, Any] = {}
    specs = (
        (
            "calibrations",
            ("calibrations", "calibration_requirements", "requirements"),
            CALIBRATION_FIELDS,
        ),
        ("blockers", ("blockers", "blocking"), BLOCKER_FIELDS),
        (
            "documents",
            ("research.documents", "research_case.documents", "documents"),
            DOCUMENT_FIELDS,
        ),
        (
            "workspace",
            ("research.workspace", "research_case.workspace", "workspace"),
            WORKSPACE_FIELDS,
        ),
    )
    for name, paths, fields in specs:
        present, items, count = _records(snapshot, paths, fields)
        if not present:
            continue
        extra: dict[str, Any] = {}
        if name == "workspace":
            extra = {
                "research_state": _scalar(
                    _dig(snapshot, "research.state", "research_case.state"), 100
                ),
                "research_version": _scalar(
                    _dig(snapshot, "research.version", "research_case.version"), 80
                ),
            }
        sections[name] = _section(
            "calibration_iq", items, count, version=version, **extra
        )
    return {"schema_version": SCHEMA_VERSION, "sections": sections}


def _active_reference(subject: dict[str, Any]) -> dict[str, Any]:
    repair_order = (
        subject.get("repair_order")
        if isinstance(subject.get("repair_order"), dict)
        else {}
    )
    vehicle = subject.get("vehicle") if isinstance(subject.get("vehicle"), dict) else {}
    return {
        key: item
        for key, item in {
            "resource_id": _scalar(subject.get("resource_id"), 300),
            "repair_order_id": _scalar(
                subject.get("repair_order_id") or repair_order.get("id"), 300
            ),
            "ro_number": _scalar(
                subject.get("ro_number") or repair_order.get("ro_number"), 180
            ),
            "status": _scalar(repair_order.get("status"), 100),
            "phase": _scalar(repair_order.get("phase"), 100),
            "version": _scalar(repair_order.get("version"), 80),
            "vehicle": _scalar(vehicle.get("label"), 240),
            "identity_source_owner": _scalar(
                subject.get("identity_source_owner"), 120
            ),
        }.items()
        if item is not None
    }


def _size(value: dict[str, Any]) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


def finalize(subject: dict[str, Any]) -> dict[str, Any]:
    """Evolve legacy payloads lazily and enforce the store's size boundary."""
    value = deepcopy(subject)
    context = (
        value.get("working_context")
        if isinstance(value.get("working_context"), dict)
        else {}
    )
    context["schema_version"] = SCHEMA_VERSION
    value["context_schema_version"] = SCHEMA_VERSION
    value["working_context"] = context
    sections = context.get("sections") if isinstance(context.get("sections"), dict) else {}
    evidence = context.get("evidence") if isinstance(context.get("evidence"), dict) else {}
    if value.get("type") == "calibration_iq.repair_order":
        value.setdefault("identity_source_owner", "calibration_iq")
        prior_active = (
            context.get("active_repair_order")
            if isinstance(context.get("active_repair_order"), dict)
            else {}
        )
        active = _active_reference(value)
        if isinstance(prior_active.get("observation"), dict):
            active["observation"] = deepcopy(prior_active["observation"])
        context["active_repair_order"] = active
        rich = bool(sections or evidence)
        value["subject_scope"] = (
            "identity_workflow_and_authoritative_working_context"
            if rich
            else "identity_and_workflow_context_only"
        )
        value["current_calibration_detail_included"] = "calibrations" in sections
        if "calibrations" in sections:
            value.pop("next_capability_for_current_ro_detail", None)
        else:
            value["next_capability_for_current_ro_detail"] = "calibration_iq_ro"

    while _size(value) > MAX_BYTES:
        candidates: list[tuple[int, list[Any]]] = []

        def walk(item: Any) -> None:
            if isinstance(item, dict):
                for child in item.values():
                    walk(child)
            elif isinstance(item, list) and item:
                candidates.append((len(json.dumps(item, default=str)), item))

        walk(context)
        if not candidates:
            break
        max(candidates, key=lambda pair: pair[0])[1].pop()
        context["context_truncated"] = True
    return value


def attach_snapshot(subject: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(subject)
    value["identity_source_owner"] = "calibration_iq"
    value["working_context"] = snapshot_context(snapshot)
    return finalize(value)


def _identity_subject(
    *,
    repair_order_id: Any = None,
    ro_number: Any = None,
    vehicle_label: Any = None,
    context: Optional[dict[str, Any]] = None,
    identity_owner: str = "calibration_iq",
) -> Optional[dict[str, Any]]:
    exact_id = _scalar(repair_order_id, 300)
    number = _scalar(ro_number, 180)
    resource_id = exact_id or (f"ro-number:{number}" if number else None)
    if not resource_id:
        return None
    subject: dict[str, Any] = {
        "type": "calibration_iq.repair_order",
        "resource_id": str(resource_id),
        "repair_order_id": exact_id,
        "ro_number": number,
        "identity_source_owner": identity_owner,
        "repair_order": {
            key: item
            for key, item in {"id": exact_id, "ro_number": number}.items()
            if item is not None
        },
        "working_context": context or {"schema_version": SCHEMA_VERSION},
    }
    if (label := _scalar(vehicle_label, 240)) is not None:
        subject["vehicle"] = {"label": label}
    return finalize(subject)


def _scrapex_evidence(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    batch_item = data.get("item")
    provenance = data.get("provenance")
    if not isinstance(batch_item, dict) or not isinstance(provenance, dict):
        return None
    requirements = [
        compact
        for raw in provenance.get("requirements") or []
        if (
            compact := _record(
                raw,
                {
                    "label": ("label", "calibration_type", "name"),
                    "type": ("type", "requirement_type"),
                    "method": ("method", "calibration_method"),
                    "source": ("source",),
                },
            )
        )
    ]
    raw_result = provenance.get("raw_result")
    requirement_records = [
        compact
        for raw in (
            raw_result.get("requirement_records")
            if isinstance(raw_result, dict)
            else []
        )
        or []
        if (
            compact := _record(
                raw,
                {
                    "label": ("label",),
                    "source": ("source",),
                    "source_context": ("source_context",),
                    "runtime_id": ("source_context_runtime_id",),
                },
            )
        )
    ]
    reconciliation = provenance.get("ciq_reconciliation")
    compact_reconciliation = {
        key: compact_value
        for key in (
            "verified",
            "snapshot_verified",
            "receipt_count",
            "status",
            "resource_id",
            "repair_order_id",
        )
        if (compact_value := _scalar(
            reconciliation.get(key) if isinstance(reconciliation, dict) else None,
            180,
        ))
        is not None
    }
    result: dict[str, Any] = {
        "source_owner": "scrapex_adas_map",
        "authoritative": True,
        "batch": {
            key: item
            for key, item in {
                "id": _scalar(data.get("batch_id"), 300),
                "name": _scalar(data.get("batch_name"), 180),
                "state": _scalar(data.get("batch_state"), 100),
            }.items()
            if item is not None
        },
        "item": {
                key: item_value
                for key, item_value in {
                    "id": _scalar(batch_item.get("id"), 300),
                    "ro_number": _scalar(batch_item.get("ro_number"), 180),
                    "state": _scalar(
                        batch_item.get("adas_map_state")
                        or batch_item.get("status"),
                        100,
                    ),
            }.items()
            if item_value is not None
        },
        "contract_version": _scalar(provenance.get("contract_version"), 40),
        "state": _scalar(provenance.get("state"), 100),
        "requirements_proven": provenance.get("requirements_proven") is True,
        "inspection_id": _scalar(provenance.get("inspection_id"), 240),
        "checked_at": _scalar(provenance.get("checked_at"), 100),
        "ciq_reconciliation_state": _scalar(
            provenance.get("ciq_reconciliation_state"), 100
        ),
        "requirements": requirements[:MAX_ITEMS],
        "requirements_count": len(requirements),
        "requirement_records": requirement_records[:MAX_ITEMS],
        "requirement_records_count": len(requirement_records),
        "ciq_reconciliation": compact_reconciliation,
    }
    if (source_url := _safe_url(provenance.get("source_url"))) is not None:
        result["source_url"] = source_url
    return result


def _scrapex_subject(tool_name: str, result: dict[str, Any]) -> Optional[dict[str, Any]]:
    action = str(result.get("action") or "")
    if tool_name == "scrapex_read":
        valid = (
            action == "batch_item"
            and result.get("status") == "verified"
            and result.get("success") is True
            and result.get("verified") is True
        )
    else:
        valid = (
            action == "process_one"
            and result.get("status") == "completed"
            and result.get("success") is True
            and result.get("executed") is True
            and result.get("verified") is True
            and result.get("work_complete") is True
        )
    if not valid or not isinstance(result.get("data"), dict):
        return None
    data = result["data"]
    item = data.get("item")
    if not isinstance(item, dict):
        return None
    batch_id = _scalar(data.get("batch_id"), 300)
    ro_number = _scalar(item.get("ro_number") or data.get("ro_number"), 180)
    if not batch_id or not ro_number:
        return None
    if data.get("ro_number") not in (None, "", ro_number):
        return None
    if item.get("batch_id") not in (None, "", batch_id):
        return None
    evidence = _scrapex_evidence(data)
    if evidence is None:
        return None
    return _identity_subject(
        repair_order_id=_dig(
            item, "repair_order_id", "ciq_repair_order_id", "ro_id"
        ),
        ro_number=ro_number,
        identity_owner="scrapex_adas_map",
        context={
            "schema_version": SCHEMA_VERSION,
            "evidence": {"scrapex_adas_map": evidence},
        },
    )


def _weekly_item(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    item: dict[str, Any] = {
        key: compact
        for key, compact in {
            "repair_order_id": _scalar(value.get("repair_order_id"), 300),
            "ro_number": _scalar(value.get("ro_number"), 180),
            "vehicle": _scalar(
                value.get("vehicle") or value.get("vehicle_label"), 240
            ),
            "status": _scalar(value.get("status") or value.get("item_status"), 100),
            "ready": (
                value.get("ready") if isinstance(value.get("ready"), bool) else None
            ),
            "coverage_status": _scalar(value.get("coverage_status"), 100),
            "adas_map_status": _scalar(_dig(value, "adas_map.status"), 100),
            "category": _scalar(value.get("category"), 80),
            "attempts": (
                value.get("attempts")
                if isinstance(value.get("attempts"), int)
                else None
            ),
        }.items()
        if compact is not None
    }
    for source_key, target_key in (
        ("missing_si", "missing_calibrations"),
        ("missing_calibrations", "missing_calibrations"),
        ("unverified_si", "unverified_calibrations"),
        ("unverified_calibrations", "unverified_calibrations"),
    ):
        labels = [
            label
            for raw in value.get(source_key) or []
            if (
                label := _scalar(
                    raw.get("calibration") if isinstance(raw, dict) else raw, 140
                )
            )
        ]
        if labels and target_key not in item:
            item[target_key] = labels[:6]
    return item or None


def _weekly_section(result: dict[str, Any]) -> dict[str, Any]:
    section: dict[str, Any] = {
        "source_owner": "calibration_iq_work_prep",
        "authoritative": True,
    }
    for key in (
        "mode",
        "status",
        "readiness_complete",
        "queue_count",
        "ready_count",
        "exception_count",
        "needs_si_count",
        "si_missing_count",
        "si_unverified_count",
        "adas_map_verified_count",
        "adas_map_missing_count",
        "adas_map_unverified_count",
        "reconciliation_failed_count",
        "ro_unavailable_count",
        "completed_count",
        "unresolved_count",
        "actionable_count",
        "failure_count",
        "done_count",
        "total_count",
        "stale",
        "acquisition_status",
        "queue_persistence_status",
        "queue_persistence_verified",
        "repair_orders_total",
        "repair_orders_shown",
        "repair_orders_truncated",
    ):
        if (value := _scalar(result.get(key), 180)) is not None:
            section[key] = value
    phases = [_scalar(value, 40) for value in result.get("phase_scope") or []]
    section["phase_scope"] = [value for value in phases if value is not None][:12]
    filters = result.get("filters")
    if isinstance(filters, dict):
        section["filters"] = {
            key: value
            for key in ("phase", "shop", "include_completed")
            if (value := _scalar(filters.get(key), 180)) is not None
        }
    if isinstance(result.get("status_counts"), dict):
        section["status_counts"] = {
            str(key)[:80]: value
            for key, value in result["status_counts"].items()
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        }
    rows = result.get("repair_orders")
    if not isinstance(rows, list):
        rows = result.get("items") if isinstance(result.get("items"), list) else []
    items = [item for row in rows if (item := _weekly_item(row))]
    items.sort(key=lambda item: item.get("ready") is True)
    section["items"] = items[:MAX_ITEMS]
    section["items_count"] = len(items)
    section["items_truncated"] = len(items) > MAX_ITEMS
    return section


def _work_list_row(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    return {
        key: item
        for key, item in {
            "repair_order_id": _scalar(
                _dig(value, "id", "repair_order_id", "uuid"), 300
            ),
            "ro_number": _scalar(
                _dig(value, "RO", "ro_number", "roNumber", "number", "ro"),
                180,
            ),
            "vehicle": _scalar(
                _dig(value, "Vehicle", "vehicle_description", "vehicle_display"),
                240,
            ),
            "status": _scalar(_dig(value, "Status", "status"), 100),
            "phase": _scalar(_dig(value, "Phase", "phase"), 100),
            "shop": _scalar(_dig(value, "Shop", "shop.name", "shop_name"), 180),
            "version": _scalar(_dig(value, "version", "revision"), 80),
        }.items()
        if item is not None
    } or None


def _work_list_section(result: dict[str, Any]) -> dict[str, Any]:
    section: dict[str, Any] = {
        "source_owner": "calibration_iq",
        "authoritative": True,
    }
    for key in (
        "mode",
        "status",
        "scope",
        "result_scope",
        "count",
        "shown_count",
        "active_count",
        "completed_count",
        "upstream_total",
        "duplicate_count",
        "collection_complete",
        "collection_capped",
        "truncated",
        "summary_only",
        "exact_ro_detail_included",
    ):
        if (item := _scalar(result.get(key), 180)) is not None:
            section[key] = item
    filters = result.get("filters")
    if isinstance(filters, dict):
        section["filters"] = {
            key: item
            for key in (
                "q",
                "shop",
                "insurance",
                "status",
                "phase",
                "include_completed",
                "terminal_only",
            )
            if (item := _scalar(filters.get(key), 180)) is not None
        }
    rows = result.get("rows")
    if not isinstance(rows, list):
        rows = result.get("items") if isinstance(result.get("items"), list) else []
    references = [
        reference for row in rows if (reference := _work_list_row(row)) is not None
    ]
    section["references"] = references[:MAX_ITEMS]
    section["references_count"] = len(references)
    section["references_truncated"] = len(references) > MAX_ITEMS
    return section


def _work_list_subject(result: dict[str, Any]) -> Optional[dict[str, Any]]:
    if result.get("status") != "verified":
        return None
    scope = _scalar(result.get("scope"), 100) or "active"
    filters = result.get("filters") if isinstance(result.get("filters"), dict) else {}
    phase = _scalar(filters.get("phase"), 80) or "all-phases"
    shop = _scalar(filters.get("shop"), 100) or "all-shops"
    return finalize(
        {
            "type": "calibration_iq.work_list",
            "resource_id": f"work-list:{scope}:{phase}:{shop}"[:300],
            "subject_scope": "conversation_work_list_context",
            "working_context": {
                "schema_version": SCHEMA_VERSION,
                "sections": {"work_list": _work_list_section(result)},
            },
        }
    )


def _work_prep_subject(result: dict[str, Any]) -> Optional[dict[str, Any]]:
    mode = str(result.get("mode") or "")
    if mode == "phase_list":
        return _work_list_subject(result)
    if mode in {"week_readiness", "phase_coverage", "queue_list"}:
        if result.get("verified") is not True or result.get("status") in {
            "failed",
            "invalid_request",
            "context_missing",
        }:
            return None
        phases = "-".join(str(value)[:40] for value in result.get("phase_scope") or [])
        shop = _scalar(_dig(result, "filters.shop"), 100) or "all-shops"
        return finalize(
            {
                "type": "calibration_iq.weekly_readiness",
                "resource_id": f"weekly:{mode}:{phases or 'all-phases'}:{shop}"[:300],
                "subject_scope": "conversation_weekly_work_context",
                "working_context": {
                    "schema_version": SCHEMA_VERSION,
                    "sections": {"weekly": _weekly_section(result)},
                },
            }
        )
    if mode not in {"ro_requirements", "queue_next"}:
        return None
    if not (
        result.get("status") == "success"
        and result.get("success") is True
        and result.get("verified") is True
    ):
        return None
    if mode == "ro_requirements" and result.get("snapshot_verified") is not True:
        return None
    repair_order_id = _scalar(result.get("repair_order_id"), 300)
    ro_number = _scalar(result.get("ro_number"), 180)
    if not repair_order_id and not ro_number:
        return None
    sections: dict[str, Any] = {}
    requirements = result.get("calibration_requirements")
    if isinstance(requirements, list):
        items = [
            item
            for raw in requirements
            if (item := _record(raw, CALIBRATION_FIELDS))
        ]
        sections["calibrations"] = _section(
            "calibration_iq", items[:MAX_ITEMS], len(items)
        )
    if mode == "queue_next":
        sections["weekly"] = _weekly_section(result)
    return _identity_subject(
        repair_order_id=repair_order_id,
        ro_number=ro_number,
        vehicle_label=result.get("vehicle"),
        context={"schema_version": SCHEMA_VERSION, "sections": sections},
    )


def non_ciq_subject(tool_name: str, result: Any) -> Optional[dict[str, Any]]:
    if not isinstance(result, dict):
        return None
    if tool_name == WORK_PREP_TOOL:
        return _work_prep_subject(result)
    if tool_name in WORK_LIST_TOOLS:
        return _work_list_subject(result)
    if tool_name in SCRAPEX_TOOLS:
        return _scrapex_subject(tool_name, result)
    return None


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _year(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _vehicle_matches(subject: dict[str, Any], candidate: Any) -> bool:
    vehicle = subject.get("vehicle")
    if not isinstance(vehicle, dict) or not isinstance(candidate, dict):
        return False
    subject_year = _year(vehicle.get("year"))
    return bool(
        subject_year is not None
        and _year(candidate.get("year")) == subject_year
        and _normalized(vehicle.get("make"))
        and _normalized(candidate.get("make") or candidate.get("manufacturer"))
        == _normalized(vehicle.get("make"))
        and _normalized(vehicle.get("model"))
        and _normalized(candidate.get("model")) == _normalized(vehicle.get("model"))
    )


def _adas_si_patch(
    tool_name: str, result: dict[str, Any], subject: dict[str, Any]
) -> Optional[dict[str, Any]]:
    if str(result.get("status") or "") not in {
        "success",
        "partial_success",
        "verified",
    }:
        return None
    documents: list[dict[str, Any]] = []
    query_context: dict[str, Any] = {}
    if tool_name == "adas_si_search":
        structured = result.get("structured_query")
        if not isinstance(structured, dict) or not _vehicle_matches(
            subject, structured.get("vehicle")
        ):
            return None
        if result.get("exact_source_matched") is not True:
            return None
        for key in (
            "system",
            "component",
            "repair_event",
            "requirement_type",
            "search_mode",
        ):
            if (value := _scalar(structured.get(key), 180)) is not None:
                query_context[key] = value
        rows: list[dict[str, Any]] = []
        for row in result.get("matched_documents") or []:
            if isinstance(row, dict) and _vehicle_matches(subject, row):
                rows.append(row)
        for row in result.get("results") or []:
            if isinstance(row, dict) and _vehicle_matches(
                subject, row.get("vehicle")
            ):
                rows.append(row)
        documents = [
            document
            for row in rows
            if (
                document := _record(
                    row,
                    {
                        "title": ("title", "source", "document"),
                        "relative_path": ("relative_path",),
                        "url": ("url",),
                        "page": ("page",),
                        "pages_total": ("pages_total",),
                        "topic": ("topic", "vehicle.topic"),
                        "source_match_score": ("source_match_score",),
                    },
                )
            )
        ]
    else:
        document = result.get("document")
        if not isinstance(document, dict) or not _vehicle_matches(
            subject, document.get("vehicle")
        ):
            return None
        compact = _record(
            document,
            {
                "title": ("title",),
                "relative_path": ("relative_path",),
                "url": ("url",),
                "page": ("page",),
                "pages_total": ("pages_total",),
            },
        )
        if compact:
            documents = [compact]
    if not documents:
        return None
    return {
        "schema_version": SCHEMA_VERSION,
        "evidence": {
            "adas_si": {
                "source_owner": "adas_si",
                "authoritative_source_documents": True,
                "application_bound": True,
                "query_context": query_context,
                "documents": documents[:MAX_ITEMS],
                "documents_count": len(documents),
                "documents_truncated": len(documents) > MAX_ITEMS,
            }
        },
    }


def _knowledge_matches(subject: dict[str, Any], record: Any) -> bool:
    if not isinstance(record, dict) or record.get("lifecycle") != "verified":
        return False
    integrity = record.get("source_integrity")
    if not (
        isinstance(integrity, dict)
        and integrity.get("status") == "current"
        and integrity.get("verified_read_allowed") is True
    ):
        return False
    application = record.get("application")
    vehicle = subject.get("vehicle")
    if not isinstance(application, dict) or not isinstance(vehicle, dict):
        return False
    year = _year(vehicle.get("year"))
    start = _year(application.get("year_start") or application.get("year"))
    end = _year(application.get("year_end") or application.get("year"))
    if year is None or start is None or end is None or not start <= year <= end:
        return False
    if _normalized(
        application.get("manufacturer") or application.get("make")
    ) != _normalized(vehicle.get("make")):
        return False
    if _normalized(application.get("model")) != _normalized(vehicle.get("model")):
        return False
    app_trim = _normalized(application.get("trim"))
    return not (
        app_trim
        and _normalized(vehicle.get("trim"))
        and app_trim != _normalized(vehicle.get("trim"))
    )


def _knowledge_record(record: dict[str, Any]) -> dict[str, Any]:
    requirement = record.get("requirement") if isinstance(record.get("requirement"), dict) else {}
    system = record.get("system") if isinstance(record.get("system"), dict) else {}
    component = record.get("component") if isinstance(record.get("component"), dict) else {}
    event = record.get("repair_event") if isinstance(record.get("repair_event"), dict) else {}
    evidence: list[dict[str, Any]] = []
    for item in record.get("evidence") or []:
        if not (
            isinstance(item, dict)
            and item.get("verification_effective") is True
            and item.get("verification_status") == "verified"
        ):
            continue
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        evidence.append(
            {
                key: value
                for key, value in {
                    "id": _scalar(item.get("id"), 180),
                    "page_start": _scalar(item.get("page_start"), 40),
                    "page_end": _scalar(item.get("page_end"), 40),
                    "section": _scalar(item.get("section"), 220),
                    "document_id": _scalar(source.get("document_id"), 180),
                    "source_name": _scalar(source.get("source_name"), 220),
                    "content_sha256": _scalar(source.get("content_sha256"), 80),
                }.items()
                if value is not None
            }
        )
    return {
        key: value
        for key, value in {
            "id": _scalar(record.get("id"), 180),
            "version": _scalar(record.get("version"), 40),
            "system": _scalar(system.get("name"), 180),
            "component": _scalar(component.get("name"), 180),
            "repair_event": _scalar(event.get("description"), 240),
            "requirement_type": _scalar(requirement.get("requirement_type"), 100),
            "requirement": _scalar(requirement.get("text"), 320),
            "calibration_type": _scalar(requirement.get("calibration_type"), 100),
            "inspection_required": (
                requirement.get("inspection_required")
                if isinstance(requirement.get("inspection_required"), bool)
                else None
            ),
            "evidence": evidence[:4],
            "evidence_count": len(evidence),
        }.items()
        if value is not None
    }


def _knowledge_patch(
    result: dict[str, Any], subject: dict[str, Any]
) -> Optional[dict[str, Any]]:
    if str(result.get("status") or "") not in {"success", "verified"}:
        return None
    records = result.get("records")
    if not isinstance(records, list):
        records = [result.get("record")] if isinstance(result.get("record"), dict) else []
    compact = [_knowledge_record(record) for record in records if _knowledge_matches(subject, record)]
    if not compact:
        return None
    return {
        "schema_version": SCHEMA_VERSION,
        "evidence": {
            "durable_automotive_knowledge": {
                "source_owner": "durable_automotive_knowledge",
                "verified_hashed_sources_only": True,
                "application_bound": True,
                "records": compact[:4],
                "records_count": len(compact),
                "records_truncated": len(compact) > 4,
            }
        },
    }


def _enrichment(
    tool_name: str, result: dict[str, Any], current: Optional[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    if (
        not isinstance(current, dict)
        or current.get("type") != "calibration_iq.repair_order"
    ):
        return None
    if tool_name in ADAS_SI_TOOLS:
        return _adas_si_patch(tool_name, result, current)
    if tool_name in KNOWLEDGE_TOOLS:
        return _knowledge_patch(result, current)
    return None


def _stamp(
    context: dict[str, Any],
    tool_name: str,
    result: dict[str, Any],
    tool_call_id: Optional[str],
) -> dict[str, Any]:
    value = deepcopy(context)
    observation = {
        key: item
        for key, item in {
            "tool_name": _scalar(tool_name, 160),
            "tool_call_id": _scalar(tool_call_id, 300),
            "result_status": _scalar(result.get("status"), 100),
            "evidence_id": _scalar(result.get("evidence_id"), 240),
            "observed_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
        }.items()
        if item is not None
    }
    for bucket in ("sections", "evidence"):
        entries = value.get(bucket)
        if isinstance(entries, dict):
            for entry in entries.values():
                if isinstance(entry, dict):
                    entry["observation"] = observation
    active = value.get("active_repair_order")
    if isinstance(active, dict):
        active["observation"] = observation
    return value


def _merge_context(base: Any, update: Any) -> dict[str, Any]:
    result = deepcopy(base) if isinstance(base, dict) else {}
    patch = update if isinstance(update, dict) else {}
    result["schema_version"] = SCHEMA_VERSION
    for bucket in ("sections", "evidence"):
        old = result.get(bucket) if isinstance(result.get(bucket), dict) else {}
        new = patch.get(bucket) if isinstance(patch.get(bucket), dict) else {}
        if old or new:
            result[bucket] = {**deepcopy(old), **deepcopy(new)}
    return result


def _same_ro(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if not (
        left.get("type") == "calibration_iq.repair_order"
        and right.get("type") == "calibration_iq.repair_order"
    ):
        return False
    left_id = _normalized(left.get("repair_order_id") or _dig(left, "repair_order.id"))
    right_id = _normalized(right.get("repair_order_id") or _dig(right, "repair_order.id"))
    if left_id and right_id:
        return left_id == right_id
    left_number = _normalized(left.get("ro_number") or _dig(left, "repair_order.ro_number"))
    right_number = _normalized(right.get("ro_number") or _dig(right, "repair_order.ro_number"))
    return bool(left_number and right_number and left_number == right_number)


def _version(subject: dict[str, Any]) -> Optional[float]:
    value = _dig(subject, "repair_order.version")
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _merge_subject(
    current: Optional[dict[str, Any]], incoming: dict[str, Any], tool_name: str
) -> dict[str, Any]:
    if not isinstance(current, dict):
        return finalize(incoming)
    if incoming.get("type") in {
        "calibration_iq.weekly_readiness",
        "calibration_iq.work_list",
    }:
        if current.get("type") == "calibration_iq.repair_order":
            result = deepcopy(current)
            result["working_context"] = _merge_context(
                current.get("working_context"), incoming.get("working_context")
            )
            return finalize(result)
        result = deepcopy(incoming)
        result["working_context"] = _merge_context(
            current.get("working_context"), incoming.get("working_context")
        )
        return finalize(result)
    if _same_ro(current, incoming):
        old_version = _version(current)
        new_version = _version(incoming)
        if (
            tool_name in CIQ_TOOLS
            and old_version is not None
            and new_version is not None
            and new_version < old_version
        ):
            return deepcopy(current)
        result = deepcopy(current)
        for key, value in incoming.items():
            if key not in {"repair_order", "vehicle", "shop", "working_context"}:
                result[key] = deepcopy(value)
        for key in ("repair_order", "vehicle", "shop"):
            old = current.get(key) if isinstance(current.get(key), dict) else {}
            new = incoming.get(key) if isinstance(incoming.get(key), dict) else {}
            if old or new:
                result[key] = {**deepcopy(old), **deepcopy(new)}
        # Number-only sources cannot downgrade a known CIQ UUID.
        if not incoming.get("repair_order_id") and current.get("repair_order_id"):
            result["repair_order_id"] = current["repair_order_id"]
            result["resource_id"] = current["resource_id"]
            result["identity_source_owner"] = current.get(
                "identity_source_owner", "calibration_iq"
            )
            result["repair_order"]["id"] = current["repair_order_id"]
        result["working_context"] = _merge_context(
            current.get("working_context"), incoming.get("working_context")
        )
        return finalize(result)
    # Exact new RO replaces active identity, while global weekly context survives.
    result = deepcopy(incoming)
    current_sections = _dig(current, "working_context.sections")
    carried = {
        key: deepcopy(current_sections[key])
        for key in ("weekly", "work_list")
        if isinstance(current_sections, dict)
        and isinstance(current_sections.get(key), dict)
    }
    if carried:
        result["working_context"] = _merge_context(
            {"schema_version": SCHEMA_VERSION, "sections": carried},
            incoming.get("working_context"),
        )
    return finalize(result)


def persist_update(
    store: Any,
    *,
    conversation_id: int,
    tool_name: str,
    result: dict[str, Any],
    incoming: Optional[dict[str, Any]],
    tool_call_id: Optional[str],
    message_id: Optional[int],
    user_id: Optional[str],
) -> Optional[dict[str, Any]]:
    """Merge and persist a source update with one optimistic retry."""
    for attempt in range(2):
        row = store.get_conversation_subject(conversation_id, user_id=user_id)
        current = (
            row.get("payload")
            if isinstance(row, dict) and isinstance(row.get("payload"), dict)
            else None
        )
        subject = incoming or non_ciq_subject(tool_name, result)
        patch = _enrichment(tool_name, result, current)
        if subject is None and patch is None:
            return None
        if subject is not None:
            stamped = deepcopy(subject)
            stamped["working_context"] = _stamp(
                stamped.get("working_context") or {},
                tool_name,
                result,
                tool_call_id,
            )
            merged = _merge_subject(current, stamped, tool_name)
        else:
            assert current is not None and patch is not None
            merged = deepcopy(current)
            merged["working_context"] = _merge_context(
                current.get("working_context"),
                _stamp(patch, tool_name, result, tool_call_id),
            )
            merged = finalize(merged)
        if current is not None and merged == current:
            return None
        try:
            return store.set_conversation_subject(
                conversation_id,
                merged,
                source_tool_name=tool_name,
                source_tool_call_id=tool_call_id,
                source_message_id=message_id,
                user_id=user_id,
                expected_version=(
                    int(row["version"])
                    if isinstance(row, dict) and row.get("version") is not None
                    else None
                ),
            )
        except Exception as exc:
            if type(exc).__name__ != "ConversationSubjectConflict" or attempt:
                raise
    return None
