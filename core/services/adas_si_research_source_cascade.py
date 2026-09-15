"""Local-library-first source cascade for CIQ-bound service-information research.

Every Calibration IQ calibration requirement is checked against the shared
Year/Make/Model ADAS SI library before licensed browsing begins. A local hit
only satisfies the objective when X's independent semantic review says it is
the ACTUAL_PROCEDURE for that exact vehicle/system. Related requirements,
R&I pages, descriptions, diagnostics, and supporting information never suppress
the ALLDATA fallback for a missing calibration procedure.

The existing ALLDATA Navigator remains the fallback. It already receives the
exact CIQ/ADAS-Map VIN and its vehicle-anchor forces a mechanical VIN selection
before X is allowed to navigate. Accepted ALLDATA captures are stored by
ScrapeX in the same shared Year/Make/Model ADAS SI library, then attached from
that library artifact to the exact RO/calibration item.
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

_INSTALLED_ATTR = "__xomni_adas_si_research_source_cascade_v1__"
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


def _search_args(objective: dict[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {
        "vehicle": _vehicle(objective),
        "question": _clean(objective.get("topic"), 500),
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

    metadata = await asyncio.to_thread(_source_metadata, path)
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


async def _local_procedure(
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


def _source_aware_attach_factory(research_module: Any):
    """Preserve the normal CIQ attach/reread gate with truthful provenance."""

    def default_attach(settings: Any, adas: Any):
        async def attach(
            objective: dict[str, Any],
            document: dict[str, Any],
            context: dict[str, Any],
        ) -> dict[str, Any]:
            from . import calibration_iq

            artifact = document.get("artifact") or {}
            relative = (
                str(artifact.get("relative_path") or "")
                .strip()
                .replace("\\", "/")
            )
            if not relative:
                return {"attached": False, "status": "no_artifact"}
            try:
                source_path = adas.resolve_relative(relative)
            except Exception as exc:  # noqa: BLE001
                return {
                    "attached": False,
                    "status": "artifact_unresolvable",
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                }

            source_uri = f"adas-si:///{quote(relative)}"
            ro_id = str(objective.get("repair_order_id") or "")
            before = await calibration_iq.get_repair_order(
                settings, {"repair_order_id": ro_id}
            )
            if (
                before.get("status") != "verified"
                or not isinstance(before.get("raw"), dict)
            ):
                return {
                    "attached": False,
                    "status": "ro_unreadable",
                    "message": before.get("message"),
                }

            existing = calibration_iq._existing_research_documents(  # noqa: SLF001
                before["raw"]
            )
            already = next(
                (
                    item
                    for item in existing
                    if str(item.get("source_uri") or "").strip().casefold()
                    == source_uri.casefold()
                ),
                None,
            )
            if already is not None:
                return {
                    "attached": True,
                    "status": "already_attached",
                    "document_id": already.get("id")
                    or already.get("document_id"),
                    "source_uri": source_uri,
                }

            review = (
                document.get("review")
                if isinstance(document.get("review"), dict)
                else {}
            )
            status = (
                "validated"
                if review.get("decision") in _ACCEPTING
                and float(review.get("confidence") or 0) >= 0.8
                else "candidate"
            )
            provider = _clean(document.get("provider"), 80) or (
                "ADAS SI"
                if artifact.get("already_present")
                else "ALLDATA"
            )
            source_url = _clean(document.get("url"), 1000) or source_uri
            origin_note = (
                "Reused from the shared ADAS SI library"
                if artifact.get("already_present")
                else "Captured into the shared ADAS SI library"
            )
            arguments: dict[str, Any] = {
                "source_path": str(source_path),
                "document_type": "oem_procedure",
                "semantic_type": "OEM_PROCEDURE",
                "evidence_role": "PROCEDURE",
                "title": _clean(
                    document.get("title")
                    or artifact.get("title")
                    or objective.get("calibration_title"),
                    255,
                ),
                "source_uri": source_uri,
                "source_name": relative.rsplit("/", 1)[-1][:255],
                "page_references": [],
                "citation": _clean(f"{provider}, {source_url}", 500),
                "notes": _clean(
                    f"{origin_note} by X's service-information research. "
                    f"Reviewer: {review.get('classification') or ''} / "
                    f"{review.get('decision') or ''} "
                    f"({review.get('confidence')}). "
                    f"{review.get('evidence_summary') or ''}",
                    1500,
                ),
                "status": status,
            }
            if objective.get("calibration_id"):
                arguments["calibration_item_ids"] = [
                    str(objective["calibration_id"])
                ]

            actions = [
                {
                    "operation": "ensure_case_workspace",
                    "repair_order_id": ro_id,
                    "arguments": {},
                },
                {
                    "operation": "import_document",
                    "repair_order_id": ro_id,
                    "arguments": arguments,
                },
            ]
            digest = str(artifact.get("sha256") or "")[:12]
            invocation = dict(context)
            invocation["tool_call_id"] = (
                f"{context.get('tool_call_id') or 'adas_si_research'}:"
                f"{objective.get('objective_id')}:{digest or 'library'}"
            )
            result = await calibration_iq.operator_execute(
                settings,
                adas,
                {
                    "actions": actions,
                    research_module.INVOCATION_KEY: invocation,
                },
            )
            after = await calibration_iq.get_repair_order(
                settings, {"repair_order_id": ro_id}
            )
            attached_document = None
            if (
                after.get("status") == "verified"
                and isinstance(after.get("raw"), dict)
            ):
                for item in calibration_iq._existing_research_documents(  # noqa: SLF001
                    after["raw"]
                ):
                    if (
                        str(item.get("source_uri") or "").strip().casefold()
                        == source_uri.casefold()
                    ):
                        attached_document = item
                        break

            return {
                "attached": attached_document is not None,
                "status": (
                    "attached"
                    if attached_document is not None
                    else "not_confirmed"
                ),
                "document_id": (attached_document or {}).get("id")
                or (attached_document or {}).get("document_id"),
                "document_status": status,
                "source_uri": source_uri,
                "receipt_status": (
                    result.get("status")
                    if isinstance(result, dict)
                    else None
                ),
                "receipt_message": _clean(
                    (
                        (result or {}).get("message")
                        if isinstance(result, dict)
                        else ""
                    ),
                    300,
                )
                or None,
                "calibration_item_id": objective.get("calibration_id"),
            }

        return attach

    return default_attach


def install(research_module: Any) -> None:
    if getattr(research_module, _INSTALLED_ATTR, False):
        return

    service_class = research_module.AdasSiResearchService
    original_research = service_class._research
    original_start = service_class.start

    async def research_library_first(
        self: Any, objective: dict[str, Any]
    ) -> dict[str, Any]:
        local = await _local_procedure(self, objective)
        if local is not None:
            return local

        # The existing fallback receives objective["vin"]. The installed
        # Navigator vehicle-anchor then forces `select_vehicle(vin=...)` before
        # the model can choose links or interpret the ALLDATA page.
        return await original_research(self, objective)

    async def start_with_source_cascade(
        self: Any, args: dict[str, Any]
    ) -> dict[str, Any]:
        result = await original_start(self, args)
        if isinstance(result, dict) and result.get("status") == "running":
            count = int(result.get("objective_count") or 0)
            result["message"] = (
                f"Started service-information research: {count} procedure "
                "objective(s). X checks the shared Year/Make/Model ADAS SI "
                "library first for each requirement. Only objectives without "
                "a semantically confirmed actual procedure escalate to "
                "ALLDATA, where the exact RO VIN is selected before navigation. "
                "Accepted external procedures are saved back into ADAS SI and "
                "the library artifact is attached to the RO. Results post here "
                "when the background job finishes."
            )
        return result

    service_class._research = research_library_first
    service_class.start = start_with_source_cascade
    research_module.default_attach = _source_aware_attach_factory(
        research_module
    )
    setattr(research_module, _INSTALLED_ATTR, True)
