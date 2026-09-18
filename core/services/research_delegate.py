"""Delegated research worker behind the ``delegate_research`` capability.

The conversational model hands over one structured objective (plus optional
vehicle, system, deliverable, and source preferences). This worker retrieves
from the sources in order and has every retrieved candidate judged by the
shared semantic evidence evaluator (``research_evidence_contract``) -- the
same evaluator Calibration IQ research uses:

    automotive_knowledge durable claims already promoted to verified from
                         authoritative, hashed, anchored, reviewed evidence;
                         checked first so a settled answer is reused rather
                         than researched again
    adas_si              the local authoritative OEM/service-information library
    web                  public OEM web search with bounded page reads

Retrieval is not an answer. A source's hit only counts when the evaluator
says it answers the objective for this exact vehicle and system; the result
carries one operational outcome -- SATISFIED, PARTIAL, or UNSATISFIED -- and
each finding says whether it was accepted. The worker stops at the first
SATISFIED source unless ``exhaustive``.

A SATISFIED answer anchored in the authoritative ADAS SI library is offered
to the trusted durable-knowledge promotion (``learn``), which applies its own
gates; model inference never reaches it.

Structural only: no user prose is parsed here. ALLDATA is sunset and is not a
source (``alldata_sunset``). The worker never writes to Calibration IQ.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from typing import Any, Callable, Optional

from . import research_evidence_contract as contract

log = logging.getLogger("xomni.research_delegate")

DEFAULT_SOURCE_ORDER: tuple[str, ...] = (
    "automotive_knowledge",
    "adas_si",
    "web",
)
MAX_FINDINGS = 8
EXCERPT_CHARS = 1_500
# ADAS SI pages keep their line structure and get room for a whole table page;
# every excerpt together stays well inside the model's tool-result budget so no
# finding is dropped to make room for another's text.
ADAS_EXCERPT_CHARS = 3_200
TOTAL_EXCERPT_CHARS = 7_000
MIN_EXCERPT_CHARS = 400
WEB_REVIEW_CHARS = 8_000
# Each review is one model call; the worker stays bounded however many
# documents a source returns.
MAX_REVIEWS_PER_SOURCE = 3
MAX_REVIEWS_TOTAL = 6
# Returned with every result. The findings are evidence for X to interpret, not
# text to relay: live, X pasted a flattened OCR table into its answer and then
# answered from general knowledge instead of from what the table said.
READING_GUIDE = (
    "outcome is what the evidence established: SATISFIED answers the objective for "
    "this vehicle and system, PARTIAL answers part of it (see unresolved), "
    "UNSATISFIED answers none of it. Only findings with accepted=true establish "
    "facts; a finding that was not accepted shows only that the document exists, and "
    "its review says why. Findings are listed in source-authority order. Excerpts keep "
    "the source's line structure; in OCR'd tables ' | ' separates columns and each "
    "row lines up with the header row above it. Answer Otis in your own words from "
    "the accepted evidence; quote or show raw excerpts only when he asks to see the "
    "source. Result counts are not library inventory. sources_checked lists every "
    "source searched; never say any other source was searched or found nothing."
)
_COLUMN_GAP_RE = re.compile(r"[ \t]{3,}")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _clean_lines(value: Any, limit: int) -> str:
    """Normalize spacing but keep rows: collapsing a table to one line destroys it."""
    lines = []
    for raw_line in str(value or "").splitlines():
        cells = [" ".join(cell.split()) for cell in _COLUMN_GAP_RE.split(raw_line.strip())]
        line = " | ".join(cell for cell in cells if cell)
        if line:
            lines.append(line)
    return "\n".join(lines)[:limit]


def _bound_excerpts(findings: list[dict[str, Any]]) -> None:
    """Share one excerpt budget across findings, in authority order."""
    remaining = TOTAL_EXCERPT_CHARS
    for finding in findings:
        excerpt = finding.get("excerpt")
        if not isinstance(excerpt, str):
            continue
        allowed = max(MIN_EXCERPT_CHARS, remaining)
        if len(excerpt) > allowed:
            finding["excerpt"] = excerpt[:allowed].rstrip()
            finding["excerpt_truncated"] = True
        remaining = max(0, remaining - len(finding["excerpt"]))


def _vehicle(args: dict[str, Any]) -> dict[str, Any]:
    raw = args.get("vehicle") if isinstance(args.get("vehicle"), dict) else {}
    vehicle: dict[str, Any] = {}
    year = raw.get("year")
    if isinstance(year, int) and not isinstance(year, bool):
        vehicle["year"] = year
    for field in ("make", "model", "trim"):
        value = _clean(raw.get(field), 120)
        if value:
            vehicle[field] = value
    # A VIN identifies one vehicle including trim and engine. Kept apart from
    # the label so it never becomes part of a search phrase.
    vin = "".join(_clean(raw.get("vin"), 32).split()).upper()
    if vin:
        vehicle["vin"] = vin
    label = " ".join(
        str(vehicle[field]) for field in ("year", "make", "model", "trim") if field in vehicle
    )
    if label:
        vehicle["label"] = label
    return vehicle


def source_order(args: dict[str, Any]) -> list[str]:
    preferred = args.get("sources")
    order = [
        source
        for source in (preferred if isinstance(preferred, list) else [])
        if source in DEFAULT_SOURCE_ORDER
    ]
    if not order:
        order = list(DEFAULT_SOURCE_ORDER)
    # Settled, verified knowledge is always consulted first unless Otis
    # excluded it: live, the model's own source list skipped it and the
    # library was researched again for an answer already on record.
    if "automotive_knowledge" in order:
        order.remove("automotive_knowledge")
    order.insert(0, "automotive_knowledge")
    excluded = args.get("exclude_sources")
    excluded = set(excluded) if isinstance(excluded, list) else set()
    return [source for source in order if source not in excluded]


def _library_vehicle(value: Any) -> Optional[dict[str, Any]]:
    """The structured Year/Make/Model a source is filed under, when it has one."""

    if not isinstance(value, dict):
        return None
    filing = {
        key: value.get(key)
        for key in ("year", "year_start", "year_end", "make", "manufacturer", "model")
        if value.get(key) not in (None, "")
    }
    return filing or None


def _adas_findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    for item in (result.get("results") or [])[:MAX_FINDINGS]:
        if not isinstance(item, dict):
            continue
        extraction = item.get("text_extraction") if isinstance(item.get("text_extraction"), dict) else {}
        finding = {
            "source": "adas_si",
            "title": _clean(item.get("title") or item.get("source"), 200),
            "relative_path": item.get("relative_path"),
            "page": item.get("page"),
            "excerpt": _clean_lines(item.get("excerpt"), ADAS_EXCERPT_CHARS),
            "excerpt_truncated": True if item.get("excerpt_truncated") else None,
            "text_method": extraction.get("method"),
            "ocr_confidence": (
                round(float(extraction["confidence"]), 2)
                if extraction.get("method") == "ocr"
                and isinstance(extraction.get("confidence"), (int, float))
                else None
            ),
            "url": item.get("url"),
            "library_vehicle": _library_vehicle(item.get("vehicle")),
        }
        if item.get("evidence_id"):
            finding["evidence_id"] = item["evidence_id"]
        findings.append({key: value for key, value in finding.items() if value not in (None, "")})
    return findings


def _knowledge_text(record: dict[str, Any]) -> str:
    """A durable record as reviewable text: its claim, then the source text it rests on."""

    requirement = record.get("requirement") if isinstance(record.get("requirement"), dict) else {}
    application = record.get("application") if isinstance(record.get("application"), dict) else {}
    system = record.get("system") if isinstance(record.get("system"), dict) else {}
    years = (
        str(application.get("year_start"))
        if application.get("year_start") == application.get("year_end")
        else f"{application.get('year_start')}-{application.get('year_end')}"
    )
    lines = [
        f"Applies to: {years} {application.get('manufacturer') or ''} {application.get('model') or ''}".strip(),
        f"System: {system.get('name') or 'not stated'}",
        f"Requirement ({requirement.get('requirement_type') or 'not stated'}): {requirement.get('text') or ''}",
    ]
    for key, label in (
        ("procedure_summary", "Procedure summary"),
        ("applicability_notes", "Applicability"),
    ):
        if requirement.get(key):
            lines.append(f"{label}: {requirement[key]}")
    for item in record.get("evidence") or []:
        if not isinstance(item, dict) or item.get("verification_effective") is not True:
            continue
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        where = f"{source.get('source_name') or 'source'}" + (
            f", page {item['page_start']}" if item.get("page_start") else ""
        )
        if item.get("excerpt"):
            lines.append(f"Source text ({where}): {item['excerpt']}")
    return "\n".join(line for line in lines if line)


def _knowledge_findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    for record in (result.get("records") or [])[:MAX_FINDINGS]:
        if not isinstance(record, dict):
            continue
        provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
        sources = record.get("sources") if isinstance(record.get("sources"), list) else []
        requirement = record.get("requirement") if isinstance(record.get("requirement"), dict) else {}
        application = record.get("application") if isinstance(record.get("application"), dict) else {}
        finding = {
            "source": "automotive_knowledge",
            "record_id": record.get("id") or record.get("record_id"),
            "title": _clean(
                record.get("title")
                or requirement.get("text")
                or record.get("claim")
                or record.get("summary"),
                200,
            ),
            "lifecycle": record.get("lifecycle"),
            "excerpt": _clean_lines(
                _knowledge_text(record)
                if requirement
                else record.get("procedure")
                or record.get("summary")
                or record.get("claim")
                or record.get("statement"),
                EXCERPT_CHARS,
            ),
            "provenance": provenance or (sources[:2] if sources else None),
            "library_vehicle": _library_vehicle(application),
        }
        if record.get("evidence_id"):
            finding["evidence_id"] = record["evidence_id"]
        findings.append({key: value for key, value in finding.items() if value not in (None, "", {}, [])})
    return findings


def _web_findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    reads = {
        str(item.get("url")): item
        for item in (result.get("read_results") or [])
        if isinstance(item, dict) and item.get("url")
    }
    for source in (result.get("sources") or [])[:MAX_FINDINGS]:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "")
        read = reads.get(url) or {}
        page_text = str(read.get("page_text") or source.get("snippet") or "")
        finding = {
            "source": "web",
            "title": _clean(read.get("title") or source.get("title"), 200),
            "url": url,
            "excerpt": _clean(page_text, EXCERPT_CHARS),
            "provider": source.get("provider"),
            # Kept for the review only; never sent to the model or the card.
            "_review_text": page_text[:WEB_REVIEW_CHARS],
        }
        findings.append({key: value for key, value in finding.items() if value not in (None, "")})
    return findings


# Result fields the model reads above the findings. The request echo (objective,
# vehicle, depth, source order) is left out: live, X searched with an invented
# vehicle and then read the chart against that echo instead of the question.
_MODEL_HEADER_KEYS = (
    "outcome",
    "status",
    "verified",
    "unresolved",
    "sources_checked",
    "source_ledger",
    "finding_count",
    "accepted_count",
    "evidence_ids",
    "learned",
    "message",
    "reading_guide",
)
_FINDING_DETAIL_KEYS = (
    "accepted",
    "evaluation",
    "relative_path",
    "record_id",
    "lifecycle",
    "provenance",
)


def result_text_for_model(result: Any, *, max_chars: int) -> str:
    """Render a research result for the model: a small JSON header, then text.

    Inside JSON an OCR table is one long string of escaped newlines, and the
    live 30B worker read the Hyundai/Kia/Genesis bumper chart that way as
    having no bumper information at all. Each finding's excerpt is therefore
    given as its own block with real line breaks; the full structured result
    still reaches the card, the store, and the evidence record unchanged.
    """

    if not isinstance(result, dict):
        return json.dumps(result, default=str)[:max_chars]
    header = {key: result[key] for key in _MODEL_HEADER_KEYS if key in result}
    parts = [json.dumps(header, ensure_ascii=False, default=str)]
    used = len(parts[0])
    findings = result.get("findings") if isinstance(result.get("findings"), list) else []
    for index, finding in enumerate(findings, start=1):
        if not isinstance(finding, dict):
            continue
        label = [str(finding.get("source") or "source")]
        if finding.get("page"):
            label.append(f"page {finding['page']}")
        if finding.get("text_method") == "ocr":
            confidence = finding.get("ocr_confidence")
            label.append("OCR" if confidence is None else f"OCR confidence {confidence}")
        if finding.get("excerpt_truncated"):
            label.append("excerpt shortened")
        label.append("accepted" if finding.get("accepted") else "not accepted")
        title = finding.get("title") or finding.get("url") or "Untitled"
        block = [f"--- Finding {index}: {title} ({', '.join(label)}) ---"]
        details = {
            key: finding[key]
            for key in _FINDING_DETAIL_KEYS
            if finding.get(key) not in (None, "", [], {})
        }
        if details:
            block.append(json.dumps(details, ensure_ascii=False, default=str))
        block.append(str(finding.get("excerpt") or "(no text extracted)"))
        text = "\n".join(block)
        if used + len(text) + 2 > max_chars:
            parts.append(f"--- {len(findings) - index + 1} more finding(s) omitted for length ---")
            break
        parts.append(text)
        used += len(text) + 2
    return "\n\n".join(parts)[:max_chars]


_STATUS_FOR_OUTCOME = {
    contract.SATISFIED: "satisfied",
    contract.PARTIAL: "partial",
    contract.UNSATISFIED: "unsatisfied",
}


def make_delegate_research(
    settings: Any,
    *,
    adas_search: Callable[[dict[str, Any]], Any],
    knowledge_search: Callable[[dict[str, Any]], Any],
    public_search: Optional[Callable[..., Any]] = None,
    adas: Any = None,
    learn: Optional[Callable[..., Any]] = None,
    evaluator: Optional[Callable[..., Any]] = None,
    client_provider: Optional[Callable[[], Any]] = None,
) -> Callable[[dict[str, Any]], Any]:
    """Build the handler with its sources injected (tests supply fakes).

    ``adas`` (the ADAS SI service) lets a procedure objective be reviewed on
    the whole library document, exactly as Calibration IQ research reviews
    it. ``learn`` is the trusted durable-knowledge promotion. ``evaluator``
    and ``client_provider`` default to the shared evaluator and the model
    bound for the current tool call.
    """

    evaluate = evaluator or contract.evaluate
    current_client = client_provider or contract.current_model_client

    async def _candidate(
        finding: dict[str, Any], deliverable: str
    ) -> tuple[dict[str, Any], Optional[tuple[bytes, str]]]:
        candidate: dict[str, Any] = {
            "title": finding.get("title"),
            "url": finding.get("url"),
            "page": finding.get("page"),
            "text": finding.get("_review_text") or finding.get("excerpt") or "",
            "library_vehicle": finding.get("library_vehicle"),
        }
        screenshot = None
        if finding.get("source") == "adas_si" and adas is not None and finding.get("relative_path"):
            from . import adas_si_research_source_cascade as cascade

            if deliverable == "procedure":
                prepared = await cascade.prepare_library_candidate(
                    adas,
                    str(finding["relative_path"]),
                    page=int(finding.get("page") or 1),
                    title=str(finding.get("title") or ""),
                    url=finding.get("url"),
                )
                if prepared is not None:
                    candidate.update(prepared["candidate"])
                    screenshot = prepared["screenshot"]
            else:
                try:
                    path = adas.resolve_relative(str(finding["relative_path"]))
                    screenshot = await cascade._screenshot(adas, path, int(finding.get("page") or 1))  # noqa: SLF001
                except Exception:  # noqa: BLE001 - the text review still runs
                    screenshot = None
        return candidate, screenshot

    async def delegate_research(args: dict[str, Any]) -> dict[str, Any]:
        objective = _clean(args.get("objective"), 600)
        if len(objective) < 3:
            raise ValueError("objective is required")
        vehicle = _vehicle(args)
        deliverable = contract.normalize_deliverable(args.get("deliverable"))
        depth = str(args.get("depth") or "standard").strip().casefold()
        if depth not in {"standard", "calibration_requirements", "repair_policy"}:
            depth = "standard"
        exhaustive = args.get("exhaustive") is True
        order = source_order(args)
        system = _clean(args.get("system"), 200) or None
        component = _clean(args.get("component"), 200) or None
        review_objective = {"objective": objective, "system": system, "component": component}
        client = current_client()

        ledger: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        evidence_ids: list[str] = []
        outcomes: list[str] = []
        unresolved: list[str] = []
        reviews_used = 0
        satisfied_evidence: Optional[dict[str, Any]] = None

        for source in order:
            entry: dict[str, Any] = {"source": source, "attempted": True, "outcome": contract.UNSATISFIED}
            source_findings: list[dict[str, Any]] = []
            try:
                if source == "adas_si":
                    query: dict[str, Any] = {"question": objective}
                    scoped = {
                        key: vehicle[key]
                        for key in ("year", "make", "model", "trim")
                        if key in vehicle
                    }
                    if scoped:
                        query["vehicle"] = scoped
                    for field, value in (("system", system), ("component", component)):
                        if value:
                            query[field] = value
                    if depth == "calibration_requirements" or deliverable == "procedure":
                        query["search_mode"] = "calibration_requirements"
                    result = await _maybe_await(adas_search(query))
                    result = result if isinstance(result, dict) else {}
                    entry["retrieval_status"] = result.get("status")
                    source_findings = _adas_findings(result)
                    if result.get("evidence_id"):
                        evidence_ids.append(str(result["evidence_id"]))
                elif source == "automotive_knowledge":
                    # Durable records are found by the vehicle they apply to;
                    # whether one answers this objective is the evaluator's call.
                    query = {"limit": MAX_FINDINGS}
                    if all(field in vehicle for field in ("year", "make", "model")):
                        query.update(
                            {
                                "year": vehicle["year"],
                                "manufacturer": vehicle["make"],
                                "model": vehicle["model"],
                            }
                        )
                    else:
                        query["query"] = objective
                    result = await _maybe_await(knowledge_search(query))
                    result = result if isinstance(result, dict) else {}
                    entry["retrieval_status"] = result.get("status")
                    source_findings = _knowledge_findings(result)
                    if result.get("evidence_id"):
                        evidence_ids.append(str(result["evidence_id"]))
                elif source == "web":
                    from . import research_workflow

                    search_web = public_search or research_workflow.search_public_oem
                    query_text = " ".join(part for part in (vehicle.get("label"), objective) if part)
                    result = await _maybe_await(
                        search_web(query_text, vehicle.get("make"), source_depth=depth)
                    )
                    result = result if isinstance(result, dict) else {}
                    source_findings = _web_findings(result)
                    entry["retrieval_status"] = "searched" if result.get("searched") else "not_searched"
                    entry["providers"] = result.get("providers")
                else:  # pragma: no cover - schema enum prevents this
                    entry.update({"attempted": False, "retrieval_status": "unknown_source"})
            except Exception as exc:  # noqa: BLE001 - one source failing is a ledger fact
                log.warning("delegate_research source %s failed", source, exc_info=True)
                entry["retrieval_status"] = "error"
                entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
            entry["retrieved"] = len(source_findings)

            # Retrieval is not an answer: the evaluator decides, one candidate
            # at a time in authority order, until one satisfies the objective.
            source_outcomes: list[str] = []
            reviewed_documents: set[str] = set()
            for finding in source_findings:
                identity = str(
                    finding.get("relative_path") or finding.get("record_id") or finding.get("url") or id(finding)
                )
                if (
                    identity in reviewed_documents
                    or len(reviewed_documents) >= MAX_REVIEWS_PER_SOURCE
                    or reviews_used >= MAX_REVIEWS_TOTAL
                ):
                    finding["accepted"] = False
                    finding["evaluation"] = {"outcome": None, "reasons": ["not reviewed"]}
                    continue
                reviewed_documents.add(identity)
                reviews_used += 1
                candidate, screenshot = await _candidate(finding, deliverable)
                try:
                    evaluation = await evaluate(
                        client=client,
                        objective=review_objective,
                        vehicle=vehicle,
                        candidate=candidate,
                        provider={"adas_si": "ADAS SI", "automotive_knowledge": "durable automotive knowledge", "web": "public web"}.get(source, source),
                        deliverable=deliverable,
                        screenshot=screenshot,
                    )
                except Exception as exc:  # noqa: BLE001 - an evaluator failure is never an acceptance
                    log.warning("evidence evaluation failed", exc_info=True)
                    evaluation = {
                        "outcome": contract.UNSATISFIED,
                        "deliverable": deliverable,
                        "reasons": [f"evaluation failed: {type(exc).__name__}"],
                    }
                outcome = evaluation.get("outcome") if evaluation.get("outcome") in contract.OUTCOMES else contract.UNSATISFIED
                source_outcomes.append(outcome)
                finding["accepted"] = outcome in {contract.SATISFIED, contract.PARTIAL}
                finding["evaluation"] = contract.compact_evaluation(evaluation)
                if outcome == contract.PARTIAL:
                    unresolved.extend(evaluation.get("unresolved") or [])
                if outcome == contract.SATISFIED:
                    if satisfied_evidence is None and source == "adas_si":
                        satisfied_evidence = {"finding": finding, "evaluation": evaluation, "candidate": candidate}
                    break
            entry["outcome"] = contract.combine(source_outcomes)
            entry["reviewed"] = len(source_outcomes)
            ledger.append(entry)
            outcomes.append(entry["outcome"])
            findings.extend(source_findings[: max(0, MAX_FINDINGS - len(findings))])
            for finding in source_findings:
                for key in ("evidence_id", "record_id"):
                    if finding.get(key):
                        evidence_ids.append(str(finding[key]))
            if entry["outcome"] == contract.SATISFIED and not exhaustive:
                break

        for finding in findings:
            finding.pop("_review_text", None)
        _bound_excerpts(findings)
        outcome = contract.combine(outcomes)
        checked = [item["source"] for item in ledger if item.get("attempted")]

        learned = None
        if outcome == contract.SATISFIED and satisfied_evidence is not None and learn is not None:
            try:
                learned = await _maybe_await(
                    learn(
                        objective=objective,
                        vehicle=vehicle,
                        system=system,
                        component=component,
                        finding=satisfied_evidence["finding"],
                        evaluation=satisfied_evidence["evaluation"],
                        candidate=satisfied_evidence["candidate"],
                    )
                )
            except Exception as exc:  # noqa: BLE001 - learning never changes the answer
                log.warning("durable knowledge promotion failed", exc_info=True)
                learned = {"promoted": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}

        unresolved_unique = list(dict.fromkeys(item for item in unresolved if item))[:6]
        return {
            "outcome": outcome,
            "status": _STATUS_FOR_OUTCOME[outcome],
            # Retrieval is not verification: only a SATISFIED objective is.
            "verified": outcome == contract.SATISFIED,
            "objective": objective,
            "deliverable": deliverable,
            "vehicle": vehicle or None,
            "system": system,
            "component": component,
            "depth": depth,
            "source_order": order,
            "sources_checked": checked,
            "source_ledger": ledger,
            "findings": findings,
            "finding_count": len(findings),
            "accepted_count": sum(1 for item in findings if item.get("accepted")),
            "unresolved": unresolved_unique or None,
            "reading_guide": READING_GUIDE,
            "evidence_ids": sorted(set(evidence_ids)),
            "learned": (
                {key: learned.get(key) for key in ("promoted", "record_id", "reason") if learned.get(key) is not None}
                if isinstance(learned, dict)
                else None
            ),
            "mutated_calibration_iq": False,
            "message": {
                contract.SATISFIED: "The accepted evidence answers the objective for this vehicle and system.",
                contract.PARTIAL: "The accepted evidence answers part of the objective; the rest is unresolved.",
                contract.UNSATISFIED: (
                    "Nothing retrieved answers the objective for this vehicle and system; "
                    "this is a miss in the sources checked only."
                ),
            }[outcome],
        }

    return delegate_research
