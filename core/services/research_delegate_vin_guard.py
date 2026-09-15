"""Keep ad-hoc research from opening ALLDATA without an exact VIN.

``delegate_research`` remains useful for local ADAS SI, durable knowledge and
public OEM research. It is *not* the CIQ SI attachment workflow. Historically,
its ALLDATA branch accepted year/make/model and could therefore start a second
Navigator run after canonical ``research_si`` had correctly failed closed on a
missing VIN. That produced the appearance of two SI systems and, worse, let an
ALLDATA browser move without exact vehicle identity.

This guard is structural only. It does not inspect user prose or decide what SI
means. If the model includes ALLDATA in a delegated research source order, that
source is executable only with one syntactically valid 17-character VIN. Other
sources retain their normal behavior. CIQ production SI remains
``stage_action research_si`` and already requires exact VIN before Navigator.
"""

from __future__ import annotations

import re
from typing import Any

from . import research_delegate

_INSTALLED_ATTR = "__xomni_delegate_exact_vin_guard_v1__"
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def _valid_vin(args: dict[str, Any]) -> str:
    vehicle = args.get("vehicle") if isinstance(args.get("vehicle"), dict) else {}
    vin = "".join(str(vehicle.get("vin") or "").upper().split())
    return vin if _VIN_RE.fullmatch(vin) else ""


def _insert_identity_ledger(
    result: dict[str, Any], intended_order: list[str]
) -> dict[str, Any]:
    output = dict(result)
    ledger = [dict(item) for item in (output.get("source_ledger") or []) if isinstance(item, dict)]
    if not any(str(item.get("source") or "") == "alldata" for item in ledger):
        guard_entry = {
            "source": "alldata",
            "attempted": False,
            "verified": False,
            "status": "vehicle_identity_required",
            "reason": (
                "ALLDATA navigation requires the exact 17-character VIN. No ALLDATA task "
                "was created. For a Calibration IQ repair order, use stage_action "
                "research_si so the VIN comes from the authoritative RO/ADAS Map handoff."
            ),
        }
        index = intended_order.index("alldata") if "alldata" in intended_order else len(ledger)
        # Insert relative to the intended source order, not simply at the end.
        before = {name for name in intended_order[:index]}
        position = sum(1 for item in ledger if str(item.get("source") or "") in before)
        ledger.insert(position, guard_entry)
    output["source_order"] = intended_order
    output["source_ledger"] = ledger
    output["sources_checked"] = [
        str(item.get("source"))
        for item in ledger
        if item.get("attempted") is True and item.get("source")
    ]
    output["alldata_vehicle_identity_required"] = True
    return output


def install() -> None:
    if getattr(research_delegate, _INSTALLED_ATTR, False):
        return

    original_factory = research_delegate.make_delegate_research

    def make_delegate_research(*args: Any, **kwargs: Any):
        original_handler = original_factory(*args, **kwargs)

        async def delegate_research(call_args: dict[str, Any]) -> dict[str, Any]:
            payload = dict(call_args or {})
            intended_order = research_delegate.source_order(payload)
            if "alldata" not in intended_order or _valid_vin(payload):
                return await original_handler(payload)

            excluded = list(payload.get("exclude_sources") or [])
            if "alldata" not in excluded:
                excluded.append("alldata")
            payload["exclude_sources"] = excluded
            result = await original_handler(payload)
            if not isinstance(result, dict):
                result = {"status": "no_result", "verified": False}
            return _insert_identity_ledger(result, intended_order)

        return delegate_research

    research_delegate.make_delegate_research = make_delegate_research
    setattr(research_delegate, _INSTALLED_ATTR, True)


install()
