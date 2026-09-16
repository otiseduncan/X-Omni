"""Local-library-first source cascade for CIQ-bound service-information research.

Every Calibration IQ calibration requirement is checked against the shared
Year/Make/Model ADAS SI library before licensed browsing begins. A local hit
only satisfies the objective when X's independent semantic review says it is
the ACTUAL_PROCEDURE for that exact vehicle/system. Related requirements,
R&I pages, descriptions, diagnostics, and supporting information never suppress
the ALLDATA fallback for a missing calibration procedure.

A library shared by Year/Make/Model does not make every procedure in it
evidence for every calibration on the vehicle. ScrapeX capture sidecars record
the research objective that produced an artifact, including its
``calibration_item_id``. That stored id is provenance, not semantics: an
artifact captured for one CIQ calibration item is never reused for a different
one. Legacy or manual library files without captured objective provenance stay
eligible for X's independent review.

The ALLDATA Navigator is the fallback. It receives the exact CIQ VIN and
reselects that vehicle before X navigates. Accepted ALLDATA captures are stored
by ScrapeX in the same library, then attached from that library artifact to the
exact RO calibration item (``adas_si_research.default_attach``).

``AdasSiResearchService._research`` calls ``local_procedure`` directly; nothing
here is installed or rebinds another module.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

log = logging.getLogger("xomni.adas_si_research_source_cascade")

_LOCAL_TEXT_CHARS = 60_000
_LOCAL_DOC_LIMIT = 5
_ACCEPTING = frozenset({"ACCEPT", "ACCEPT_WITH_DEPENDENCIES"})
_ACTUAL = "ACTUAL_PROCEDURE"


def _clean(value: Any, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _vehicle(objective: dict[str, Any]) -> dict[str, Any]:
    raw = objective.get("vehicle") if isinstance(objective.get("vehicle"), dict) else {}
    return {
        key: raw[key]
        for key in ("year", "make", "model", "trim")
        if raw.get(key) not in (None, "")
    }


def captured_calibration_id(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    objective = metadata.get("objective")
    if not isinstance(objective, dict):
        return ""
    return _clean(objective.get("calibration_item_id") or objective.get("calibration_id"), 120)


def provenance_allows_reuse(metadata: Any, objective: Any) -> bool:
    """Refuse only a proved captured-objective mismatch; legacy files stay reviewable."""
    captured = captured_calibration_id(metadata)
    current = (
        _clean(objective.get("calibration_id") or objective.get("calibration_item_id"), 120)
        if isinstance(objective, dict)
        else ""
    )
    return not (captured and current and captured != current)


def _search_args(objective: dict[str, Any]) -> dict[str, Any]:
    requirement = _clean(objective.get("requirement_label") or objective.get("calibration_title"), 200)
    args: dict[str, Any] = {
        "vehicle": _vehicle(objective),
        # The library is searched by the requirement itself; the reviewer, not
        # the query wording, decides whether a hit is the procedure.
        "question": requirement or _clean(objective.get("topic"), 500),
        "search_mode": "calibration_requirements",
    }
    system = _clean(objective.get("system"), 200)
    component = _clean(objective.get("calibration_title"), 200)
    if system:
        args["system"] = system
    if component:
        args["component"] = component
    return args


def _candidate_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one best page hint per matched library document."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in result.get("results") or []:
        if not isinstance(item, dict):
            continue
        relative = _clean(item.get("relative_path"), 1000)
        if not relative or relative.casefold() in seen:
            continue
        seen.add(relative.casefold())
        try:
            page = max(1, int(item.get("page") or 1))
        except (TypeError, ValueError):
            page = 1
        rows.append(
            {
                "relative_path": relative,
                "title": _clean(item.get("title") or item.get("source"), 300),
                "page": page,
                "url": item.get("url"),
            }
        )
        if len(rows) >= _LOCAL_DOC_LIMIT:
            return rows

    # A scan may match by canonical Year/Make/Model identity while exposing no
    # searchable PDF text. Keep it eligible for visual review instead of
    # incorrectly declaring the procedure absent.
    for item in result.get("matched_documents") or []:
        if not isinstance(item, dict):
            continue
        relative = _clean(item.get("relative_path"), 1000)
        if not relative or relative.casefold() in seen:
            continue
        seen.add(relative.casefold())
        rows.append(
            {
                "relative_path": relative,
                "title": _clean(item.get("title") or item.get("source"), 300),
                "page": 1,
                "url": item.get("url"),
            }
        )
        if len(rows) >= _LOCAL_DOC_LIMIT:
            break
    return rows


def _sidecar_text(path: Path) -> str:
    """Prefer ScrapeX's exact extracted text beside a rendered-page PDF."""
    text_path = path.with_suffix(".text.txt")
    if not text_path.is_file():
        return ""
    try:
        return text_path.read_text(encoding="utf-8", errors="replace")[:_LOCAL_TEXT_CHARS]
    except OSError:
        return ""


