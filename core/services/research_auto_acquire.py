"""Deduplicate explicit ALLDATA captures by authoritative source URL."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from . import research_operator as ro

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False
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


def install() -> None:
    global _INSTALLED, _PREVIOUS_CAPTURE
    with _INSTALL_LOCK:
        if _INSTALLED:
            return

        _PREVIOUS_CAPTURE = ro.LicensedBrowser._capture_to_adas  # noqa: SLF001
        if not getattr(_PREVIOUS_CAPTURE, "_xomni_dedup_capture", False):
            deduplicating_capture._xomni_dedup_capture = True  # type: ignore[attr-defined]
            ro.LicensedBrowser._capture_to_adas = deduplicating_capture  # noqa: SLF001

        _INSTALLED = True


_PREVIOUS_CAPTURE = ro.LicensedBrowser._capture_to_adas  # noqa: SLF001
