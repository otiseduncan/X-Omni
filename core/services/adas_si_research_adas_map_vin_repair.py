"""Repair a missing CIQ VIN from authoritative ADAS Map evidence before SI.

The SI workflow is VIN-bound. Older Calibration IQ repair orders can have the
ADAS Map attached while the VIN field itself is empty. Recovery is intentionally
mechanical and exact:

1. Prefer ScrapeX's stored ADAS Map identity proof when it still exists.
2. Otherwise resolve the exact Calibration IQ operator snapshot, fetch only
   attached ADAS Map documents, and read those verified managed PDFs with X
   Omni's existing PDF/OCR extractor.
3. Accept only one unambiguous 17-character VIN bound to that exact RO. If
   multiple readable maps disagree, nothing is changed.
4. Persist only ``vin`` through Calibration IQ's normal optimistic-concurrency
   operator path, then reread authoritative state before SI may continue.

No year/make/model fallback is introduced and no page meaning is delegated to
ScrapeX. The only inference here is mechanical vehicle identity from the
governing ADAS Map already attached to the exact repair order.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from contextvars import ContextVar
from functools import wraps
from typing import Any


log = logging.getLogger(__name__)
_INSTALLED_ATTR = "__x_adas_map_vin_repair_v3__"
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_VIN_IN_TEXT = re.compile(
    r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])", re.IGNORECASE
)
_VIN_LABEL_RE = re.compile(
    r"\bVIN\b[\s:#-]{0,12}([A-HJ-NPR-Z0-9]{17})(?![A-Z0-9])",
    re.IGNORECASE,
)
_MAX_MAP_DOCUMENTS = 6

# ``research_si`` resolves the RO synchronously inside ``start`` before it
# launches the background driver. A ContextVar carries that already-authorized
# chat invocation into the default RO reader without putting internal fields on
# Calibration IQ read requests or sharing state between concurrent conversations.
_INVOCATION_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "adas_si_vin_repair_invocation", default=None
)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _ro_key(value: Any) -> str:
    return "".join(_text(value).split()).casefold()


def _ro_number(read: dict[str, Any]) -> str:
    summary = _mapping(read.get("repair_order"))
    raw = _mapping(read.get("raw"))
    raw_ro = _mapping(raw.get("repair_order"))
    for value in (
        summary.get("RO"),
        summary.get("ro_number"),
        summary.get("number"),
        raw_ro.get("ro_number"),
        raw_ro.get("RO"),
        raw_ro.get("number"),
    ):
        result = _text(value)
        if result:
            return result
    return ""


def _snapshot_ro_number(snapshot: dict[str, Any]) -> str:
    ro = _mapping(snapshot.get("repair_order"))
    for value in (ro.get("ro_number"), ro.get("RO"), ro.get("number")):
        result = _text(value)
        if result:
            return result
    return ""


def _snapshot_vin(snapshot: dict[str, Any]) -> str:
    ro = _mapping(snapshot.get("repair_order"))
    vehicle = _mapping(snapshot.get("vehicle"))
    for value in (ro.get("vin"), vehicle.get("vin")):
        candidate = _text(value).upper()
        if _VIN_RE.fullmatch(candidate):
            return candidate
    return ""


def _snapshot_version(snapshot: dict[str, Any]) -> int:
    ro = _mapping(snapshot.get("repair_order"))
    for value in (ro.get("version"), snapshot.get("version")):
        try:
            version = int(value)
        except (TypeError, ValueError):
            continue
        if version >= 1:
            return version
    return 0


def _document_label(document: dict[str, Any]) -> str:
    return " ".join(
        _text(document.get(key))
        for key in (
            "title",
            "source_name",
            "original_filename",
            "source_uri",
            "citation",
        )
        if _text(document.get(key))
    )


def _is_adas_map_document(document: dict[str, Any]) -> bool:
    typed = any(
        _text(document.get(key)).casefold() == "adas_map_report"
        for key in ("semantic_type", "document_type", "storage_class")
    )
    return typed or "adas map" in _document_label(document).casefold()


def _document_ro_bound(
    document: dict[str, Any], ro_number: str, text: str
) -> bool:
    expected = _ro_key(ro_number)
    if not expected:
        return False
    explicit = _ro_key(document.get("ro_number"))
    if explicit and explicit == expected:
        return True
    metadata = _ro_key(_document_label(document))
    if expected in metadata:
        return True
    return expected in _ro_key(text)


def _vin_candidates(text: str) -> set[str]:
    upper = str(text or "").upper()
    labeled = {
        match.group(1).upper()
        for match in _VIN_LABEL_RE.finditer(upper)
        if _VIN_RE.fullmatch(match.group(1).upper())
    }
    if labeled:
        return labeled
    return {
        match.group(0).upper()
        for match in _VIN_IN_TEXT.finditer(upper)
        if _VIN_RE.fullmatch(match.group(0).upper())
    }


def _extract_pdf_text(raw: bytes, filename: str) -> str:
    """Use X Omni's existing bounded PDF/PDFium/OCR attachment reader."""
    from . import attachments

    accepted = attachments.accept(raw, filename)
    if accepted.kind != attachments.KIND_PDF:
        return ""
    extracted = attachments._extract_pdf(accepted)  # noqa: SLF001
    return str(extracted.text or "")