def _source_metadata(path: Path) -> dict[str, Any]:
    sidecar = path.with_suffix(".source.json")
    if not sidecar.is_file():
        return {}
    try:
        value = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def _library_text(adas: Any, path: Path) -> str:
    text = await asyncio.to_thread(_sidecar_text, path)
    if text.strip():
        return text

    # This is inside the ADAS SI service boundary. `_pages` is the shared,
    # cached PDF/OCR path used by normal ADAS SI search, so local preflight sees
    # the same text X sees in chat and does not invent a second parser.
    pages = await asyncio.to_thread(adas._pages, path)  # noqa: SLF001
    output: list[str] = []
    used = 0
    for page, raw in pages:
        text = str(raw or "").strip()
        if not text:
            continue
        chunk = f"\n\n[Page {page}]\n{text}"
        remaining = _LOCAL_TEXT_CHARS - used
        if remaining <= 0:
            break
        output.append(chunk[:remaining])
        used += min(len(chunk), remaining)
    return "".join(output).strip()


async def _screenshot(
    adas: Any, path: Path, page: int
) -> tuple[bytes, str] | None:
    try:
        data = await asyncio.to_thread(adas.render_page, path, page, 1100)
    except Exception:  # noqa: BLE001 - text review can still proceed
        return None
    return (data, "image/png") if data else None


async def _review_local(
    service: Any,
    objective: dict[str, Any],
    row: dict[str, Any],
) -> dict[str, Any] | None:
    if service.adas is None or service.client is None:
        return None

    try:
        path = service.adas.resolve_relative(row["relative_path"])
        metadata = await asyncio.to_thread(_source_metadata, path)
        if not provenance_allows_reuse(metadata, objective):
            return None
        text = await _library_text(service.adas, path)
        shot = await _screenshot(service.adas, path, int(row.get("page") or 1))
    except Exception:  # noqa: BLE001 - one bad library file must not stop fallback
        log.warning(
            "Could not prepare ADAS SI candidate %s",
            row.get("relative_path"),
            exc_info=True,
        )
        return None

    from . import research_semantic_review

    review_objective = {
        "objective": objective.get("topic"),
        "requirement_label": objective.get("requirement_label") or objective.get("calibration_title"),
        "system": objective.get("system"),
        "component": objective.get("calibration_title"),
        "repair_order": objective.get("ro_number"),
        "calibration_item_id": objective.get("calibration_id"),
    }
    candidate = {
        "title": row.get("title") or path.stem,
        "breadcrumb": list(Path(row["relative_path"]).parts[:-1]),
        "url": row.get("url") or f"adas-si:///{quote(row['relative_path'])}",
        "text": text,
        "text_truncated": len(text) >= _LOCAL_TEXT_CHARS,
        "referenced_links": [],
    }
    review = await research_semantic_review.review_candidate(
        client=service.client,
        objective=review_objective,
        vehicle={**_vehicle(objective), "vin": objective.get("vin") or None},
        candidate=candidate,
        provider="ADAS SI",
        screenshot=shot,
    )
    if not isinstance(review, dict):
        return None

    sha256 = await asyncio.to_thread(
        lambda: hashlib.sha256(path.read_bytes()).hexdigest()
    )
    source_url = _clean(metadata.get("source_url"), 1000) or candidate["url"]
    provider = _clean(metadata.get("provider"), 80) or "ADAS SI"
    accepted_actual = (
        review.get("decision") in _ACCEPTING
        and review.get("classification") == _ACTUAL
        and review.get("vehicle_match") != "DIFFERENT_VEHICLE"
    )
    document = {
        "role": "primary",
        "title": candidate["title"],
        "url": source_url,
        "provider": provider,
        "accepted": accepted_actual,
        # "captured" means a usable library artifact exists. For a local hit
        # it is already present rather than newly captured this turn.
        "captured": True,
        "classification": review.get("classification"),
        "decision": review.get("decision"),
        "review": review,
        "artifact": {
            "relative_path": row["relative_path"],
            "sha256": sha256,
            "title": candidate["title"],
            "already_present": True,
            "storage_policy": "year/make/model",
        },
    }
    return {
        "review": review,
        "document": document,
        "source_url": source_url,
        "provider": provider,
    }


