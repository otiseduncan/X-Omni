"""Delegated research worker behind the ``delegate_research`` capability.

The conversational model hands over one structured objective (plus optional
vehicle, scope, and source preferences).  This worker runs the existing
sources in order and returns provenance-bearing findings:

    adas_si              local authoritative OEM/service-information library
    automotive_knowledge durable, provenance-backed structured knowledge
    alldata              licensed ALLDATA through ScrapeX's Navigator, driven
                         by the active X model inside this one tool call;
                         each candidate page is judged by an independent
                         semantic review before it counts as a finding
    web                  public OEM web search with bounded page reads

Structural only: no user prose is parsed here.  Source order comes from the
model's ``sources``/``exclude_sources`` fields or the default; the worker
stops at the first verified finding unless ``exhaustive``.  It never writes
to Calibration IQ.  ``preserve=true`` is the only side effect and captures
verified external evidence into ADAS SI through the existing capture paths.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from typing import Any, Callable, Optional

log = logging.getLogger("xomni.research_delegate")

DEFAULT_SOURCE_ORDER: tuple[str, ...] = (
    "adas_si",
    "automotive_knowledge",
    "alldata",
    "web",
)
MAX_FINDINGS = 8
EXCERPT_CHARS = 1_500
ALLDATA_EXTRACT_CHARS = 4_000
# ADAS SI pages keep their line structure and get room for a whole table page;
# every excerpt together stays well inside the model's tool-result budget so no
# finding is dropped to make room for another's text.
ADAS_EXCERPT_CHARS = 3_200
TOTAL_EXCERPT_CHARS = 7_000
MIN_EXCERPT_CHARS = 400
# Returned with every result. The findings are evidence for X to interpret, not
# text to relay: live, X pasted a flattened OCR table into its answer and then
# answered from general knowledge instead of from what the table said.
READING_GUIDE = (
    "Findings are source evidence for you to interpret, listed in source-authority "
    "order. Excerpts keep the source's line structure; in OCR'd tables ' | ' separates "
    "columns and each row lines up with the header row above it. Work out what the "
    "evidence means for Otis's question and answer in your own words; quote or show "
    "raw excerpts only when he asks to see the source."
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
    # A VIN identifies one vehicle including trim and engine, which a
    # year/make/model cascade cannot. Kept apart from the label so it never
    # becomes part of a search phrase.
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
    excluded = args.get("exclude_sources")
    excluded = set(excluded) if isinstance(excluded, list) else set()
    return [source for source in order if source not in excluded]


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
        }
        if item.get("evidence_id"):
            finding["evidence_id"] = item["evidence_id"]
        findings.append({key: value for key, value in finding.items() if value not in (None, "")})
    return findings


def _knowledge_findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    findings = []
    for record in (result.get("records") or [])[:MAX_FINDINGS]:
        if not isinstance(record, dict):
            continue
        provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
        sources = record.get("sources") if isinstance(record.get("sources"), list) else []
        finding = {
            "source": "automotive_knowledge",
            "record_id": record.get("id") or record.get("record_id"),
            "title": _clean(record.get("title") or record.get("claim") or record.get("summary"), 200),
            "lifecycle": record.get("lifecycle"),
            "excerpt": _clean(
                record.get("procedure")
                or record.get("summary")
                or record.get("claim")
                or record.get("statement"),
                EXCERPT_CHARS,
            ),
            "provenance": provenance or (sources[:2] if sources else None),
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
        finding = {
            "source": "web",
            "title": _clean(read.get("title") or source.get("title"), 200),
            "url": url,
            "excerpt": _clean(read.get("page_text") or source.get("snippet"), EXCERPT_CHARS),
            "provider": source.get("provider"),
        }
        findings.append({key: value for key, value in finding.items() if value not in (None, "")})
    return findings


# Result fields the model reads above the findings. The request echo (objective,
# vehicle, depth, source order) is left out: live, X searched with an invented
# vehicle and then read the chart against that echo instead of the question.
_MODEL_HEADER_KEYS = (
    "status",
    "verified",
    "sources_checked",
    "source_ledger",
    "finding_count",
    "evidence_ids",
    "authentication_required",
    "requires_human",
    "message",
    "reading_guide",
)
_FINDING_DETAIL_KEYS = (
    "relative_path",
    "record_id",
    "lifecycle",
    "provenance",
    "semantic_review",
    "documents",
    "dependencies",
    "complete",
    "captured",
    "task_id",
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
    navigator_search: Optional[Callable[..., Any]] = None,
    public_search: Optional[Callable[..., Any]] = None,
) -> Callable[[dict[str, Any]], Any]:
    """Build the handler with its sources injected (tests supply fakes)."""

    async def delegate_research(args: dict[str, Any]) -> dict[str, Any]:
        objective = _clean(args.get("objective"), 600)
        if len(objective) < 3:
            raise ValueError("objective is required")
        vehicle = _vehicle(args)
        depth = str(args.get("depth") or "standard").strip().casefold()
        if depth not in {"standard", "calibration_requirements", "repair_policy"}:
            depth = "standard"
        exhaustive = args.get("exhaustive") is True
        preserve = args.get("preserve") is True
        order = source_order(args)

        ledger: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        evidence_ids: list[str] = []
        authentication_required = False
        requires_human = False

        for source in order:
            entry: dict[str, Any] = {"source": source, "attempted": True, "verified": False}
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
                    for field in ("system", "component"):
                        value = _clean(args.get(field), 200)
                        if value:
                            query[field] = value
                    if depth == "calibration_requirements":
                        query["search_mode"] = "calibration_requirements"
                    result = await _maybe_await(adas_search(query))
                    result = result if isinstance(result, dict) else {}
                    entry["status"] = result.get("status")
                    source_findings = _adas_findings(result)
                    entry["result_count"] = len(source_findings)
                    entry["verified"] = bool(
                        result.get("status") in {"success", "partial_success"} and source_findings
                    )
                    if result.get("evidence_id"):
                        evidence_ids.append(str(result["evidence_id"]))
                elif source == "automotive_knowledge":
                    query = {"query": objective, "limit": MAX_FINDINGS}
                    if all(field in vehicle for field in ("year", "make", "model")):
                        query.update(
                            {
                                "year": vehicle["year"],
                                "manufacturer": vehicle["make"],
                                "model": vehicle["model"],
                            }
                        )
                        if "trim" in vehicle:
                            query["trim"] = vehicle["trim"]
                    for field in ("system", "component"):
                        value = _clean(args.get(field), 200)
                        if value:
                            query[field] = value
                    result = await _maybe_await(knowledge_search(query))
                    result = result if isinstance(result, dict) else {}
                    entry["status"] = result.get("status")
                    source_findings = _knowledge_findings(result)
                    entry["result_count"] = len(source_findings)
                    entry["verified"] = bool(
                        result.get("status") in {"success", "partial_success"} and source_findings
                    )
                    if result.get("evidence_id"):
                        evidence_ids.append(str(result["evidence_id"]))
                elif source == "alldata":
                    if not all(field in vehicle for field in ("year", "make", "model")):
                        entry.update(
                            {
                                "attempted": False,
                                "status": "not_attempted",
                                "reason": "ALLDATA needs an exact year, make, and model.",
                            }
                        )
                        ledger.append(entry)
                        continue
                    from . import research_navigator_agent as nav_agent

                    search = navigator_search or nav_agent.run_navigator_search
                    client = nav_agent.current_model_client()
                    if client is None and navigator_search is None:
                        entry.update(
                            {
                                "attempted": False,
                                "status": "not_attempted",
                                "reason": "The active X model is unavailable to the ALLDATA Navigator.",
                            }
                        )
                        ledger.append(entry)
                        continue
                    target = {
                        "year": vehicle["year"],
                        "make": vehicle["make"],
                        "model": " ".join(
                            part for part in (vehicle.get("model"), vehicle.get("trim")) if part
                        ),
                    }
                    if vehicle.get("vin"):
                        target["vin"] = vehicle["vin"]
                    research_objective = {
                        "objective": objective,
                        "system": _clean(args.get("system"), 200) or None,
                        "component": _clean(args.get("component"), 200) or None,
                    }
                    result = await _maybe_await(
                        search(
                            client=client,
                            settings=settings,
                            provider="alldata",
                            target=target,
                            topic=objective,
                            capture=preserve,
                            objective=research_objective,
                        )
                    )
                    result = result if isinstance(result, dict) else {}
                    entry["status"] = result.get("status") or (
                        "verified" if result.get("verified") else "unverified"
                    )
                    entry["verified"] = bool(result.get("verified"))
                    entry["complete"] = result.get("complete")
                    entry["reason"] = result.get("reason") or result.get("verification_reason")
                    entry["task_id"] = result.get("task_id")
                    if result.get("incomplete_reasons"):
                        entry["incomplete_reasons"] = list(result["incomplete_reasons"])[:6]
                    if result.get("requires_human") or result.get("status") == "authentication_required":
                        authentication_required = True
                        requires_human = True
                    for task_id in result.get("task_ids") or ([result["task_id"]] if result.get("task_id") else []):
                        evidence_ids.append(f"navigator-task:{task_id}")
                    if entry["verified"]:
                        review = result.get("semantic_review") if isinstance(result.get("semantic_review"), dict) else {}
                        finding = {
                            "source": "alldata",
                            "title": _clean(result.get("evidence_title") or result.get("topic") or objective, 200),
                            "url": result.get("source_url"),
                            "excerpt": _clean(result.get("extracted_text"), ALLDATA_EXTRACT_CHARS),
                            "task_id": result.get("task_id"),
                            "captured": bool(result.get("captured")),
                            "provenance": result.get("provenance"),
                            "verification": result.get("verification"),
                            "semantic_review": {
                                key: review.get(key)
                                for key in ("classification", "procedure_type", "decision", "confidence", "evidence_summary")
                                if review.get(key) is not None
                            } or None,
                            "documents": [
                                {
                                    key: document.get(key)
                                    for key in ("role", "title", "url", "accepted", "classification", "decision", "captured", "artifact")
                                }
                                for document in (result.get("documents") or [])
                                if isinstance(document, dict)
                            ][:8] or None,
                            "dependencies": [
                                {key: dependency.get(key) for key in ("title", "reason", "status")}
                                for dependency in (result.get("dependencies") or [])
                                if isinstance(dependency, dict)
                            ][:8] or None,
                            "complete": result.get("complete"),
                        }
                        if result.get("captured") and isinstance(result.get("capture"), dict):
                            finding["capture"] = result["capture"]
                        source_findings = [
                            {key: value for key, value in finding.items() if value not in (None, "", {})}
                        ]
                    if isinstance(result.get("research_receipt"), dict):
                        receipt = result["research_receipt"]
                        entry["receipt"] = {
                            "task_ids": receipt.get("task_ids"),
                            "final_status": receipt.get("final_status"),
                            "critic_decisions": receipt.get("critic_decisions"),
                            "artifacts": receipt.get("artifacts"),
                            "stale_action_rejections": receipt.get("stale_action_rejections"),
                            "metrics": receipt.get("metrics"),
                        }
                    entry["result_count"] = len(source_findings)
                elif source == "web":
                    from . import research_workflow

                    search_web = public_search or research_workflow.search_public_oem
                    query_text = " ".join(part for part in (vehicle.get("label"), objective) if part)
                    result = await _maybe_await(
                        search_web(query_text, vehicle.get("make"), source_depth=depth)
                    )
                    result = result if isinstance(result, dict) else {}
                    source_findings = _web_findings(result)
                    entry["status"] = "verified" if result.get("verified") else "unverified"
                    entry["result_count"] = int(result.get("result_count") or len(source_findings))
                    entry["verified"] = bool(result.get("verified") and source_findings)
                    entry["providers"] = result.get("providers")
                else:  # pragma: no cover - schema enum prevents this
                    entry.update({"attempted": False, "status": "unknown_source"})
            except Exception as exc:  # noqa: BLE001 - one source failing is a ledger fact
                log.warning("delegate_research source %s failed", source, exc_info=True)
                entry["status"] = "error"
                entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
            ledger.append(entry)
            findings.extend(source_findings[: max(0, MAX_FINDINGS - len(findings))])
            for finding in source_findings:
                for key in ("evidence_id", "record_id"):
                    if finding.get(key):
                        evidence_ids.append(str(finding[key]))
            if entry.get("verified") and not exhaustive:
                break

        _bound_excerpts(findings)
        verified = any(item.get("verified") for item in ledger)
        checked = [item["source"] for item in ledger if item.get("attempted")]
        if verified:
            status = "success" if all(
                item.get("verified") for item in ledger if item.get("attempted")
            ) else "partial_success"
        elif authentication_required:
            status = "blocked"
        else:
            status = "no_result"
        return {
            "status": status,
            "verified": verified,
            "objective": objective,
            "vehicle": vehicle or None,
            "depth": depth,
            "source_order": order,
            "sources_checked": checked,
            "source_ledger": ledger,
            "findings": findings,
            "finding_count": len(findings),
            "reading_guide": READING_GUIDE,
            "evidence_ids": sorted(set(evidence_ids)),
            "authentication_required": authentication_required,
            "requires_human": requires_human,
            "mutated_calibration_iq": False,
            "message": (
                "Verified findings returned with provenance."
                if verified
                else (
                    "ALLDATA requires interactive sign-in; no verified finding yet."
                    if authentication_required
                    else "No verified finding in the sources checked; this is a miss in those sources only."
                )
            ),
        }

    return delegate_research
