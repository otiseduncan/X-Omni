"""Strict vehicle-identity guard for ADAS SI search.

ADAS SI already scores filename identity ahead of page text, but scoring alone is
not enough when a generic topic such as blind-spot monitoring exists for many
manufacturers. If the user explicitly names a manufacturer, a document from a
different manufacturer is not relevant evidence and must never be returned just
because its page text contains the same calibration vocabulary.

Real ADAS SI filenames are not perfectly normalized. Some begin with a year,
some begin directly with the make, and some omit the space between make/model
(e.g. ``HyundaiPalisade(2020-25)...``). This guard therefore uses the parsed
make when available and a conservative filename-prefix fallback when it is not.
Broad searches remain broad; explicit-make searches fail closed to that make.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Optional

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def _compact(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


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


def descriptor_make(descriptor: dict[str, Any], adas_mod: Any) -> Optional[str]:
    """Return the document make without guessing across unrelated title text.

    Prefer the normal filename parser. For legacy/unparsed filenames, accept a
    make only when its compact spelling occurs at the beginning of the title or
    immediately after a leading numeric year/range prefix. This handles names
    like ``HyundaiPalisade...`` and ``2023-2026ToyotaHighlander...`` without
    turning arbitrary page/topic words into vehicle identity.
    """

    parsed = str(descriptor.get("make") or "").strip()
    if parsed:
        return parsed

    title = _compact(descriptor.get("title"))
    if not title:
        return None

    candidates: list[tuple[str, str]] = []
    for make in getattr(adas_mod, "KNOWN_MAKES", ()) or ():
        canonical = str(make)
        compact = _compact(canonical)
        if compact:
            candidates.append((compact, canonical))
    for alias, canonical in (getattr(adas_mod, "MAKE_ALIASES", {}) or {}).items():
        compact = _compact(alias)
        if compact:
            candidates.append((compact, str(canonical)))

    # Longest names first prevents a shorter overlapping spelling from winning.
    candidates.sort(key=lambda item: len(item[0]), reverse=True)
    for compact_make, canonical in candidates:
        index = title.find(compact_make)
        if index < 0:
            continue
        prefix = title[:index]
        if index == 0 or (index <= 8 and prefix.isdigit()):
            return canonical
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
                # generic-topic matches from other makes. Then discard every
                # cross-make or genuinely unknown-make candidate before any page
                # text reaches the model/UI.
                expanded_limit = max(25, int(limit or adas_mod.MAX_RESULTS))
                candidates = previous(self, query, limit=expanded_limit)
                filtered = [
                    item
                    for item in candidates
                    if str(descriptor_make(item.get("descriptor") or {}, adas_mod) or "").casefold()
                    == requested_make.casefold()
                ]
                return filtered[: max(1, min(int(limit or adas_mod.MAX_RESULTS), 25))]

            matching_documents._xomni_identity_guard = True  # type: ignore[attr-defined]
            inventory_cls.matching_documents = matching_documents

        _INSTALLED = True
