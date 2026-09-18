"""The ADAS SI library as the source for CIQ-bound service-information research.

Every Calibration IQ calibration requirement is checked against the shared
Year/Make/Model ADAS SI library. A library hit is retrieval, not an answer:
each candidate is judged by the shared semantic evidence evaluator
(``research_evidence_contract.evaluate`` with ``deliverable="procedure"``),
the same evaluator ordinary chat research uses. The objective ends

* SATISFIED -- the reviewer accepted the page as the actual procedure for this
  vehicle and system;
* PARTIAL -- it is the actual procedure but names a required supporting
  document that is not resolved;
* UNSATISFIED -- nothing in the library is the procedure. Related
  requirements, R&I pages, descriptions, diagnostics, and supporting
  information never close a procedure objective.

A library shared by Year/Make/Model does not make every procedure in it
evidence for every calibration on the vehicle. ScrapeX capture sidecars record
the research objective that produced an artifact, including its
``calibration_item_id``. That stored id is provenance, not semantics: an
artifact captured for one CIQ calibration item is never reused for a different
one. Legacy or manual library files without captured objective provenance stay
eligible for review.

ALLDATA is sunset; there is no licensed-browser fallback after a library miss.
``AdasSiResearchService._research`` calls ``local_procedure`` directly, and
``prepare_library_candidate`` is shared with ``delegate_research`` so both
paths review exactly the same evidence.
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
# Library hits considered per objective, and how many of them may be sent to
# the reviewer. A vehicle's own folder often ranks its other procedures above
# the one asked for, so hits captured for a different calibration item are
# refused by provenance first and never use a review slot.
_LOCAL_DOC_LIMIT = 12
_LOCAL_REVIEW_LIMIT = 5


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


async def prepare_library_candidate(
    adas: Any, relative_path: str, *, page: int = 1, title: str = "", url: Any = None
) -> dict[str, Any] | None:
    """Build the reviewer's candidate for one library document, or None.

    The text is ScrapeX's exact extracted text when a capture sidecar exists,
    otherwise the shared cached PDF/OCR pages ADAS SI search uses; a rendered
    page image rides along for visual review of scans and charts.
    """
    try:
        path = adas.resolve_relative(relative_path)
        metadata = await asyncio.to_thread(_source_metadata, path)
        text = await _library_text(adas, path)
        shot = await _screenshot(adas, path, int(page or 1))
    except Exception:  # noqa: BLE001 - one bad library file must not stop the rest
        log.warning("Could not prepare ADAS SI candidate %s", relative_path, exc_info=True)
        return None
    return {
        "path": path,
        "metadata": metadata,
        "screenshot": shot,
        "candidate": {
            # The capture sidecar keeps the provider's own page title; the file
            # name is only a storage identity with a timestamp on it.
            "title": _clean(metadata.get("title"), 300) or title or path.stem,
            "breadcrumb": list(Path(relative_path).parts[:-1]),
            "url": url or f"adas-si:///{quote(relative_path)}",
            "text": text,
            "text_truncated": len(text) >= _LOCAL_TEXT_CHARS,
            "referenced_links": [],
            "page": int(page or 1),
        },
    }


async def _review_local(
    service: Any,
    objective: dict[str, Any],
    row: dict[str, Any],
) -> dict[str, Any] | None:
    if service.adas is None or service.client is None:
        return None
    prepared = await prepare_library_candidate(
        service.adas,
        row["relative_path"],
        page=int(row.get("page") or 1),
        title=str(row.get("title") or ""),
        url=row.get("url"),
    )
    if prepared is None:
        return None
    path, metadata, candidate = prepared["path"], prepared["metadata"], prepared["candidate"]
    if not provenance_allows_reuse(metadata, objective):
        return None

    from . import research_evidence_contract as contract

    review_objective = {
        "objective": objective.get("topic"),
        "requirement_label": objective.get("requirement_label") or objective.get("calibration_title"),
        "system": objective.get("system"),
        "component": objective.get("calibration_title"),
        "repair_order": objective.get("ro_number"),
        "calibration_item_id": objective.get("calibration_id"),
    }
    evaluation = await contract.evaluate(
        client=service.client,
        objective=review_objective,
        vehicle={**_vehicle(objective), "vin": objective.get("vin") or None},
        candidate=candidate,
        provider="ADAS SI",
        deliverable="procedure",
        screenshot=prepared["screenshot"],
    )
    review = evaluation.get("review")
    if not isinstance(review, dict):
        return None

    sha256 = await asyncio.to_thread(
        lambda: hashlib.sha256(path.read_bytes()).hexdigest()
    )
    source_url = _clean(metadata.get("source_url"), 1000) or candidate["url"]
    provider = _clean(metadata.get("provider"), 80) or "ADAS SI"
    document = {
        "role": "primary",
        "title": candidate["title"],
        "url": source_url,
        "provider": provider,
        "accepted": evaluation["outcome"] in {contract.SATISFIED, contract.PARTIAL},
        # "captured" means a usable library artifact exists. For a local hit
        # it is already present rather than newly captured this turn.
        "captured": True,
        "classification": review.get("classification"),
        "decision": review.get("decision"),
        "outcome": evaluation["outcome"],
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
        "evaluation": evaluation,
        "document": document,
        "source_url": source_url,
        "provider": provider,
    }


def _provenance_refusal(service: Any, objective: dict[str, Any], row: dict[str, Any]) -> str:
    """Why a library hit may not be reused for this objective, or ''."""
    try:
        path = service.adas.resolve_relative(row["relative_path"])
        metadata = _source_metadata(path)
    except Exception:  # noqa: BLE001 - the review step owns unreadable files
        return ""
    if provenance_allows_reuse(metadata, objective):
        return ""
    return f"captured for calibration item {captured_calibration_id(metadata)}, not this one"


def _review_record(
    row: dict[str, Any], review: dict[str, Any], outcome: Any = None
) -> dict[str, Any]:
    return {
        "relative_path": row.get("relative_path"),
        "title": row.get("title"),
        "outcome": outcome,
        "decision": review.get("decision"),
        "classification": review.get("classification"),
        "objective_match": review.get("objective_match"),
        "vehicle_match": review.get("vehicle_match"),
        "reason": _clean(review.get("evidence_summary"), 240) or None,
    }


async def local_procedure(
    service: Any,
    objective: dict[str, Any],
    reviews: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Return the research result for a SATISFIED or PARTIAL library procedure.

    None means UNSATISFIED: no library document is the procedure. ``reviews``,
    when given, receives one entry per library hit considered -- the
    evaluator's outcome and verdict, or why the hit was not reviewed -- so a
    requirement that stays unsatisfied says exactly what the library held.
    """
    from . import research_evidence_contract as contract

    reviews = reviews if reviews is not None else []
    if service.adas is None:
        return None
    try:
        result = await asyncio.to_thread(
            service.adas.model_search, _search_args(objective)
        )
    except Exception:  # noqa: BLE001 - library trouble is an unsatisfied objective
        log.warning(
            "ADAS SI library search failed for %s",
            objective.get("objective_id"),
            exc_info=True,
        )
        return None

    if (
        not isinstance(result, dict)
        or result.get("status") not in {"success", "partial_success"}
    ):
        return None

    partial: dict[str, Any] | None = None
    reviewed_count = 0
    for row in _candidate_rows(result):
        refusal = await asyncio.to_thread(_provenance_refusal, service, objective, row)
        if refusal:
            reviews.append({"relative_path": row.get("relative_path"), "title": row.get("title"), "not_reviewed": refusal})
            continue
        if reviewed_count >= _LOCAL_REVIEW_LIMIT:
            reviews.append({"relative_path": row.get("relative_path"), "title": row.get("title"), "not_reviewed": "review limit reached"})
            continue
        reviewed = await _review_local(service, objective, row)
        if reviewed is None:
            reviews.append({"relative_path": row.get("relative_path"), "title": row.get("title"), "not_reviewed": "could not be prepared for review"})
            continue
        reviewed_count += 1
        review = reviewed["review"]
        outcome = reviewed["evaluation"]["outcome"]
        reviews.append(_review_record(row, review, outcome))
        if outcome == contract.UNSATISFIED:
            # A bumper R&I/requirement page can be useful and still be the wrong
            # answer to "give me the BSM calibration procedure." A related
            # library hit never closes the procedure objective.
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
        built = _library_result(reviewed, outcome, dependencies)
        if outcome == contract.SATISFIED:
            return built
        if partial is None:
            # Keep looking for a complete procedure; report this one if none is.
            partial = built
    return partial


