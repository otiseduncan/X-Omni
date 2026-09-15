"""Bind shared ADAS SI reuse and CIQ attachment to exact calibration identity.

A Year/Make/Model library is intentionally shared across repair orders.  That does
not make every procedure in that folder evidence for every calibration on the
vehicle.  ScrapeX capture sidecars already preserve the exact research objective
that produced an artifact, including ``calibration_item_id``.  This guard treats
that stored id as provenance, not semantics:

* a captured artifact for one CIQ calibration item is never auto-reused for a
  different calibration item merely because a reviewer likes the page;
* an existing CIQ document counts as attached for an objective only when that
  objective's calibration id is actually linked to the document;
* when one genuinely shared procedure is accepted for another calibration, the
  existing document is linked through CIQ's normal optimistic-concurrency
  ``link_document`` operation and then reread before success is reported.

Legacy/manual library files without captured objective provenance remain eligible
for X's independent semantic review.  Nothing here classifies page meaning.
"""

from __future__ import annotations

import asyncio
from functools import wraps
from typing import Any
from urllib.parse import quote

_INSTALLED_ATTR = "__xomni_si_provenance_guard_v1__"
RESEARCH_CONTRACT_VERSION = 2


def _text(value: Any, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


def captured_calibration_id(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    objective = metadata.get("objective")
    if not isinstance(objective, dict):
        return ""
    return _text(
        objective.get("calibration_item_id")
        or objective.get("calibration_id"),
        120,
    )


def current_calibration_id(objective: Any) -> str:
    if not isinstance(objective, dict):
        return ""
    return _text(
        objective.get("calibration_id")
        or objective.get("calibration_item_id"),
        120,
    )


def provenance_allows_reuse(metadata: Any, objective: Any) -> bool:
    """Fail only a proved captured-objective mismatch; legacy files stay reviewable."""
    captured = captured_calibration_id(metadata)
    current = current_calibration_id(objective)
    return not (captured and current and captured != current)


def _linked_ids(document: Any) -> set[str]:
    if not isinstance(document, dict):
        return set()
    return {
        _text(item, 120)
        for item in (document.get("calibration_item_ids") or [])
        if _text(item, 120)
    }


def _reset_legacy_active_record(record: dict[str, Any]) -> bool:
    """Re-run active jobs created before calibration-provenance binding existed."""
    if str(record.get("state") or "") != "running":
        return False
    try:
        version = int(record.get("research_contract_version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version >= RESEARCH_CONTRACT_VERSION:
        return False

    for objective in record.get("objectives") or []:
        if not isinstance(objective, dict):
            continue
        objective["status"] = "pending"
        for key in (
            "result",
            "attachments",
            "outcome",
            "started_at",
            "finished_at",
        ):
            objective.pop(key, None)
    record["research_contract_version"] = RESEARCH_CONTRACT_VERSION
    record["state"] = "running"
    record["notified"] = False
    for key in ("result", "error", "finished_at", "result_message_id"):
        record.pop(key, None)
    return True


def install(cascade_module: Any, research_module: Any) -> None:
    if getattr(research_module, _INSTALLED_ATTR, False):
        return

    # ---------------------------------------------------------------- local reuse
    original_review_local = cascade_module._review_local

    @wraps(original_review_local)
    async def review_local_with_provenance(
        service: Any,
        objective: dict[str, Any],
        row: dict[str, Any],
    ):
        if service.adas is not None:
            try:
                path = service.adas.resolve_relative(row["relative_path"])
                metadata = await asyncio.to_thread(cascade_module._source_metadata, path)
            except Exception:  # noqa: BLE001 - original path owns fallback/logging
                metadata = {}
            if not provenance_allows_reuse(metadata, objective):
                return None
        return await original_review_local(service, objective, row)

    cascade_module._review_local = review_local_with_provenance

    # ---------------------------------------------------------- truthful CIQ linking
    original_attach_factory = research_module.default_attach

    def default_attach_with_exact_link(settings: Any, adas: Any):
        original_attach = original_attach_factory(settings, adas)

        async def attach(
            objective: dict[str, Any],
            document: dict[str, Any],
            context: dict[str, Any],
        ) -> dict[str, Any]:
            from . import calibration_iq

            artifact = document.get("artifact") if isinstance(document, dict) else {}
            artifact = artifact if isinstance(artifact, dict) else {}
            relative = str(artifact.get("relative_path") or "").strip().replace("\\", "/")
            calibration_id = current_calibration_id(objective)
            ro_id = _text(objective.get("repair_order_id"), 160)
            if not relative or not calibration_id or not ro_id:
                return await original_attach(objective, document, context)

            source_uri = f"adas-si:///{quote(relative)}"
            before = await calibration_iq.get_repair_order(
                settings, {"repair_order_id": ro_id}
            )
            if before.get("status") != "verified" or not isinstance(before.get("raw"), dict):
                return await original_attach(objective, document, context)

            existing = calibration_iq._existing_research_documents(before["raw"])  # noqa: SLF001
            already = next(
                (
                    item
                    for item in existing
                    if str(item.get("source_uri") or "").strip().casefold()
                    == source_uri.casefold()
                ),
                None,
            )
            if already is None or calibration_id in _linked_ids(already):
                return await original_attach(objective, document, context)

            document_id = _text(
                already.get("id") or already.get("document_id"), 160
            )
            if not document_id:
                return {
                    "attached": False,
                    "status": "existing_document_unidentified",
                    "source_uri": source_uri,
                    "calibration_item_id": calibration_id,
                }
            try:
                expected_version = calibration_iq._existing_document_version(  # noqa: SLF001
                    already, operation="link_document"
                )
            except Exception as exc:  # noqa: BLE001 - fail closed on missing concurrency proof
                return {
                    "attached": False,
                    "status": "existing_document_unversioned",
                    "document_id": document_id,
                    "source_uri": source_uri,
                    "calibration_item_id": calibration_id,
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }

            digest = _text(artifact.get("sha256"), 64)[:12] or "library"
            invocation = dict(context)
            invocation["tool_call_id"] = (
                f"{context.get('tool_call_id') or 'adas_si_research'}:"
                f"{objective.get('objective_id')}:link:{digest}"
            )
            result = await calibration_iq.operator_execute(
                settings,
                adas,
                {
                    "actions": [
                        {
                            "operation": "link_document",
                            "target_id": document_id,
                            "expected_version": expected_version,
                            "arguments": {
                                "calibration_item_ids": [calibration_id],
                                "evidence_role": "PROCEDURE",
                            },
                        }
                    ],
                    research_module.INVOCATION_KEY: invocation,
                },
            )

            after = await calibration_iq.get_repair_order(
                settings, {"repair_order_id": ro_id}
            )
            linked_document = None
            if after.get("status") == "verified" and isinstance(after.get("raw"), dict):
                for item in calibration_iq._existing_research_documents(after["raw"]):  # noqa: SLF001
                    if (
                        str(item.get("source_uri") or "").strip().casefold()
                        == source_uri.casefold()
                        and calibration_id in _linked_ids(item)
                    ):
                        linked_document = item
                        break
            return {
                "attached": linked_document is not None,
                "status": "linked_existing" if linked_document is not None else "existing_not_linked",
                "document_id": (
                    (linked_document or {}).get("id")
                    or (linked_document or {}).get("document_id")
                    or document_id
                ),
                "source_uri": source_uri,
                "calibration_item_id": calibration_id,
                "receipt_status": result.get("status") if isinstance(result, dict) else None,
                "receipt_message": _text(
                    result.get("message") if isinstance(result, dict) else "", 300
                ) or None,
            }

        return attach

    research_module.default_attach = default_attach_with_exact_link

    # ------------------------------------------------------------- active-job migration
    service_class = research_module.AdasSiResearchService
    original_save = service_class._save
    original_resume = service_class.resume

    @wraps(original_save)
    def save_with_contract(self: Any, record: dict[str, Any]):
        record["research_contract_version"] = RESEARCH_CONTRACT_VERSION
        return original_save(self, record)

    @wraps(original_resume)
    async def resume_with_revalidation(self: Any) -> int:
        for record in self._records(None):
            if _reset_legacy_active_record(record):
                original_save(self, record)
        return await original_resume(self)

    service_class._save = save_with_contract
    service_class.resume = resume_with_revalidation
    setattr(research_module, _INSTALLED_ATTR, True)
