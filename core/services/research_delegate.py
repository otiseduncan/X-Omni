"""Delegated research worker behind the ``delegate_research`` capability.

The conversational model hands over one structured objective (plus optional
vehicle, scope, and source preferences).  This worker runs the existing
sources in order and returns provenance-bearing findings:

    adas_si              local authoritative OEM/service-information library
    automotive_knowledge durable, provenance-backed structured knowledge
    alldata              licensed ALLDATA through ScrapeX's Navigator, driven
                         by the active X model inside this one tool call
    web                  public OEM web search with bounded page reads

Structural only: no user prose is parsed here.  Source order comes from the
model's ``sources``/``exclude_sources`` fields or the default; the worker
stops at the first verified finding unless ``exhaustive``.  It never writes
to Calibration IQ.  ``preserve=true`` is the only side effect and captures
verified external evidence into ADAS SI through the existing capture paths.
"""

from __future__ import annotations

import inspect
import logging
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


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


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
        finding = {
            "source": "adas_si",
            "title": _clean(item.get("title") or item.get("source"), 200),
            "relative_path": item.get("relative_path"),
            "page": item.get("page"),
            "excerpt": _clean(item.get("excerpt"), EXCERPT_CHARS),
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
                    result = await _maybe_await(
                        search(
                            client=client,
                            settings=settings,
                            provider="alldata",
                            target={
                                "year": vehicle["year"],
                                "make": vehicle["make"],
                                "model": " ".join(
                                    part for part in (vehicle.get("model"), vehicle.get("trim")) if part
                                ),
                            },
                            topic=objective,
                            capture=preserve,
                        )
                    )
                    result = result if isinstance(result, dict) else {}
                    entry["status"] = result.get("status") or (
                        "verified" if result.get("verified") else "unverified"
                    )
                    entry["verified"] = bool(result.get("verified"))
                    entry["reason"] = result.get("reason") or result.get("verification_reason")
                    entry["task_id"] = result.get("task_id")
                    if result.get("requires_human") or result.get("status") == "authentication_required":
                        authentication_required = True
                        requires_human = True
                    if result.get("task_id"):
                        evidence_ids.append(f"navigator-task:{result['task_id']}")
                    if entry["verified"]:
                        finding = {
                            "source": "alldata",
                            "title": _clean(result.get("topic") or objective, 200),
                            "url": result.get("source_url"),
                            "excerpt": _clean(result.get("extracted_text"), ALLDATA_EXTRACT_CHARS),
                            "task_id": result.get("task_id"),
                            "captured": bool(result.get("captured")),
                            "provenance": result.get("provenance"),
                            "verification": result.get("verification"),
                        }
                        if result.get("captured") and isinstance(result.get("capture"), dict):
                            finding["capture"] = result["capture"]
                        source_findings = [
                            {key: value for key, value in finding.items() if value not in (None, "", {})}
                        ]
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