def _library_result(
    reviewed: dict[str, Any], outcome: str, dependencies: list[dict[str, Any]]
) -> dict[str, Any]:
    from . import research_evidence_contract as contract

    review = reviewed["review"]
    document = reviewed["document"]
    complete = outcome == contract.SATISFIED
    incomplete = [
        f"requires {item['title']}, which the ADAS SI library review did not resolve"
        for item in dependencies
    ]
    return {
        "status": "verified" if complete else "partial",
        "outcome": outcome,
        # The document is the reviewed actual procedure either way; "complete"
        # says whether the objective is SATISFIED.
        "verified": True,
        "complete": complete,
        "captured": True,
        "requires_human": False,
        "reason": (
            "The exact procedure is present in the shared Year/Make/Model ADAS SI library."
            if complete
            else "The procedure is in the shared ADAS SI library, but it requires a "
            "supporting document that is not resolved."
        ),
        "evidence_title": document["title"],
        "source_url": reviewed["source_url"],
        "semantic_review": review,
        "documents": [document],
        "dependencies": dependencies,
        "incomplete_reasons": incomplete,
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
                    "outcome": outcome,
                }
            ],
            "dependencies": dependencies,
            "stale_action_rejections": 0,
            "artifacts": [document["artifact"]],
            "final_status": "verified" if complete else "partial",
            "incomplete_reasons": incomplete,
            "metrics": {
                "local_library_hit": True,
                "navigator_started": False,
            },
            "actions": [],
            "observation_ids": [],
        },
        "source": "adas_si",
    }
