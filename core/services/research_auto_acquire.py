"""Automatically acquire missing exact ADAS procedures from verified ALLDATA research.

When an exact vehicle/system calibration/reset procedure is absent from ADAS SI,
a successful licensed ALLDATA result is the current acquisition path. The source
is saved back into ADAS SI automatically so the same information is local next
time. Captures are deduplicated by the authoritative ALLDATA URL.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from . import adas_calibration_depth
from . import research_operator as ro
from . import research_workflow

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False
_PROCEDURE_RE = re.compile(
    r"\b(?:procedure|calibrat\w*|recalibrat\w*|aim\w*|align\w*|adjust\w*|"
    r"initializ\w*|relearn\w*|reset\w*|setup|program\w*|configur\w*)\b",
    re.IGNORECASE,
)


def acquisition_candidate(result: dict[str, Any]) -> bool:
    query = str(result.get("query") or "")
    if not (_PROCEDURE_RE.search(query) or adas_calibration_depth.calibration_intent(query)):
        return False

    local = result.get("adas_si") if isinstance(result.get("adas_si"), dict) else {}
    if int(local.get("result_count") or 0) > 0:
        return False

    alldata = result.get("alldata") if isinstance(result.get("alldata"), dict) else {}
    return bool(
        alldata.get("verified") is True
        and alldata.get("searched") is True
        and alldata.get("query_submitted") is True
        and isinstance(alldata.get("vehicle_selection"), dict)
        and alldata["vehicle_selection"].get("selected") is True
        and str(alldata.get("result_title") or "").strip()
        and int(alldata.get("relevance_score") or 0) >= 8
        and len(str(alldata.get("page_text") or "").strip()) >= 80
    )


def _existing_capture(folder: Path, url: str) -> dict[str, Any] | None:
    if not folder.is_dir() or not url:
        return None
    canonical = url.strip()
    for sidecar in folder.glob("*.source.json"):
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(payload.get("url") or "").strip() != canonical:
            continue
        name = sidecar.name
        pdf_name = name[:-len(".source.json")] + ".pdf" if name.endswith(".source.json") else ""
        pdf = sidecar.with_name(pdf_name) if pdf_name else None
        return {
            "sidecar": sidecar,
            "pdf": pdf if pdf is not None and pdf.is_file() else None,
            "provenance": payload,
        }
    return None


async def deduplicating_capture(self: Any, args: dict[str, Any]) -> dict[str, Any]:
    await self.start(auto_login=True)
    page = self._page  # noqa: SLF001
    if page is None:
        raise ValueError("No active ALLDATA page is available to capture.")
    current_url = str(page.url or "")[: ro.MAX_URL_CHARS]
    folder = Path(self.adas.source_root) / "Acquired" / "ALLDATA" if self.adas is not None else Path()
    existing = _existing_capture(folder, current_url) if self.adas is not None else None
    if existing is not None:
        pdf = existing.get("pdf")
        sidecar = existing.get("sidecar")
        return {
            "status": "success",
            "action": "capture_to_adas",
            "saved": False,
            "already_present": True,
            "relative_path": self.adas.relative_of(pdf) if pdf is not None else None,
            "source_sidecar": self.adas.relative_of(sidecar) if sidecar is not None else None,
            "provenance": existing.get("provenance") or {},
        }
    return await _PREVIOUS_CAPTURE(self, args)


async def full_research_with_acquisition(
    args: dict[str, Any], *, adas: Any, browser: Any
) -> dict[str, Any]:
    result = await _PREVIOUS_FULL(args, adas=adas, browser=browser)
    if not isinstance(result, dict) or not acquisition_candidate(result):
        return result

    alldata = result.get("alldata") or {}
    vehicle = alldata.get("vehicle") if isinstance(alldata.get("vehicle"), dict) else {}
    vehicle_label = str(vehicle.get("label") or result.get("requested_manufacturer") or "Vehicle")
    topic = str(alldata.get("result_title") or alldata.get("topic") or result.get("query") or "Calibration procedure")

    captures = [item for item in (result.get("captures") or []) if isinstance(item, dict)]
    if any(item.get("source") == "ALLDATA" for item in captures):
        return result

    try:
        saved = await browser._capture_to_adas({  # noqa: SLF001 - same research operator boundary
            "vehicle": vehicle_label,
            "topic": topic[:160],
        })
        captures.append({"source": "ALLDATA", "auto_acquired": True, **saved})
        result["captures"] = captures
        result["auto_acquired_to_adas_si"] = True
        result["acquisition_reason"] = (
            "Exact vehicle/system calibration or reset procedure was absent locally and a verified ALLDATA procedure was found."
        )
    except Exception as exc:  # noqa: BLE001
        captures.append({
            "source": "ALLDATA",
            "auto_acquired": True,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        })
        result["captures"] = captures
        result["auto_acquired_to_adas_si"] = False
    return result


def install() -> None:
    global _INSTALLED, _PREVIOUS_CAPTURE, _PREVIOUS_FULL
    with _INSTALL_LOCK:
        if _INSTALLED:
            return

        _PREVIOUS_CAPTURE = ro.LicensedBrowser._capture_to_adas  # noqa: SLF001
        if not getattr(_PREVIOUS_CAPTURE, "_xomni_dedup_capture", False):
            deduplicating_capture._xomni_dedup_capture = True  # type: ignore[attr-defined]
            ro.LicensedBrowser._capture_to_adas = deduplicating_capture  # noqa: SLF001

        _PREVIOUS_FULL = research_workflow.full_research
        if not getattr(_PREVIOUS_FULL, "_xomni_auto_acquire", False):
            full_research_with_acquisition._xomni_auto_acquire = True  # type: ignore[attr-defined]
            research_workflow.full_research = full_research_with_acquisition

        _INSTALLED = True


_PREVIOUS_CAPTURE = ro.LicensedBrowser._capture_to_adas  # noqa: SLF001
_PREVIOUS_FULL = research_workflow.full_research