async def _repair_from_proven_adas_map(settings: Any, ro_number: str) -> bool:
    """First try ScrapeX's persisted, mechanically proven ADAS Map identity."""
    from . import scrapex

    try:
        startup = await scrapex.start_native(settings)
        if not isinstance(startup, dict) or startup.get("success") is not True:
            return False
        payload = await scrapex._request(  # noqa: SLF001 - internal service composite
            settings,
            "POST",
            "/api/adas-map/repair-proven-vins",
            body={"ro_numbers": [ro_number]},
            timeout=scrapex.OPERATOR_TIMEOUT,
            may_mutate=True,
        )
    except Exception:  # noqa: BLE001 - failure leaves the VIN-only guard closed
        log.warning(
            "Stored ADAS Map VIN repair was unavailable for RO %s",
            ro_number,
            exc_info=True,
        )
        return False

    if not isinstance(payload, dict):
        return False
    results = payload.get("results")
    if (
        payload.get("requested_count") != 1
        or payload.get("repaired_count") != 1
        or not isinstance(results, list)
        or len(results) != 1
    ):
        return False
    result = _mapping(results[0])
    return bool(
        _text(result.get("ro_number")) == ro_number
        and _text(result.get("status")).casefold() == "repaired"
        and _VIN_RE.fullmatch(_text(result.get("vin")).upper())
    )


async def _attached_map_vin(
    settings: Any,
    snapshot: dict[str, Any],
    ro_number: str,
) -> tuple[str, list[dict[str, str]]] | None:
    """Read attached governing ADAS Maps and return one consensus VIN proof."""
    from . import calibration_iq

    documents = [
        item
        for item in calibration_iq._existing_research_documents(  # noqa: SLF001
            snapshot
        )
        if isinstance(item, dict) and _is_adas_map_document(item)
    ][:_MAX_MAP_DOCUMENTS]
    if not documents:
        return None

    proofs: list[dict[str, str]] = []
    conflict = False
    for document in documents:
        document_id = _text(document.get("id") or document.get("document_id"))
        if not document_id:
            continue
        fetched = await calibration_iq.fetch_operator_document(settings, document_id)
        if (
            not isinstance(fetched, dict)
            or fetched.get("status") != "verified"
            or fetched.get("success") is not True
            or fetched.get("verified") is not True
            or not isinstance(fetched.get("content"), bytes)
        ):
            continue

        raw = fetched["content"]
        filename = (
            _text(document.get("source_name"))
            or _text(document.get("original_filename"))
            or f"{document_id}.pdf"
        )
        try:
            text = await asyncio.to_thread(_extract_pdf_text, raw, filename)
        except Exception:  # noqa: BLE001 - one unreadable map does not prove absence
            log.warning(
                "Could not read attached ADAS Map document %s for RO %s",
                document_id,
                ro_number,
                exc_info=True,
            )
            continue
        if not _document_ro_bound(document, ro_number, text):
            continue

        candidates = _vin_candidates(text)
        if len(candidates) > 1:
            conflict = True
            continue
        if not candidates:
            continue
        vin = next(iter(candidates))
        digest = _text(fetched.get("sha256")).casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            digest = hashlib.sha256(raw).hexdigest()
        proofs.append(
            {
                "vin": vin,
                "document_id": document_id,
                "sha256": digest,
            }
        )

    if conflict or not proofs:
        return None
    vins = {item["vin"] for item in proofs}
    if len(vins) != 1:
        return None
    return next(iter(vins)), proofs


