"""Keep deep calibration scanning on the explicitly requested vehicle.

The deep scanner intentionally looks beyond top-ranked pages, but it must not
look across other models just because they share a manufacturer. This wrapper
uses the same year/make/model identity contract as ordinary ADAS SI search.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from . import adas_calibration_depth, adas_identity_guard

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def install(adas_mod: Any) -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        previous = adas_calibration_depth._deep_documents  # noqa: SLF001
        if not getattr(previous, "_xomni_exact_vehicle_depth", False):
            def exact_vehicle_documents(adas: Any, module: Any, query: str):
                items = previous(adas, module, query)
                filtered = []
                for descriptor, score in items:
                    if not isinstance(descriptor, dict):
                        continue
                    if adas_identity_guard.descriptor_matches_query(descriptor, query, adas_mod):
                        filtered.append((descriptor, score))
                return filtered

            exact_vehicle_documents._xomni_exact_vehicle_depth = True  # type: ignore[attr-defined]
            adas_calibration_depth._deep_documents = exact_vehicle_documents  # noqa: SLF001
        _INSTALLED = True
