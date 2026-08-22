"""Deep calibration-rule scanning for ADAS SI.

Calibration requirements are often buried in a late-page note, sidebar, warning,
or one-line applicability statement.  Normal ranking is still useful for finding
the right documents, but a calibration question must not stop at the first high-
scoring page.  This wrapper scans every page of the relevant OEM PDFs (using the
existing native+OCR page path) for calibration-trigger language and folds those
findings back into ordinary ADAS SI results.

Because it wraps ``AdasSI.search`` itself, the behavior automatically applies to
chat research, Calibration IQ ``research_ro``, and any future consumer of the
same authoritative ADAS SI service.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Optional

from . import adas_identity_guard

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False

_CALIBRATION_INTENT_RE = re.compile(
    r"\b(?:calibrat(?:e|ed|es|ing|ion|ions)?|recalibrat(?:e|ed|ion)?|"
    r"aim(?:ing|ment)?|beam\s+axis|target\s+(?:placement|setting)|"
    r"camera\s+(?:aim|alignment)|radar\s+(?:aim|alignment|adjustment)|"
    r"adas\s+(?:calibration|aiming)|eyesight|blind\s+spot\s+(?:monitor|radar)|"
    r"forward\s+recognition\s+camera|millimeter\s+wave\s+radar|"
    # These are standalone ADAS system names (same tier as eyesight / blind
    # spot monitor above) so a plain "I need the 360 camera procedure for X"
    # routes into research without also requiring collision-context language
    # -- a real reported gap: that exact phrasing matched neither this regex
    # nor the collision-context fallback below, so ALLDATA was never even
    # attempted and the turn silently dead-ended at a local ADAS SI miss.
    r"360[\s-]?(?:degree\s+)?(?:view\s+)?camera|surround\s+(?:view|vision)\s+camera|"
    r"parking\s+aid\s+camera)\b",
    re.IGNORECASE,
)
_COLLISION_CONTEXT_RE = re.compile(
    r"\b(?:collision|accident|impact|body\s+repair|sheet\s+metal|"
    r"windshield|bumper|suspension|alignment|remove|removed|removal|"
    r"replace|replaced|replacement|repair|repaired|installation|installed)\b",
    re.IGNORECASE,
)

# These are deliberately phrased as requirement/trigger language rather than
# one OEM-specific sentence.  The goal is to find small policy statements such
# as "after any collision" as well as procedural trigger tables.
_RULE_PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    (
        "collision_trigger",
        re.compile(
            r"\b(?:after|following|whenever|when)\s+(?:any|all|a|the)?\s*"
            r"(?:collision|accident|impact)\b.{0,180}\b(?:calibrat|aim|adjust|inspect|confirm|initialize)",
            re.IGNORECASE | re.DOTALL,
        ),
        18,
    ),
    (
        "collision_trigger_reverse",
        re.compile(
            r"\b(?:calibrat\w*|aim\w*|adjust\w*|beam\s+axis\s+confirmation|initializ\w*)\b"
            r".{0,180}\b(?:after|following|required\s+after)\b.{0,80}\b(?:collision|accident|impact)\b",
            re.IGNORECASE | re.DOTALL,
        ),
        18,
    ),
    (
        "explicit_requirement",
        re.compile(
            r"\b(?:calibration|recalibration|aiming|adjustment|beam\s+axis\s+confirmation|"
            r"camera\s+alignment|radar\s+alignment)\b.{0,90}\b"
            r"(?:is|required|necessary|must|shall|needs?\s+to|should)\b",
            re.IGNORECASE | re.DOTALL,
        ),
        16,
    ),
    (
        "explicit_requirement_reverse",
        re.compile(
            r"\b(?:must|shall|required|necessary|needs?\s+to|be\s+sure\s+to)\b.{0,90}\b"
            r"(?:calibrat\w*|recalibrat\w*|aim\w*|adjust\w*|beam\s+axis|initialize\w*)\b",
            re.IGNORECASE | re.DOTALL,
        ),
        16,
    ),
    (
        "operation_trigger",
        re.compile(
            r"\b(?:if|when|after|following)\b.{0,90}\b"
            r"(?:removed|removal|installed|installation|replaced|replacement|repaired|repair|"
            r"windshield|bumper|suspension|wheel\s+alignment|sheet\s+metal)\b.{0,180}\b"
            r"(?:calibrat|aim|adjust|confirm|initialize|inspection)",
            re.IGNORECASE | re.DOTALL,
        ),
        14,
    ),
    (
        "procedure_trigger_table",
        re.compile(
            r"\b(?:operation|condition|situation|case|procedure|trigger)\b.{0,140}\b"
            r"(?:calibrat|aim|adjust|beam\s+axis|initialize)",
            re.IGNORECASE | re.DOTALL,
        ),
        10,
    ),
    (
        "oem_warning",
        re.compile(
            r"\b(?:notice|caution|warning|important|hint)\b.{0,220}\b"
            r"(?:calibrat|aim|adjust|beam\s+axis|initialize|adas|eyesight)",
            re.IGNORECASE | re.DOTALL,
        ),
        9,
    ),
)


def calibration_intent(query: object) -> bool:
    text = str(query or "")
    if not text.strip():
        return False
    if _CALIBRATION_INTENT_RE.search(text):
        return True
    # Some field questions omit the word calibration but pair an ADAS system
    # with collision/replacement language.  Treat those as calibration research.
    system = re.search(
        r"\b(?:adas|eyesight|blind\s+spot|lane\s+(?:keep|departure)|front\s+camera|"
        r"forward\s+camera|radar|rear\s+corner\s+radar)\b",
        text,
        re.IGNORECASE,
    )
    return bool(system and _COLLISION_CONTEXT_RE.search(text))


def _query_tokens(query: str) -> set[str]:
    ignored = {
        "after", "all", "any", "are", "calibration", "calibrate", "collision",
        "does", "for", "from", "how", "is", "need", "required", "the", "this",
        "vehicle", "when", "with", "what", "which",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9-]+", query.casefold())
        if len(token) >= 3 and token not in ignored
    }


def _rule_matches(text: str) -> list[tuple[str, int, int]]:
    found: list[tuple[str, int, int]] = []
    for label, pattern, weight in _RULE_PATTERNS:
        match = pattern.search(text)
        if match:
            found.append((label, match.start(), weight))
    found.sort(key=lambda item: (-item[2], item[1]))
    return found


def _excerpt(text: str, position: int, length: int = 2200) -> str:
    start = max(0, int(position) - 550)
    end = min(len(text), start + length)
    return str(text[start:end]).strip()


def _candidate_score_map(inventory: Any, query: str) -> dict[str, int]:
    try:
        candidates = inventory.matching_documents(query, limit=25)
    except Exception:
        return {}
    result: dict[str, int] = {}
    for item in candidates:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if isinstance(path, Path):
            result[str(path.resolve())] = int(item.get("score") or 0)
    return result


def _deep_documents(adas: Any, adas_mod: Any, query: str) -> list[tuple[dict[str, Any], int]]:
    """Return every relevant OEM document we must inspect page-by-page.

    If a make is explicit, scan *all* documents belonging to that make.  This is
    intentionally broader than normal top-N search because a global collision
    requirement can live in a general OEM document whose filename does not name
    the specific sensor the user asked about.
    """
    score_map = _candidate_score_map(adas.inventory, query)
    requested_make = adas_identity_guard.explicit_make(query, adas_mod)
    docs = adas.inventory.documents()
    output: list[tuple[dict[str, Any], int]] = []

    if requested_make:
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if adas_identity_guard.descriptor_make(doc, adas_mod) != requested_make:
                continue
            path = doc.get("_path")
            if not isinstance(path, Path):
                continue
            # A make-only fallback score is useful for surfacing deep policy
            # findings, but it is deliberately below Calibration IQ's >=10
            # exact-source evidence threshold unless normal identity scoring
            # independently proves the specific source.
            score = score_map.get(str(path.resolve()), 4)
            output.append((doc, score))
        return output

    # Without a named make, stay bounded to the normal identity-ranked set.
    by_path = {str((doc.get("_path") or "")): doc for doc in docs if isinstance(doc, dict)}
    for raw_path, score in score_map.items():
        doc = next(
            (
                value for value in docs
                if isinstance(value, dict)
                and isinstance(value.get("_path"), Path)
                and str(value["_path"].resolve()) == raw_path
            ),
            None,
        )
        if doc is not None:
            output.append((doc, score))
    return output


def install(adas_mod: Any) -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        cls = adas_mod.AdasSI
        previous = cls.search
        if getattr(previous, "_xomni_calibration_depth", False):
            _INSTALLED = True
            return

        def search(self, args: dict):
            result = previous(self, args)
            if not isinstance(result, dict):
                return result
            query = str((args or {}).get("query") or "").strip()
            if not calibration_intent(query) or not self.available():
                return result

            query_tokens = _query_tokens(query)
            deep_findings: list[dict[str, Any]] = []
            pages_scanned = 0
            documents_scanned = 0
            deep_docs: dict[str, dict[str, Any]] = {}

            for descriptor, source_score in _deep_documents(self, adas_mod, query):
                path = descriptor.get("_path")
                if not isinstance(path, Path):
                    continue
                documents_scanned += 1
                try:
                    pages = self._pages(path)
                except Exception:
                    continue
                for page_number, raw_text in pages:
                    pages_scanned += 1
                    text = str(raw_text or "")
                    if not text.strip():
                        continue
                    matches = _rule_matches(text)
                    if not matches:
                        continue
                    folded = text.casefold()
                    context_matches = sorted(token for token in query_tokens if token in folded)
                    # For a named make we intentionally allow global OEM rules
                    # even when the page does not repeat the component name.
                    # Otherwise require at least some query context.
                    requested_make = adas_identity_guard.explicit_make(query, adas_mod)
                    if not requested_make and query_tokens and not context_matches:
                        continue
                    label, position, weight = matches[0]
                    relative = self.relative_of(path)
                    entry = {
                        "source": path.name,
                        "title": str(descriptor.get("title") or path.stem),
                        "page": int(page_number),
                        "relative_path": relative,
                        "url": f"/api/adas-si/document?path={__import__('urllib.parse').parse.quote(relative)}",
                        "excerpt": _excerpt(text, position),
                        "match_score": int(weight + min(len(context_matches), 5) * 3),
                        "source_match_score": int(source_score),
                        "vehicle": {
                            key: descriptor.get(key)
                            for key in ("year", "make", "model", "drivetrain", "platform_code", "topic")
                            if descriptor.get(key) is not None
                        },
                        "deep_calibration_rule": True,
                        "matched_rule": label,
                        "matched_context_terms": context_matches[:10],
                    }
                    deep_findings.append(entry)
                    deep_docs[relative] = {
                        "title": entry["title"],
                        "source": path.name,
                        "relative_path": relative,
                        "url": entry["url"],
                        "pages_total": self.page_count(path),
                        "source_match_score": int(source_score),
                        **{
                            key: descriptor.get(key)
                            for key in ("year", "make", "model", "drivetrain", "platform_code", "topic")
                        },
                    }

            deep_findings.sort(
                key=lambda item: (
                    int(item.get("source_match_score") or 0),
                    int(item.get("match_score") or 0),
                ),
                reverse=True,
            )

            existing = [item for item in (result.get("results") or []) if isinstance(item, dict)]
            merged: list[dict[str, Any]] = []
            seen: set[tuple[str, int]] = set()
            # Requirement/trigger findings go first for calibration questions.
            for item in [*deep_findings, *existing]:
                key = (str(item.get("relative_path") or item.get("source") or ""), int(item.get("page") or 0))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
            result["results"] = merged[: max(adas_mod.MAX_RESULTS, 8)]

            matched = [item for item in (result.get("matched_documents") or []) if isinstance(item, dict)]
            matched_paths = {str(item.get("relative_path") or "") for item in matched}
            for relative, doc in deep_docs.items():
                if relative not in matched_paths:
                    matched.append(doc)
                    matched_paths.add(relative)
            # Keep enough deep documents for Calibration IQ to associate a
            # buried trigger with its authoritative OEM source.
            matched.sort(key=lambda item: int(item.get("source_match_score") or 0), reverse=True)
            result["matched_documents"] = matched[:12]
            if any(int(item.get("source_match_score") or 0) >= 10 for item in deep_findings):
                result["exact_source_matched"] = True
            if deep_findings and result.get("status") in {"no_result", "partial_success"}:
                result["status"] = "success"
                result["message"] = None

            result["deep_calibration_findings"] = deep_findings[:24]
            result["calibration_deep_scan"] = {
                "enabled": True,
                "documents_scanned": documents_scanned,
                "pages_scanned": pages_scanned,
                "finding_count": len(deep_findings),
                "scanned_full_documents": True,
                "uses_native_and_ocr_text": True,
            }
            return result

        search._xomni_calibration_depth = True  # type: ignore[attr-defined]
        cls.search = search
        _INSTALLED = True