async def _repair_from_attached_adas_map(
    settings: Any,
    ro_number: str,
    context: dict[str, Any] | None,
) -> bool:
    """Backfill VIN from the exact RO's attached ADAS Map, then verify it."""
    from . import calibration_iq

    if (
        not isinstance(context, dict)
        or not context.get("conversation_id")
        or isinstance(context.get("message_id"), bool)
        or not isinstance(context.get("message_id"), int)
        or context.get("message_id") <= 0
        or not context.get("tool_call_id")
    ):
        return False

    resolved = await calibration_iq.operator_resolve_snapshot(settings, ro_number)
    if (
        not isinstance(resolved, dict)
        or resolved.get("status") != "verified"
        or resolved.get("verified") is not True
        or not isinstance(resolved.get("snapshot"), dict)
    ):
        return False
    snapshot = resolved["snapshot"]
    if _ro_key(_snapshot_ro_number(snapshot)) != _ro_key(ro_number):
        return False

    current_vin = _snapshot_vin(snapshot)
    if current_vin:
        return True

    recovered = await _attached_map_vin(settings, snapshot, ro_number)
    if recovered is None:
        return False
    vin, proofs = recovered

    version = _snapshot_version(snapshot)
    repair_order_id = _text(resolved.get("repair_order_id"))
    if version < 1 or not repair_order_id:
        return False

    proof_digest = hashlib.sha256(
        "|".join(
            sorted(
                f"{item['document_id']}:{item['sha256']}:{item['vin']}"
                for item in proofs
            )
        ).encode("utf-8")
    ).hexdigest()[:16]
    invocation = dict(context)
    invocation["tool_call_id"] = (
        f"{_text(context.get('tool_call_id'))[:40]}:adas-map-vin:{proof_digest}"
    )

    result = await calibration_iq.operator_execute(
        settings,
        None,
        {
            "actions": [
                {
                    "operation": "update_ro",
                    "repair_order_id": repair_order_id,
                    "expected_version": version,
                    "arguments": {"vin": vin},
                }
            ],
            calibration_iq._INVOCATION_CONTEXT_KEY: invocation,  # noqa: SLF001
        },
    )
    if not isinstance(result, dict) or result.get("success") is not True:
        return False

    verified = await calibration_iq.operator_snapshot(settings, repair_order_id)
    final_snapshot = (
        verified.get("snapshot")
        if isinstance(verified, dict) and isinstance(verified.get("snapshot"), dict)
        else {}
    )
    return bool(
        isinstance(verified, dict)
        and verified.get("status") == "verified"
        and verified.get("verified") is True
        and _ro_key(_snapshot_ro_number(final_snapshot)) == _ro_key(ro_number)
        and _snapshot_vin(final_snapshot) == vin
    )


def install(research_module: Any) -> None:
    if getattr(research_module, _INSTALLED_ATTR, False):
        return

    original_factory = research_module.default_ro_reader
    original_start = research_module.AdasSiResearchService.start

    @wraps(original_start)
    async def start_with_invocation(
        self: Any, args: dict[str, Any]
    ) -> dict[str, Any]:
        raw_context = (
            args.get(research_module.INVOCATION_KEY)
            if isinstance(args, dict)
            else None
        )
        token = _INVOCATION_CONTEXT.set(
            dict(raw_context) if isinstance(raw_context, dict) else None
        )
        try:
            return await original_start(self, args)
        finally:
            _INVOCATION_CONTEXT.reset(token)

    def default_ro_reader(settings: Any):
        read_original = original_factory(settings)

        async def read(args: dict[str, Any]) -> dict[str, Any]:
            current = await read_original(args)
            if not isinstance(current, dict) or current.get("status") != "verified":
                return current
            if _VIN_RE.fullmatch(
                str(research_module.vin_from_read(current) or "").upper()
            ):
                return current

            ro_number = _ro_number(current)
            if not ro_number:
                return current

            # Cheapest/strongest path first: the historical ScrapeX identity proof.
            stored_repaired = await _repair_from_proven_adas_map(
                settings, ro_number
            )
            if stored_repaired:
                reread = await read_original(args)
                if (
                    isinstance(reread, dict)
                    and reread.get("status") == "verified"
                    and _VIN_RE.fullmatch(
                        str(research_module.vin_from_read(reread) or "").upper()
                    )
                ):
                    return reread
                if isinstance(reread, dict) and reread.get("status") == "verified":
                    current = reread

            # Legacy ROs can outlive ScrapeX's batch/proof row. The attached
            # authoritative ADAS Map then becomes the recovery source itself.
            attached_repaired = await _repair_from_attached_adas_map(
                settings,
                ro_number,
                _INVOCATION_CONTEXT.get(),
            )
            if not attached_repaired:
                return current

            reread = await read_original(args)
            if not isinstance(reread, dict) or reread.get("status") != "verified":
                return current
            return reread

        return read

    research_module.AdasSiResearchService.start = start_with_invocation
    research_module.default_ro_reader = default_ro_reader
    setattr(research_module, _INSTALLED_ATTR, True)
