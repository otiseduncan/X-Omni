"""Strict vehicle-identity guard for ADAS SI search.

Calibration evidence must match the vehicle the user actually named. Topic text
from a different vehicle is not acceptable merely because it uses the same ADAS
terminology. Explicit year/make/model requests therefore fail closed to that
identity before page text reaches normal search or the deep calibration scanner.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Optional

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False

_TOPIC_BOUNDARY_RE = re.compile(
    r"\b(?:front|forward|rear|blind|bsm|bsd|adas|camera|radar|sensor|module|"
    r"calibrat\w*|recalibrat\w*|aim\w*|align\w*|adjust\w*|initializ\w*|"
    r"relearn\w*|reset\w*|procedure|repair|collision|position\s+statement|"
    r"windshield|steering|occupant|parking|park\s+assist)\b",
    re.IGNORECASE,
)
_YEAR_ANY_RE = re.compile(r"(?<!\d)(?P<year>20\d{2}|\d{2})(?!\d)")
_RANGE_RE = re.compile(r"(?<!\d)(?P<start>20\d{2}|\d{2})\s*[-–]\s*(?P<end>20\d{2}|\d{2})(?!\d)")
_MODEL_SYSTEM_TOKENS = {
    "adas", "bsm", "bsd", "ccm", "ipma", "camera", "radar", "sensor",
    "module", "calibration", "alignment", "adjustment", "aiming", "aim",
    "initialization", "initialize", "relearn", "reset", "monitor", "parking",
    "surround", "forward", "front", "rear", "blind", "lane",
}


def _compact(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _model_key(value: object) -> str:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9-]*", str(value or ""))
    kept: list[str] = []
    for word in words:
        if word.casefold() in _MODEL_SYSTEM_TOKENS:
            break
        kept.append(word)
    return _compact(" ".join(kept))


def _normalize_year(raw: object) -> Optional[int]:
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return None
    if value < 100:
        value += 2000 if value <= 69 else 1900
    return value if 1900 <= value <= 2099 else None


def explicit_year(query: object) -> Optional[int]:
    text = str(query or "")
    match = _YEAR_ANY_RE.search(text)
    return _normalize_year(match.group("year")) if match else None


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


def _make_match(query: str, requested_make: str, adas_mod: Any) -> re.Match[str] | None:
    choices = [requested_make]
    for alias, canonical in (getattr(adas_mod, "MAKE_ALIASES", {}) or {}).items():
        if str(canonical).casefold() == requested_make.casefold():
            choices.append(str(alias))
    choices.sort(key=len, reverse=True)
    return re.search(
        r"(?<![a-z0-9])(?:" + "|".join(re.escape(item) for item in choices) + r")(?![a-z0-9])",
        query,
        re.IGNORECASE,
    )


def explicit_model(query: object, requested_make: Optional[str], adas_mod: Any) -> Optional[str]:
    """Extract the user-named model/trim tail after the make.

    The comparison is prefix-compatible so ``Cherokee Latitude Luxe`` matches a
    document indexed simply as ``Cherokee`` while still excluding Grand Cherokee.
    """
    if not requested_make:
        return None
    text = " ".join(str(query or "").replace("_", " ").split()).strip()
    match = _make_match(text, requested_make, adas_mod)
    if not match:
        return None
    tail = text[match.end():].strip(" ,:;-()")
    if not tail:
        return None

    tail = re.sub(r"^(?:20\d{2}|\d{2})\b\s*", "", tail).strip()
    words = tail.split()
    while words and words[0].casefold() in getattr(adas_mod, "BODY_PREFIXES", set()):
        words.pop(0)
    tail = " ".join(words)
    boundary = _TOPIC_BOUNDARY_RE.search(tail)
    if boundary:
        tail = tail[:boundary.start()]
    tail = re.split(
        r"\b(?:with|that|which|and\s+I|because|after|before|involved)\b",
        tail,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    model = " ".join(re.findall(r"[A-Za-z0-9][A-Za-z0-9-]*", tail)[:6]).strip()
    return model or None


def descriptor_make(descriptor: dict[str, Any], adas_mod: Any) -> Optional[str]:
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

    candidates.sort(key=lambda item: len(item[0]), reverse=True)
    for compact_make, canonical in candidates:
        index = title.find(compact_make)
        if index < 0:
            continue
        prefix = title[:index]
        if index == 0 or (index <= 8 and prefix.isdigit()):
            return canonical
    return None


def descriptor_year_matches(descriptor: dict[str, Any], requested_year: Optional[int]) -> bool:
    if requested_year is None:
        return True
    parsed = descriptor.get("year")
    if isinstance(parsed, int) and not isinstance(parsed, bool):
        return parsed == requested_year

    title = str(descriptor.get("title") or "")
    for match in _RANGE_RE.finditer(title):
        start = _normalize_year(match.group("start"))
        end = _normalize_year(match.group("end"))
        if start is not None and end is not None and min(start, end) <= requested_year <= max(start, end):
            return True
    for match in _YEAR_ANY_RE.finditer(title):
        if _normalize_year(match.group("year")) == requested_year:
            return True
    return not bool(_YEAR_ANY_RE.search(title) or _RANGE_RE.search(title))


def descriptor_model_matches(descriptor: dict[str, Any], requested_model: Optional[str]) -> bool:
    if not requested_model:
        return True
    query_key = _model_key(requested_model)
    if not query_key:
        return True

    parsed = str(descriptor.get("model") or "").strip()
    if parsed:
        doc_key = _model_key(parsed)
        if doc_key:
            return query_key.startswith(doc_key) or doc_key.startswith(query_key)

    title_key = _compact(descriptor.get("title"))
    make_key = _compact(descriptor.get("make") or "")
    if make_key:
        pos = title_key.find(make_key)
        if pos >= 0:
            remainder = title_key[pos + len(make_key):]
            return remainder.startswith(query_key) or query_key.startswith(remainder[: len(query_key)])
    return False


def descriptor_matches_query(descriptor: dict[str, Any], query: object, adas_mod: Any) -> bool:
    requested_make = explicit_make(query, adas_mod)
    requested_year = explicit_year(query)
    requested_model = explicit_model(query, requested_make, adas_mod)

    if requested_make:
        actual_make = descriptor_make(descriptor, adas_mod)
        if str(actual_make or "").casefold() != requested_make.casefold():
            return False
    if not descriptor_year_matches(descriptor, requested_year):
        return False
    if not descriptor_model_matches(descriptor, requested_model):
        return False
    return True


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
                requested_model = explicit_model(query, requested_make, adas_mod)
                requested_year = explicit_year(query)
                if not any((requested_make, requested_model, requested_year)):
                    return previous(self, query, limit=limit)

                expanded_limit = max(40, int(limit or adas_mod.MAX_RESULTS))
                candidates = previous(self, query, limit=expanded_limit)
                filtered = [
                    item
                    for item in candidates
                    if descriptor_matches_query(item.get("descriptor") or {}, query, adas_mod)
                ]
                return filtered[: max(1, min(int(limit or adas_mod.MAX_RESULTS), 25))]

            matching_documents._xomni_identity_guard = True  # type: ignore[attr-defined]
            inventory_cls.matching_documents = matching_documents

        _INSTALLED = True
