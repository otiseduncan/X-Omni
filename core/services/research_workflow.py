"""Verified composite post-collision research for X Omni.

For a post-collision technical research request, X must prove which information
sources were actually queried. This module runs one sequence: ADAS SI ->
authenticated ALLDATA -> public OEM/manufacturer web. It returns a source
ledger so model prose cannot substitute for tool execution.

The ALLDATA step is model-driven when a live model client is available (see
_ACTIVE_RESEARCH_CLIENT below and research_alldata_agent.run_agent_search),
falling back to a fixed vehicle-first navigation sequence otherwise. Either
way, "verified" is decided once, centrally, by research_verification -- never
by which navigation path happened to run.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import threading
from typing import Any, Optional
from urllib.parse import urlparse

from . import research_capture
from . import research_operator as ro
from . import research_verification

log = logging.getLogger("xomni.research_workflow")

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False

# Carries the live model client (already attached to the app's one running
# ModelRouter/worker) from the orchestrator turn that triggered this research
# request down to full_research(), without ever putting a live object into a
# tool-args dict -- those flow through Registry.invoke's audit/log_args path,
# which is not a safe place for anything but plain, loggable data. Only the
# Orchestrator._run wrapper installed below sets this; a model explicitly
# invoking collision_research(action="full_research") on its own leaves it
# unset, and full_research() falls back to the deterministic search in that
# case rather than trying to construct a second, unmanaged ModelRouter.
_ACTIVE_RESEARCH_CLIENT: "contextvars.ContextVar[Optional[Any]]" = contextvars.ContextVar(
    "_xomni_active_research_client", default=None
)

_INTENT_RE = re.compile(r"\b(?:research|find|verify|check|look\s*up|investigate)\b", re.I)
_DOMAIN_RE = re.compile(
    r"\b(?:all\s*data|alldata|oem|manufacturer|collision|position\s+statement|"
    r"recycled|used\s+(?:module|sensor|part)|insurance|blind\s+spot|adas\s+si|"
    r"service\s+information|repair\s+procedure|technical\s+bulletin)\b",
    re.I,
)
_RO_ONLY_RE = re.compile(r"^\s*(?:please\s+)?research\s+(?:this|that|the)\s+(?:repair\s+order|ro)\b", re.I)
_PRESERVE_RE = re.compile(
    r"\b(?:preserve|save|capture|store|archive|add|import|keep)\b.{0,100}"
    r"\b(?:adas|database|library|documentation|evidence|source|pdf)\b"
    r"|\bmissing\b.{0,80}\badas\s+si\b",
    re.I | re.S,
)
_STOPWORDS = {
    "about", "after", "alldata", "and", "any", "check", "collision", "documentation",
    "evidence", "find", "first", "for", "from", "into", "look", "manufacturer",
    "missing", "oem", "official", "our", "please", "preserve", "research", "show",
    "source", "sources", "supporting", "that", "the", "then", "this", "use", "what",
    "whether", "with",
}


def full_research_request(message: object) -> bool:
    text = str(message or "").strip()
    return bool(text and not _RO_ONLY_RE.search(text) and _INTENT_RE.search(text) and _DOMAIN_RE.search(text))


def preserve_requested(message: object) -> bool:
    return bool(_PRESERVE_RE.search(str(message or "")))


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


def _tokens(query: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9-]+", query.casefold()) if len(t) >= 3 and t not in _STOPWORDS][:24]


def _compact_adas(result: dict[str, Any], make: Optional[str]) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    for item in (result.get("results") or [])[:6]:
        if not isinstance(item, dict):
            continue
        hits.append({
            "title": item.get("title") or item.get("source"),
            "page": item.get("page"),
            "excerpt": str(item.get("excerpt") or "")[:2200],
            "url": item.get("url"),
            "vehicle": item.get("vehicle") if isinstance(item.get("vehicle"), dict) else {},
            "text_extraction": item.get("text_extraction"),
        })
    return {"status": result.get("status"), "requested_make": make, "result_count": len(hits), "hits": hits}


async def _search_input(page: Any) -> Any | None:
    selectors = (
        "input[type='search']", "input[placeholder*='search' i]", "input[aria-label*='search' i]",
        "input[name*='search' i]", "input[id*='search' i]", "input[placeholder*='keyword' i]",
        "input[aria-label*='keyword' i]",
    )
    frames = [page, *list(getattr(page, "frames", []) or [])]
    seen: set[int] = set()
    for frame in frames:
        if id(frame) in seen:
            continue
        seen.add(id(frame))
        for selector in selectors:
            try:
                loc = frame.locator(selector).first
                if await loc.is_visible(timeout=350):
                    return loc
            except Exception:  # noqa: BLE001
                continue
    return None


async def search_alldata(browser: Any, query: str) -> dict[str, Any]:
    state = await browser.start(auto_login=True)
    page = browser._page  # noqa: SLF001 - provider automation owned by this service
    if page is None:
        return {"attempted": True, "searched": False, "verified": False, "reason": "No active ALLDATA page."}
    if not state.get("authenticated"):
        return {
            "attempted": True, "searched": False, "verified": False,
            "human_action_required": True,
            "reason": "ALLDATA requires a human authentication step before search can continue.",
            "status": state,
        }

    box = await _search_input(page)
    if box is None:
        for label in ("Search", "Keyword Search", "Find"):
            try:
                nav = page.get_by_text(label, exact=False).first
                if await nav.is_visible(timeout=400):
                    await nav.click(timeout=3_000)
                    await asyncio.sleep(0.5)
                    box = await _search_input(page)
                    if box is not None:
                        break
            except Exception:  # noqa: BLE001
                continue
    if box is None:
        return {
            "attempted": True, "searched": False, "verified": False,
            "reason": "No searchable ALLDATA field was found in the authenticated provider UI.",
            "url": str(page.url)[: ro.MAX_URL_CHARS], "title": (await page.title())[:300],
        }

    try:
        await box.fill(query)
        await box.press("Enter")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=12_000)
        except Exception:  # noqa: BLE001 - SPA search may not navigate
            await asyncio.sleep(1)
        body = str(await page.locator("body").inner_text(timeout=10_000) or "")
    except Exception as exc:  # noqa: BLE001
        return {
            "attempted": True, "searched": False, "verified": False,
            "reason": f"ALLDATA search interaction failed: {type(exc).__name__}.",
            "url": str(page.url)[: ro.MAX_URL_CHARS],
        }
    matched = [token for token in _tokens(query) if token in body.casefold()]
    # This generic keyword search never selects a vehicle -- it has no concept
    # of vehicle-scoped results at all -- so it can never truthfully claim
    # "verified" regardless of what the URL says. research_alldata_navigation's
    # vehicle-first search is what supplies real vehicle-selection evidence;
    # this path only exists as a last-resort fallback when that flow can't
    # even find a search field to try.
    claim = research_verification.unselected_source_claim(
        "This search never confirmed a vehicle selection; it only reached a generic keyword search field."
    )
    return {
        "attempted": True, "searched": True, "verified": claim["verified"],
        "verification_reason": claim["reason"],
        "query_submitted": True, "query": query, "url": str(page.url)[: ro.MAX_URL_CHARS],
        "title": (await page.title())[:300], "matched_terms": matched[:12],
        "relevance_score": len(matched), "page_text": body[:12_000],
        "provenance": {"provider": ro.PROVIDER_LABEL, "licensed_session": True, "query_submitted": True},
    }


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


async def search_public_oem(query: str, make: Optional[str]) -> dict[str, Any]:
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


async def _search_alldata_best_available(browser: Any, query: str) -> dict[str, Any]:
    """Prefer model-driven ALLDATA navigation; fall back to the fixed sequence.

    A live client is only present when this research request was triggered
    from an active conversation turn (see the Orchestrator._run wrapper
    installed below) -- that's the only context where reusing the app's one
    running model worker is safe. Anything else (a model calling
    collision_research(action="full_research") on its own, or the agent
    failing before it can even authenticate) uses the deterministic
    vehicle-first search, unchanged.
    """
    client = _ACTIVE_RESEARCH_CLIENT.get()
    if client is not None:
        try:
            from . import research_alldata_agent
            from . import research_alldata_navigation as nav

            vehicle = nav.vehicle_from_query(query)
            if vehicle.get("label"):
                topic = nav.topic_from_query(query, vehicle)
                return await research_alldata_agent.run_agent_search(
                    client=client, browser=browser, vehicle=vehicle, topic=topic,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("ALLDATA agent navigation failed; falling back to deterministic search: %s", exc)
    return await search_alldata(browser, query)


async def full_research(args: dict[str, Any], *, adas: Any, browser: Any) -> dict[str, Any]:
    from . import adas_si as adas_mod

    raw = str(args.get("query") or "").strip()
    query = focused_query(raw)
    if not query:
        raise ValueError("query is required")
    make = str(args.get("manufacturer") or "").strip() or requested_make(query, adas_mod)
    preserve = bool(args.get("preserve") is True or preserve_requested(raw))

    local = adas.search({"query": query})
    compact_local = _compact_adas(local, make or None)
    local_ledger = {
        "source": "ADAS SI", "attempted": True, "searched": True,
        "verified": local.get("status") in {"success", "partial_success", "no_result"},
        "result_count": compact_local["result_count"], "requested_make": make or None,
    }

    try:
        alldata = await _search_alldata_best_available(browser, query)
    except Exception as exc:  # noqa: BLE001
        alldata = {"attempted": True, "searched": False, "verified": False, "reason": f"ALLDATA provider error: {type(exc).__name__}: {exc}"}
    alldata_ledger = {
        "source": "ALLDATA", "attempted": bool(alldata.get("attempted", True)),
        "searched": bool(alldata.get("searched")), "verified": bool(alldata.get("verified")),
        "query_submitted": bool(alldata.get("query_submitted")), "url": alldata.get("url"),
        "reason": alldata.get("reason"), "human_action_required": bool(alldata.get("human_action_required")),
    }

    try:
        public = await search_public_oem(query, make or None)
    except Exception as exc:  # noqa: BLE001
        public = {"searched": False, "verified": False, "sources": [], "read_results": [], "result_count": 0, "error": f"{type(exc).__name__}: {exc}"}
    public_ledger = {
        "source": "Public OEM web", "attempted": True, "searched": bool(public.get("searched")),
        "verified": bool(public.get("verified")), "result_count": int(public.get("result_count") or 0),
        "reason": public.get("error"),
    }

    captures: list[dict[str, Any]] = []
    if preserve:
        if alldata.get("verified") and alldata.get("query_submitted") and int(alldata.get("relevance_score") or 0) >= 2:
            try:
                captures.append({"source": "ALLDATA", **(await browser._capture_to_adas({"vehicle": make or "Vehicle", "topic": query[:120]}))})  # noqa: SLF001
            except Exception as exc:  # noqa: BLE001
                captures.append({"source": "ALLDATA", "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        for source in (public.get("sources") or [])[:4]:
            if not isinstance(source, dict) or _source_score(source, make or None) < 8:
                continue
            url = str(source.get("url") or "").strip()
            if not url:
                continue
            try:
                saved = await research_capture.public_capture({
                    "url": url, "manufacturer": make or "OEM", "vehicle": make or "",
                    "topic": source.get("title") or query[:120],
                }, adas)
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
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        from ..tools import registry as registry_mod

        schema = registry_mod.TOOL_SCHEMAS.get("collision_research", {})
        props = schema.get("parameters", {}).get("properties", {})
        enum = props.get("action", {}).get("enum")
        if isinstance(enum, list) and "full_research" not in enum:
            enum.append("full_research")
        props.setdefault("preserve", {"type": "boolean", "description": "Preserve relevant newly found evidence into ADAS SI."})

        previous_init = registry_mod.Registry.__init__
        if not getattr(previous_init, "_xomni_full_research", False):
            def registry_init(self, *args, **kwargs):
                previous_init(self, *args, **kwargs)
                prior = self._handlers.get("collision_research")  # noqa: SLF001

                async def handler(tool_args: dict[str, Any]):
                    if str(tool_args.get("action") or "").casefold() != "full_research":
                        if prior is None:
                            raise ValueError("Collision research operator is unavailable.")
                        value = prior(tool_args)
                        return await value if hasattr(value, "__await__") else value
                    from ..config import Settings
                    from . import adas_si as adas_si_mod
                    settings = Settings.load()
                    adas = adas_si_mod.AdasSI(settings.adas_si_root, settings.root / "data" / "capabilities" / "adas_si" / "index.sqlite")
                    return await full_research(tool_args, adas=adas, browser=ro.get_browser(settings.root, adas=adas))

                self.register("collision_research", handler)

            registry_init._xomni_full_research = True  # type: ignore[attr-defined]
            registry_mod.Registry.__init__ = registry_init

        try:
            from ..orchestrator import loop as loop_mod
            previous_run = loop_mod.Orchestrator._run
            if not getattr(previous_run, "_xomni_full_research", False):
                async def run(self, conversation_id, user_message, approved_tool, approval_context):
                    if approved_tool or not full_research_request(user_message) or self.registry.tier("collision_research") != "operator_authorized":
                        async for event in previous_run(self, conversation_id, user_message, approved_tool, approval_context):
                            yield event
                        return

                    history = self.store.get_messages(conversation_id)
                    user_message_id = next((m.get("id") for m in reversed(history) if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("id"), int)), None)
                    call_id = f"routed_collision_research_full_{conversation_id}_{len(history)}"
                    context = approval_context if isinstance(approval_context, dict) else {}
                    args = {"action": "full_research", "query": str(user_message or "")[:2000], "preserve": preserve_requested(user_message)}
                    yield {"type": "tool_start", "name": "collision_research", "args": args}
                    # Hand the live model client to full_research() for the duration of
                    # this one invocation only, via the contextvar -- never through
                    # `args`, which Registry.invoke logs/audits as plain data. This is
                    # the only call site where reusing self.client is safe: it's the
                    # app's one already-running ModelRouter/worker, owned by this
                    # orchestrator instance, not a second one constructed on the side.
                    client_token = _ACTIVE_RESEARCH_CLIENT.set(self.client)
                    try:
                        result = await self.registry.invoke(
                            "collision_research", args, message_id=user_message_id,
                            conversation_id=conversation_id, tool_call_id=call_id,
                            user_id=context.get("user_id"), role=context.get("role"),
                        )
                    except Exception as exc:  # noqa: BLE001
                        result = {
                            "status": "failed", "action": "full_research", "query": focused_query(user_message),
                            "workflow_complete": False, "external_search_verified": False,
                            "source_ledger": [], "error": f"{type(exc).__name__}: {exc}",
                        }
                    finally:
                        _ACTIVE_RESEARCH_CLIENT.reset(client_token)
                    _persist_research_task(conversation_id, result, len(history))
                    artifact = {"type": "research_provider", "data": result}
                    yield {"type": "tool_result", "name": "collision_research", "result": result}
                    yield {"type": "artifact", "artifact": artifact}
                    summary = await synthesize(self, user_message, result)
                    output_id = self.store.add_message(
                        conversation_id, "assistant", summary, worker_used=self.router.active_name, artifacts=[artifact]
                    )
                    yield {"type": "token", "text": summary}
                    yield {"type": "done", "message_id": output_id, "worker": self.router.active_name, "artifacts": [artifact]}

                run._xomni_full_research = True  # type: ignore[attr-defined]
                loop_mod.Orchestrator._run = run
        except Exception:  # noqa: BLE001
            pass

        _INSTALLED = True
