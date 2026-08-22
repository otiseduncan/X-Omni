"""Strict vehicle-identity guard for ADAS SI search.

ADAS SI already scores filename identity ahead of page text, but scoring alone is
not enough when a generic topic such as blind-spot monitoring exists for many
manufacturers.  If the user explicitly names a manufacturer, a document from a
different manufacturer is not relevant evidence and must never be returned just
because its page text contains the same calibration vocabulary.

This wrapper keeps broad searches broad, while making explicit-make searches
fail closed to that make.  It is intentionally installed around
``SourceInventory.matching_documents`` so every existing ADAS SI consumer --
chat search, Calibration IQ research, and future composite research -- receives
the same identity discipline.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Optional

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def explicit_make(query: object, adas_mod: Any) -> Optional[str]:
    text = str(query or "").casefold()
    if not text:
        return None

    aliases = getattr(adas_mod, "MAKE_ALIASES", {}) or {}
    for alias, canonical in aliases.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(str(alias).casefold())}(?![a-z0-9])", text):
            return str(canonical)

    for make in getattr(adas_mod, "KNOWN_MAKES", ()) or ():
        make_text = str(make)
        folded = make_text.casefold()
        if re.search(rf"(?<![a-z0-9]){re.escape(folded)}(?![a-z0-9])", text):
            return make_text
    return None


def install(adas_mod: Any) -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return

        inventory_cls = adas_mod.SourceInventory
        previous = inventory_cls.matching_documents
        if not getattr(previous, "_xomni_identity_guard", False):
            def matching_documents(self, query: str, limit: int = adas_mod.MAX_RESULTS):
                requested_make = explicit_make(query, adas_mod)
                if not requested_make:
                    return previous(self, query, limit=limit)

                # Ask the existing scorer for a wider candidate pool so the
                # correct make is not accidentally hidden behind stronger
                # generic-topic matches from other makes, then discard every
                # cross-make candidate before returning anything downstream.
                expanded_limit = max(25, int(limit or adas_mod.MAX_RESULTS))
                candidates = previous(self, query, limit=expanded_limit)
                filtered = [
                    item for item in candidates
                    if str((item.get("descriptor") or {}).get("make") or "").casefold()
                    == requested_make.casefold()
                ]
                return filtered[: max(1, min(int(limit or adas_mod.MAX_RESULTS), 25))]

            matching_documents._xomni_identity_guard = True  # type: ignore[attr-defined]
            inventory_cls.matching_documents = matching_documents

        _INSTALLED = True
