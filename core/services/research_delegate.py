"""Delegated automotive research behind the ``delegate_research`` capability.

The conversational model hands over one structured objective (plus optional
vehicle, system, deliverable, and source preferences). The worker has one
research path:

1. internally reuse an applicable verified Automotive Knowledge record when
   available. That database is a semantic cache of ADAS SI, not a competing
   source memory;
2. search the authoritative local ADAS SI source library;
3. search public OEM web when needed and allowed.

Every retrieved candidate is judged by the shared semantic evidence evaluator
(``research_evidence_contract``), the same evaluator Calibration IQ research
uses. Retrieval alone is never an answer. A result is SATISFIED, PARTIAL, or
UNSATISFIED for the exact vehicle/system/objective.

A SATISFIED fact or procedure anchored in ADAS SI is offered to the shared
trusted semantic-cache promotion gate. Model inference never self-verifies a
cache record. Structural only: no user prose is parsed here. ALLDATA is sunset
and is not a source. The worker never writes to Calibration IQ.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from typing import Any, Callable, Optional

from . import research_evidence_contract as contract

log = logging.getLogger("xomni.research_delegate")

# The cache stays first internally. The model-facing schema exposes only ADAS SI
# and web as selectable/excludable source-memory choices; Automotive Knowledge
# is a reuse optimization owned by this research path.
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
    "its review says why. The automotive_knowledge step is only reuse of a verified "
    "ADAS SI semantic cache, not a second source library. Excerpts keep the source's "
    "line structure; in OCR'd tables ' | ' separates columns and each row lines up "
    "with the header row above it. Answer Otis in your own words from accepted "
    "evidence; quote or show raw excerpts only when he asks to see the source. Result "
    "counts are not library inventory. sources_checked lists every research step "
    "consulted; never say any other source was searched or found nothing."
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
    # Settled verified cache data is always consulted first. The normal schema
    # no longer exposes this cache as a source choice, so a user/model source
    # preference controls ADAS SI/web while cache reuse remains internal.
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
    """A verified cache record as reviewable claim plus exact source evidence."""

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
    """Render a research result for the model as a compact header plus text blocks.

    OCR tables lose their meaning when line breaks are escaped inside one JSON
    string. Each finding therefore gets a real multiline block while the full
    structured result remains unchanged for cards, persistence, and audit.
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
    """Build the handler with retrieval/cache dependencies injected.

    ``adas`` lets a procedure objective be reviewed on the whole library
    document exactly as Calibration IQ research reviews it. ``learn`` is the
    trusted semantic-cache promotion hook. ``evaluator`` and ``client_provider``
    default to the shared evaluator and current tool-call model.
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
                if isinstance(prepared, dict):
                    candidate["text"] = prepared.get("text") or candidate["text"]
                    if prepared.get("screenshot"):
                        screenshot = prepared["screenshot"]
        return candidate, screenshot

    async def delegate_research(args: dict[str, Any]) -> dict[str, Any]:
        objective = _clean(args.get("objective") or args.get("question"), 500)
        if not objective:
            return {
                "status": "error",
                "outcome": contract.UNSATISFIED,
                "message": "objective is required",
                "reading_guide": READING_GUIDE,
            }
        vehicle = _vehicle(args)
        deliverable = _clean(args.get("deliverable") or "answer", 40) or "answer"
        if deliverable not in {"answer", "procedure"}:
            deliverable = "answer"
        depth = _clean(args.get("depth") or "standard", 40) or "standard"
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
                    # Verified cache reuse is deliberately narrow. Structured
                    # system/component filters carry the identity, and the FTS
                    # query repeats only those same bounded terms. The repository
                    # ANDs FTS tokens, so feeding the full conversational question
                    # here would turn harmless wording differences into cache misses.
                    query: dict[str, Any] = {"limit": MAX_FINDINGS}
                    if all(field in vehicle for field in ("year", "make", "model")):
                        query.update(
                            {
                                "year": vehicle["year"],
                                "manufacturer": vehicle["make"],
                                "model": vehicle["model"],
                            }
                        )
                    relevance_parts = [part for part in (system, component) if part]
                    if relevance_parts:
                        query["query"] = " ".join(relevance_parts)
                    elif not all(field in vehicle for field in ("year", "make", "model")):
                        # Without a structured vehicle/system scope, free-text is
                        # still better than an unbounded latest-record read.
                        query["query"] = objective
                    if system:
                        query["system"] = system
                    if component:
                        query["component"] = component
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
            except Exception as exc:  # noqa: BLE001 - one step failing is a ledger fact
                log.warning("delegate_research source %s failed", source, exc_info=True)
                entry["retrieval_status"] = "error"
                entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
            entry["retrieved"] = len(source_findings)

            # Retrieval/cache lookup is not an answer: the evaluator decides,
            # one candidate at a time in authority order, until one satisfies.
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
                        provider={
                            "adas_si": "ADAS SI",
                            "automotive_knowledge": "verified ADAS SI semantic cache",
                            "web": "public web",
                        }.get(source, source),
                        deliverable=deliverable,
                        screenshot=screenshot,
                    )
                except Exception as exc:  # noqa: BLE001 - evaluator failure is never acceptance
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
                if finding["accepted"]:
                    findings.append(finding)
                    if evaluation.get("evidence_id"):
                        evidence_ids.append(str(evaluation["evidence_id"]))
                    if outcome == contract.SATISFIED and satisfied_evidence is None:
                        satisfied_evidence = {
                            "finding": finding,
                            "evaluation": evaluation,
                            "candidate": candidate,
                        }
                else:
                    for reason in (evaluation.get("reasons") or [])[:3]:
                        if isinstance(reason, str) and reason not in unresolved:
                            unresolved.append(reason[:240])
                if outcome == contract.SATISFIED and not exhaustive:
                    entry["outcome"] = contract.SATISFIED
                    break
            else:
                if contract.SATISFIED in source_outcomes:
                    entry["outcome"] = contract.SATISFIED
                elif contract.PARTIAL in source_outcomes:
                    entry["outcome"] = contract.PARTIAL
                else:
                    entry["outcome"] = contract.UNSATISFIED
            outcomes.append(entry["outcome"])
            ledger.append(entry)
            if entry["outcome"] == contract.SATISFIED and not exhaustive:
                break

        _bound_excerpts(findings)
        overall = (
            contract.SATISFIED
            if contract.SATISFIED in outcomes
            else contract.PARTIAL
            if contract.PARTIAL in outcomes
            else contract.UNSATISFIED
        )
        learned = None
        if (
            overall == contract.SATISFIED
            and satisfied_evidence is not None
            and learn is not None
            and satisfied_evidence["finding"].get("source") == "adas_si"
        ):
            # Both SATISFIED fact answers and SATISFIED procedures use the same
            # ADAS-SI-backed cache gate. The gate itself applies the distinct
            # evidence requirements for each deliverable.
            try:
                learned = await learn(
                    objective=objective,
                    vehicle=vehicle,
                    system=system,
                    component=component,
                    finding=satisfied_evidence["finding"],
                    evaluation=satisfied_evidence["evaluation"],
                    candidate=satisfied_evidence["candidate"],
                )
            except Exception as exc:  # noqa: BLE001 - cache failure never changes research truth
                log.warning("semantic-cache promotion failed", exc_info=True)
                learned = {"promoted": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}

        accepted = [f for f in findings if f.get("accepted")]
        return {
            "status": "success" if overall != contract.UNSATISFIED else "no_result",
            "outcome": overall,
            "verified": overall == contract.SATISFIED,
            "unresolved": unresolved[:8],
            "sources_checked": [e["source"] for e in ledger if e.get("attempted")],
            "source_ledger": ledger,
            "finding_count": len(findings),
            "accepted_count": len(accepted),
            "evidence_ids": list(dict.fromkeys(evidence_ids))[:12],
            "learned": learned,
            "findings": findings,
            "reading_guide": READING_GUIDE,
            "message": {
                contract.SATISFIED: "Accepted evidence answers the objective for this vehicle and system.",
                contract.PARTIAL: "Accepted evidence answers part of the objective; see unresolved.",
                contract.UNSATISFIED: "No retrieved source answered the objective for this vehicle and system.",
            }[overall],
        }

    return delegate_research
