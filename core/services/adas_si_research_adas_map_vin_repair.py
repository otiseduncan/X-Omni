"""Repair a missing CIQ VIN from already-proven ADAS Map evidence before SI.

The workflow contract is ADAS Map -> authoritative VIN/calibrations -> SI research.
A VIN hidden from a print/report projection is still available to internal services;
this adapter is only for older repair orders where the ADAS Map was attached but its
already-proven VIN was never persisted into Calibration IQ.

No year/make/model fallback is introduced.  The repair delegates to ScrapeX's
bounded ``repair-proven-vins`` endpoint, which can write only an exact RO whose
stored ADAS Map evidence independently proves the vehicle identity.  X rereads
Calibration IQ after a verified repair and the normal VIN-only research guard then
decides whether the RO is eligible for ALLDATA.
"""

from __future__ import annotations

import logging
import re
from typing import Any


log = logging.getLogger(__name__)
_INSTALLED_ATTR = "__x_adas_map_vin_repair_v1__"
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


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


def _has_attached_adas_map(read: dict[str, Any]) -> bool:
    raw = _mapping(read.get("raw"))
    research = _mapping(raw.get("research"))
    documents = research.get("documents")
    if not isinstance(documents, list):
        documents = raw.get("documents")
    if not isinstance(documents, list):
        return False

    for value in documents:
        document = _mapping(value)
        semantic = _text(document.get("semantic_type")).casefold()
        document_type = _text(document.get("document_type")).casefold()
        if semantic == "adas_map_report" or document_type == "adas_map_report":
            return True
        # Legacy reports predate the canonical semantic tag.  This only decides
        # whether to *attempt* the proof-bound repair; ScrapeX still refuses the
        # mutation unless its stored mechanical evidence proves the exact RO/VIN.
        label = " ".join(
            _text(document.get(key))
            for key in ("title", "source_name", "original_filename")
        ).casefold()
        if "adas map" in label:
            return True
    return False


async def _repair_from_proven_adas_map(settings: Any, ro_number: str) -> bool:
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
            "ADAS Map VIN repair was unavailable for RO %s; SI remains VIN-blocked",
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
    return (
        _text(result.get("ro_number")) == ro_number
        and _text(result.get("status")).casefold() == "repaired"
    )


def install(research_module: Any) -> None:
    if getattr(research_module, _INSTALLED_ATTR, False):
        return

    original_factory = research_module.default_ro_reader

    def default_ro_reader(settings: Any):
        read_original = original_factory(settings)

        async def read(args: dict[str, Any]) -> dict[str, Any]:
            current = await read_original(args)
            if not isinstance(current, dict) or current.get("status") != "verified":
                return current
            if _VIN_RE.fullmatch(str(research_module.vin_from_read(current) or "").upper()):
                return current
            if not _has_attached_adas_map(current):
                return current

            ro_number = _ro_number(current)
            if not ro_number:
                return current
            if not await _repair_from_proven_adas_map(settings, ro_number):
                return current

            # The mutation is never trusted from its receipt alone.  The same
            # authoritative CIQ read must now expose the VIN or the existing
            # VIN-only target guard will still refuse to start Navigator.
            reread = await read_original(args)
            if not isinstance(reread, dict) or reread.get("status") != "verified":
                return current
            return reread

        return read

    research_module.default_ro_reader = default_ro_reader
    setattr(research_module, _INSTALLED_ATTR, True)
