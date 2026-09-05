"""Verified composite post-collision research for X Omni.

For a post-collision technical research request, X must prove which information
sources were actually queried. Licensed ALLDATA work uses ScrapeX's isolated
Navigator profile with the active X model. X owns interpretation and navigation;
ScrapeX owns browser-session execution and objective verification. Persistence
remains an explicit structured choice.
"""


from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Optional
from urllib.parse import urlparse

from . import research_capture
from . import research_navigator_agent
from . import research_operator as ro

log = logging.getLogger("xomni.research_workflow")

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False

_STOPWORDS = {
    "about", "after", "alldata", "and", "any", "check", "collision", "documentation",
    "evidence", "find", "first", "for", "from", "into", "look", "manufacturer",
    "missing", "oem", "official", "our", "please", "preserve", "research", "show",
    "source", "sources", "supporting", "that", "the", "then", "this", "use", "what",
    "whether", "with",
}


def focused_query(message: object) -> str:
    text = " ".join(str(message or "").split()).strip()
    if not text:
        return ""
    first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    first = re.sub(r"^\s*(?:please\s+)?(?:research|investigate|verify|find\s+out)\s+", "", first, flags=re.I)
    return (first or text)[:500].strip(" .")


def requested_make(query: str, adas_mod: Any) -> Optional[str]:
    folded = query.casefold()
    for alias, canonical in (getattr(adas_mod, "MAKE_ALIASES", {}) or {}).items():
        if re.search(rf"(?<![a-z0-9]){re.escape(str(alias).casefold())}(?![a-z0-9])", folded):
            return str(canonical)
    for make in getattr(adas_mod, "KNOWN_MAKES", ()) or ():
        value = str(make)
        if re.search(rf"(?<![a-z0-9]){re.escape(value.casefold())}(?![a-z0-9])", folded):
            return value
    return None


def _source_score(source: dict[str, Any], make: Optional[str]) -> int:
    url = str(source.get("url") or "")
    host = (urlparse(url).hostname or "").casefold()
    text = f"{source.get('title') or ''} {source.get('snippet') or ''}".casefold()
    score = 0
    if make:
        compact_make = re.sub(r"[^a-z0-9]", "", make.casefold())
        if compact_make and compact_make in re.sub(r"[^a-z0-9]", "", host):
            score += 12
        if make.casefold() in text:
            score += 6
    for term, weight in (("collision", 6), ("position statement", 8), ("recycled", 6),
                         ("used part", 5), ("blind spot", 5), ("repair", 3),
                         ("service bulletin", 4), ("technical", 2)):
        if term in text or term in host:
            score += weight
    return score


async def search_public_oem(
    query: str,
    make: Optional[str],
    *,
    source_depth: str = "standard",
) -> dict[str, Any]:
    if source_depth != "standard":
        # source_depth is already a structured caller decision. Delegating to
        # the bounded deep reader restores complete PDF/one-hop OEM evidence
        # without classifying conversational language.
        from . import research_policy_depth

        return await research_policy_depth.deep_search_public_oem(
            query,
            make,
            source_depth=source_depth,
        )
    search = await ro.public_search({"query": query, "manufacturer": make or ""})
    sources = [s for s in (search.get("sources") or []) if isinstance(s, dict)]
    sources.sort(key=lambda s: _source_score(s, make), reverse=True)
    reads: list[dict[str, Any]] = []
    for source in sources[:3]:
        url = str(source.get("url") or "").strip()
        if not url:
            continue
        try:
            read = await ro.public_read({"url": url})
            reads.append({
                "url": read.get("url") or url, "title": read.get("title") or source.get("title"),
                "content_type": read.get("content_type"),
                "page_text": str(read.get("page_text") or read.get("message") or "")[:8000],
                "source_result": source,
            })
        except Exception as exc:  # noqa: BLE001
            reads.append({"url": url, "title": source.get("title"), "read_error": f"{type(exc).__name__}: {exc}", "source_result": source})
    verified = bool(search.get("external_network") is True and search.get("source_bounded") is True)
    return {
        "searched": verified, "verified": verified, "query": search.get("query"),
        "providers": search.get("providers"), "sources": sources[:8], "read_results": reads,
        "result_count": len(sources),
    }


async def _search_alldata_best_available(
    browser: Any,
    query: str,
    *,
    capture: bool = False,
) -> dict[str, Any]:
    """Use the isolated ScrapeX Navigator for licensed ALLDATA research.

    browser stays in the signature only for compatibility with direct callers
    of this legacy composite helper. It is never used for ALLDATA navigation
    or persistence.
    """
    del browser
    from ..config import Settings
    from . import research_alldata_navigation as nav

    vehicle = nav.vehicle_from_query(query)
    target = {
        "year": vehicle.get("year"),
        "make": vehicle.get("make"),
        "model": vehicle.get("model_trim"),
    }
    if not (target["year"] and target["make"] and target["model"]):
        return {
            "attempted": False,
            "searched": False,
            "verified": False,
            "captured": False,
            "reason": (
                "Exact year, make, and model are required for licensed "
                "ALLDATA Navigator research."
            ),
        }

    client = research_navigator_agent.current_model_client()
    if client is None:
        return {
            "attempted": False,
            "searched": False,
            "verified": False,
            "captured": False,
            "reason": (
                "The active X model is unavailable to the ALLDATA Navigator; "
                "no substitute navigation path was run."
            ),
        }

    topic = nav.topic_from_query(query, vehicle)
    return await research_navigator_agent.run_navigator_search(
        client=client,
        settings=Settings.load(),
        provider="alldata",
        target=target,
        topic=topic,
        capture=capture,
    )