async def local_procedure(
    service: Any, objective: dict[str, Any]
) -> dict[str, Any] | None:
    """Return a production result only for a complete local actual procedure."""
    if service.adas is None:
        return None
    try:
        result = await asyncio.to_thread(
            service.adas.model_search, _search_args(objective)
        )
    except Exception:  # noqa: BLE001 - library trouble falls through to ALLDATA
        log.warning(
            "ADAS SI preflight failed for %s",
            objective.get("objective_id"),
            exc_info=True,
        )
        return None

    if (
        not isinstance(result, dict)
        or result.get("status") not in {"success", "partial_success"}
    ):
        return None

    for row in _candidate_rows(result):
        reviewed = await _review_local(service, objective, row)
        if reviewed is None:
            continue
        review = reviewed["review"]
        if (
            review.get("decision") not in _ACCEPTING
            or review.get("classification") != _ACTUAL
            or review.get("vehicle_match") == "DIFFERENT_VEHICLE"
        ):
            # A bumper R&I/requirement page can be useful and still be the wrong
            # answer to "give me the BSM calibration procedure." Do not let a
            # related local hit close the procedure objective.
            continue

        dependencies = [
            {
                "title": _clean(item.get("title"), 160),
                "reason": _clean(item.get("reason"), 300),
                "status": "unresolved",
            }
            for item in review.get("dependencies") or []
            if isinstance(item, dict) and _clean(item.get("title"), 160)
        ]
        if dependencies:
            # The local page is real but incomplete. The Navigator owns
            # dependency pursuit, so fall through rather than marking complete.
            return None

        document = reviewed["document"]
        return {
            "status": "verified",
            "verified": True,
            "complete": True,
            "captured": True,
            "requires_human": False,
            "reason": (
                "The exact procedure is already present in the shared "
                "Year/Make/Model ADAS SI library."
            ),
            "evidence_title": document["title"],
            "source_url": reviewed["source_url"],
            "semantic_review": review,
            "documents": [document],
            "dependencies": [],
            "incomplete_reasons": [],
            "task_ids": [],
            "research_receipt": {
                "task_ids": [],
                "visited_urls": [reviewed["source_url"]],
                "candidates": [
                    {"source": "adas_si", "title": document["title"]}
                ],
                "critic_decisions": [
                    {
                        "source": "adas_si",
                        "classification": review.get("classification"),
                        "decision": review.get("decision"),
                        "confidence": review.get("confidence"),
                    }
                ],
                "dependencies": [],
                "stale_action_rejections": 0,
                "artifacts": [document["artifact"]],
                "final_status": "verified",
                "incomplete_reasons": [],
                "metrics": {
                    "local_library_hit": True,
                    "navigator_started": False,
                },
                "actions": [],
                "observation_ids": [],
            },
            "source": "adas_si",
        }
    return None