async def full_research(args: dict[str, Any], *, adas: Any, browser: Any) -> dict[str, Any]:
    from . import adas_si as adas_mod

    raw = str(args.get("query") or "").strip()
    query = focused_query(raw)
    if not query:
        raise ValueError("query is required")
    make = str(args.get("manufacturer") or "").strip() or requested_make(query, adas_mod)
    from . import research_alldata_navigation as nav

    storage_vehicle = nav.vehicle_from_query(query)
    # Persistence is an explicit structured decision made by the model. Never
    # infer it from words embedded in the research query.
    preserve = args.get("preserve") is True
    source_depth = str(args.get("source_depth") or "standard").strip().casefold()
    if source_depth not in {"standard", "calibration_requirements", "repair_policy"}:
        raise ValueError("source_depth must be standard, calibration_requirements, or repair_policy")

    local_args = {"query": query}
    if source_depth == "calibration_requirements":
        local_args["search_mode"] = "calibration_requirements"
    local = adas.search(local_args)
    compact_local = _compact_adas(local, make or None)
    local_ledger = {
        "source": "ADAS SI", "attempted": True, "searched": True,
        "verified": local.get("status") in {"success", "partial_success", "no_result"},
        "result_count": compact_local["result_count"], "requested_make": make or None,
    }

    try:
        alldata = await _search_alldata_best_available(
            browser, query, capture=preserve
        )
    except Exception as exc:  # noqa: BLE001
        alldata = {"attempted": True, "searched": False, "verified": False, "reason": f"ALLDATA provider error: {type(exc).__name__}: {exc}"}
    alldata_ledger = {
        "source": "ALLDATA", "attempted": bool(alldata.get("attempted", True)),
        "searched": bool(alldata.get("searched")), "verified": bool(alldata.get("verified")),
        "captured": bool(alldata.get("captured")),
        "query_submitted": bool(
            alldata.get("query_submitted")
            or (alldata.get("verification") or {}).get("query_submitted")
        ),
        "url": alldata.get("source_url") or alldata.get("url"),
        # An early-exit failure (couldn't authenticate, couldn't find the
        # vehicle box) sets "reason"; a run that got as far as a result but
        # failed the evaluate_alldata_claim() checks only sets
        # "verification_reason" -- surface whichever is present so the UI
        # never silently drops the one explanation that matters most: why a
        # search that found *something* still wasn't trusted.
        "reason": alldata.get("reason") or alldata.get("verification_reason"),
        "human_action_required": bool(alldata.get("human_action_required")),
    }

    try:
        public = await search_public_oem(
            query,
            make or None,
            source_depth=source_depth,
        )
    except Exception as exc:  # noqa: BLE001
        public = {"searched": False, "verified": False, "sources": [], "read_results": [], "result_count": 0, "error": f"{type(exc).__name__}: {exc}"}
    public_ledger = {
        "source": "Public OEM web", "attempted": True, "searched": bool(public.get("searched")),
        "verified": bool(public.get("verified")), "result_count": int(public.get("result_count") or 0),
        "reason": public.get("error"),
    }

    captures: list[dict[str, Any]] = []
    if preserve:
        if alldata.get("captured") is True and isinstance(alldata.get("capture"), dict):
            captures.append({"source": "ALLDATA", **alldata["capture"]})
        elif alldata.get("verified") is True:
            captures.append(
                {
                    "source": "ALLDATA",
                    "status": "not_captured",
                    "verified": False,
                    "message": (
                        "ALLDATA research verified, but ScrapeX did not verify the "
                        "requested persistence operation."
                    ),
                }
            )
        for source in (public.get("sources") or [])[:4]:
            if not isinstance(source, dict) or _source_score(source, make or None) < 8:
                continue
            url = str(source.get("url") or "").strip()
            if not url:
                continue
            try:
                saved = await research_capture.public_capture(
                    {
                        "url": url,
                        "manufacturer": make or "OEM",
                        "vehicle": storage_vehicle.get("label") or "",
                        "vehicle_year": storage_vehicle.get("year"),
                        "vehicle_make": storage_vehicle.get("make"),
                        "vehicle_model": storage_vehicle.get("model_trim"),
                        "topic": source.get("title") or query[:120],
                    },
                    adas,
                )
                captures.append({"source": "Public OEM web", **saved})
                break
            except Exception as exc:  # noqa: BLE001
                captures.append({"source": "Public OEM web", "url": url, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})

    ledger = [local_ledger, alldata_ledger, public_ledger]
    external_verified = bool(alldata_ledger["verified"] or public_ledger["verified"])
    complete = all(bool(item.get("verified")) for item in ledger)
    status = "success" if complete else ("partial_success" if external_verified else "failed")
    return {
        "status": status, "action": "full_research", "query": query,
        "requested_manufacturer": make or None, "preserve_requested": preserve,
        "workflow_complete": complete, "external_search_verified": external_verified,
        "source_ledger": ledger, "adas_si": compact_local, "alldata": alldata,
        "public_oem": public, "captures": captures,
        "authority_note": "OEM/manufacturer, insurer, and legal/regulatory requirements are separate authorities.",
    }


def fixed_summary(result: dict[str, Any]) -> str:
    ledger = {str(i.get("source")): i for i in (result.get("source_ledger") or []) if isinstance(i, dict)}
    local = ledger.get("ADAS SI", {})
    ad = ledger.get("ALLDATA", {})
    public = ledger.get("Public OEM web", {})
    ad_state = "searched" if ad.get("verified") else "not verified as searched"
    ad_reason = f" — {ad.get('reason')}" if ad.get("reason") else ""
    web_state = "searched" if public.get("verified") else "not verified as searched"
    return (
        f"Search verification — ADAS SI: searched ({local.get('result_count', 0)} relevant passages); "
        f"ALLDATA: {ad_state}{ad_reason}; Public OEM web: {web_state} "
        f"({public.get('result_count', 0)} source results)."
    )


async def synthesize(orchestrator: Any, question: str, result: dict[str, Any]) -> str:
    evidence = json.dumps(result, ensure_ascii=False, default=str)
    if len(evidence) > 90_000:
        evidence = evidence[:90_000] + "\n[TRUNCATED FOR SYNTHESIS]"
    messages = [
        {"role": "system", "content": (
            "You are Xoduz summarizing a verified post-collision research workflow. Use ONLY the evidence JSON. "
            "Never say ALLDATA or the public OEM web was searched unless that source_ledger row has verified=true. "
            "Separate local ADAS SI, licensed ALLDATA, and public OEM/manufacturer evidence. If evidence does not "
            "resolve the question, say so. Do not invent citations, policies, page numbers, or source contents."
        )},
        {"role": "user", "content": f"Question:\n{question}\n\nEvidence JSON:\n{evidence}"},
    ]
    try:
        answer = await orchestrator.client.complete(messages, max_tokens=900, temperature=0.1)
    except Exception:  # noqa: BLE001
        answer = ""
    proof = fixed_summary(result)
    return f"{answer}\n\n{proof}".strip() if answer else proof


def _persist_research_task(conversation_id: Any, result: dict[str, Any], history_len: int) -> None:
    """Best-effort: record this research turn as the conversation's active task.

    This is what lets a later, short follow-up ("show me the exact procedure")
    be resolved generically by research_task_continuity instead of needing its
    own enumerated regex -- the vehicle/subject/status it needs is already
    sitting here from the request that just ran.
    """
    try:
        from . import research_task
        from . import research_alldata_navigation as nav
        from ..config import Settings

        alldata = result.get("alldata") if isinstance(result.get("alldata"), dict) else {}
        vehicle = alldata.get("vehicle") if isinstance(alldata.get("vehicle"), dict) else {}
        query = str(result.get("query") or "")
        if not vehicle.get("label") and query:
            vehicle = nav.vehicle_from_query(query)
        topic = str(alldata.get("topic") or query)[:200]

        ledger = {
            str(item.get("source")): item
            for item in (result.get("source_ledger") or [])
            if isinstance(item, dict)
        }
        local_row = ledger.get("ADAS SI", {})
        alldata_row = ledger.get("ALLDATA", {})
        public_row = ledger.get("Public OEM web", {})

        def status_of(row: dict[str, Any]) -> str:
            if row.get("verified"):
                return "verified"
            if row.get("searched"):
                return "searched_unverified"
            return "not_started"

        task = research_task.ResearchTask(
            conversation_id=str(conversation_id),
            vehicle_year=vehicle.get("year"),
            vehicle_make=vehicle.get("make"),
            vehicle_model_trim=vehicle.get("model_trim"),
            vehicle_label=vehicle.get("label"),
            subject=topic,
            local_status="found" if int(local_row.get("result_count") or 0) > 0 else "missing",
            alldata_status=status_of(alldata_row),
            alldata_evidence_url=alldata_row.get("url"),
            oem_web_status=status_of(public_row),
            oem_web_evidence_url=(public_row.get("url") if isinstance(public_row.get("url"), str) else None),
            acquisition_status="captured" if result.get("captures") else "pending",
            turn_count_at_update=history_len + 1,
        )
        research_task.get_store(Settings.load().root).save(task)
    except Exception:  # noqa: BLE001 - continuity is best-effort, never blocks the answer
        pass


def install() -> None:
    """Retain import compatibility without exposing the fixed source sequence.

    The base collision_research actions remain model-selectable. The legacy
    full_research helper stays directly testable, but production registration
    must not collapse source selection and escalation into a fixed composite
    action.
    """

    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        _INSTALLED = True
