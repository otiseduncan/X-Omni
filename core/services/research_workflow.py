"""Verified composite post-collision research workflow.

A model promise such as "I'll check ALLDATA next" is not evidence that the
provider was queried.  This module turns a post-collision research request into
one deterministic operator workflow:

    ADAS SI -> authenticated ALLDATA -> public OEM/manufacturer web

Every lane records an explicit source-ledger entry.  The final chat artifact and
answer can therefore distinguish "actually searched" from "intended to
search".  The workflow also keeps ADAS vehicle identity strict and optionally
preserves relevant newly found evidence into ADAS SI.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from typing import Any, Optional
from urllib.parse import urlparse

from . import research_capture
from . import research_operator as ro

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False

_RESEARCH_INTENT_RE = re.compile(
    r"\b(?:research|find|verify|check|look\s*up|investigate)\b",
    re.IGNORECASE,
)
_RESEARCH_DOMAIN_RE = re.compile(
    r"\b(?:all\s*data|alldata|oem|manufacturer|collision|position\s+statement|"
    r"recycled|used\s+(?:module|sensor|part)|insurance|blind\s+spot|adas\s+si|"
    r"service\s+information|repair\s+procedure|technical\s+bulletin)\b",
    re.IGNORECASE,
)
_RO_ONLY_RE = re.compile(
    r"^\s*(?:please\s+)?research\s+(?:this|that|the)\s+(?:repair\s+order|ro)\b",
    re.IGNORECASE,
)
_PRESERVE_RE = re.compile(
    r"\b(?:preserve|save|capture|store|archive|add|import|keep)\b.{0,100}"
    r"\b(?:adas|database|library|documentation|evidence|source|pdf)\b"
    r"|\bmissing\b.{0,80}\badas\s+si\b",
    re.IGNORECASE | re.DOTALL,
)

_STOPWORDS = {
    "about", "after", "again", "alldata", "also", "and", "any", "are", "check",
    "collision", "documentation", "evidence", "find", "first", "for", "from", "have",
    "into", "look", "manufacturer", "missing", "oem", "official", "our", "please",
    "preserve", "research", "show", "source", "sources", "supporting", "that", "the",
    "then", "this", "use", "what", "whether", "with",
}


def full_research_request(user_message: object) -> bool:
    text = str(user_message or "").strip()
    if not text or _RO_ONLY_RE.search(text):
        return False
    return bool(_RESEARCH_INTENT_RE.search(text) and _RESEARCH_DOMAIN_RE.search(text))


def preserve_requested(user_message: object) -> bool:
    return bool(_PRESERVE_RE.search(str(user_message or "")))


def _focused_query(value: object) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    # The first sentence normally contains the real technical question; later
    # sentences describe which sources to search and whether to preserve them.
    first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    first = re.sub(r"^\s*(?:please\s+)?(?:research|investigate|verify|find\s+out)\s+", "", first, flags=re.I)
    return first[:500].strip(" .") or text[:500]


def _requested_make(query: str, adas_mod: Any) -> Optional[str]:
    folded = query.casefold()
    aliases = getattr(adas_mod, "MAKE_ALIASES", {}) or {}
    for alias, canonical in aliases.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(str(alias).casefold())}(?![a-z0-9])", folded):
            return str(canonical)
    for make in getattr(adas_mod, "KNOWN_MAKES", ()) or ():
        make_text = str(make)
        if re.search(rf"(?<![a-z0-9]){re.escape(make_text.casefold())}(?![a-z0-9])", folded):
            return make_text
    return None


def _tokens(query: str) -> list[str]:
    return [
        token for token in re.findall(r"[a-z0-9-]+", query.casefold())
        if len(token) >= 3 and token not in _STOPWORDS
    ][:24]


def _compact_adas(result: dict[str, Any], requested_make: Optional[str]) -> dict[str, Any]:
    hits = []
    for item in (result.get("results") or [])[:6]:
        if not isinstance(item, dict):
            continue
        vehicle = item.get("vehicle") if isinstance(item.get("vehicle"), dict) else {}
        hits.append({
            "title": item.get("title") or item.get("source"),
            "page": item.get("page"),
            "excerpt": str(item.get("excerpt") or "")[:2200],
            "url": item.get("url"),
            "vehicle": vehicle,
            "text_extraction": item.get("text_extraction"),
        })
    return {
        "status": result.get("status"),
        "requested_make": requested_make,
        "result_count": len(hits),
        "hits": hits,
        "matched_documents": [
            {
                "title": item.get("title"),
                "make": item.get("make"),
                "model": item.get("model"),
                "relative_path": item.get("relative_path"),
            }
            for item in (result.get("matched_documents") or [])[:6]
            if isinstance(item, dict)
        ],
    }


async def _find_search_input(page: Any) -> Any | None:
    selectors = (
        "input[type='search']",
        "input[placeholder*='search' i]",
        "input[aria-label*='search' i]",
        "input[name*='search' i]",
        "input[id*='search' i]",
        "input[placeholder*='keyword' i]",
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
                locator = frame.locator(selector).first
                if await locator.is_visible(timeout=350):
                    return locator
            except Exception:  # noqa: BLE001 - continue across provider UI variants
                continue
    return None


async def _search_alldata(browser: Any, query: str) -> dict[str, Any]:
    started = await browser.start(auto_login=True)
    page = browser._page  # noqa: SLF001 - same service owns provider automation
    if page is None:
        return {
            "attempted": True,
            "searched": False,
            "verified": False,
            "reason": "ALLDATA browser did not provide an active page.",
            "status": started,
        }
    if not started.get("authenticated"):
        return {
            "attempted": True,
            "searched": False,
            "verified": False,
            "human_action_required": True,
            "reason": "ALLDATA requires a human authentication step before search can continue.",
            "status": started,
        }

    search_box = await _find_search_input(page)
    if search_box is None:
        # Some ALLDATA shells expose Search as a navigation item before the
        # input is rendered. Click it only when plainly visible, then retry.
        for label in ("Search", "Keyword Search", "Find"):
            try:
                nav = page.get_by_text(label, exact=False).first
                if await nav.is_visible(timeout=400):
                    await nav.click(timeout=3_000)
                    await asyncio.sleep(0.5)
                    search_box = await _find_search_input(page)
                    if search_box is not None:
                        break
            except Exception:  # noqa: BLE001
                continue

    if search_box is None:
        return {
            "attempted": True,
            "searched": False,
            "verified": False,
            "reason": "No searchable ALLDATA field was found in the authenticated provider UI.",
            "url": str(page.url)[: ro.MAX_URL_CHARS],
            "title": (await page.title())[:300],
        }

    try:
        await search_box.fill(query)
        await search_box.press("Enter")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=12_000)
        except Exception:  # noqa: BLE001 - SPA results may not navigate
            await asyncio.sleep(1.0)
        body = await page.locator("body").inner_text(timeout=10_000)
    except Exception as exc:  # noqa: BLE001
        return {
            "attempted": True,
            "searched": False,
            "verified": False,
            "reason": f"ALLDATA search interaction failed: {type(exc).__name__}.",
            "url": str(page.url)[: ro.MAX_URL_CHARS],
        }

    body = str(body or "")
    query_tokens = _tokens(query)
    folded = body.casefold()
    matched_tokens = [token for token in query_tokens if token in folded]
    return {
        "attempted": True,
        "searched": True,
        "verified": ro._is_alldata_url(page.url),
        "query_submitted": True,
        "query": query,
        "url": str(page.url)[: ro.MAX_URL_CHARS],
        "title": (await page.title())[:300],
        "matched_terms": matched_tokens[:12],
        "relevance_score": len(matched_tokens),
        "page_text": body[:12_000],
        "provenance": {
            "provider": ro.PROVIDER_LABEL,
            "licensed_session": True,
            "query_submitted": True,
            "url": str(page.url)[: ro.MAX_URL_CHARS],
        },
    }


def _source_score(source: dict[str, Any], manufacturer: Optional[str]) -> int:
    url = str(source.get("url") or "")
    title = str(source.get("title") or "")
    snippet = str(source.get("snippet") or "")
    host = (urlparse(url).hostname or "").casefold()
    text = f"{title} {snippet}".casefold()
    score = 0
    if manufacturer:
        token = re.sub(r"[^a-z0-9]", "", manufacturer.casefold())
        if token and token in re.sub(r"[^a-z0-9]", "", host):
            score += 12
        if manufacturer.casefold() in text:
            score += 6
    for term, weight in (
        ("collision", 6), ("position statement", 8), ("repair", 3),
        ("recycled", 6), ("used part", 5), ("blind spot", 5),
        ("technical", 2), ("service bulletin", 4),
    ):
        if term in text or term in host:
            score += weight
    return score


async def _public_oem_research(query: str, manufacturer: Optional[str]) -> dict[str, Any]:
    search = await ro.public_search({"query": query, "manufacturer": manufacturer or ""})
    sources = [item for item in (search.get("sources") or []) if isinstance(item, dict)]
    sources.sort(key=lambda item: _source_score(item, manufacturer), reverse=True)
    read_results = []
    for source in sources[:3]:
        url = str(source.get("url") or "").strip()
        if not url:
            continue
        try:
            read = await ro.public_read({"url": url})
            read_results.append({
                "url": read.get("url") or url,
                "title": read.get("title") or source.get("title"),
                "content_type": read.get("content_type"),
                "page_text": str(read.get("page_text") or read.get("message") or "")[:8_000],
                "source_result": source,
            })
        except Exception as exc:  # noqa: BLE001 - one blocked page does not erase the search proof
            read_results.append({
                "url": url,
                "title": source.get("title"),
                "read_error": f"{type(exc).__name__}: {exc}",
                "source_result": source,
            })
    return {
        "searched": bool(search.get("external_network") is True and search.get("source_bounded") is True),
        "verified": bool(search.get("external_network") is True and search.get("source_bounded") is True),
        "query": search.get("query"),
        "providers": search.get("providers"),
        "sources": sources[:8],
        "read_results": read_results,
        "result_count": len(sources),
    }


async def full_research(args: dict[str, Any], *, settings: Any, adas: Any, browser: Any) -> dict[str, Any]:
    from . import adas_si as adas_mod

    raw_query = str(args.get("query") or "").strip()
    query = _focused_query(raw_query)
    if not query:
        raise ValueError("query is required")
    manufacturer = str(args.get("manufacturer") or "").strip() or _requested_make(query, adas_mod)
    preserve = bool(args.get("preserve") is True or preserve_requested(raw_query))

    adas_result = adas.search({"query": query})
    adas_compact = _compact_adas(adas_result, manufacturer or None)
    adas_ledger = {
        "source": "ADAS SI",
        "attempted": True,
        "searched": True,
        "verified": adas_result.get("status") in {"success", "partial_success", "no_result"},
        "result_count": adas_compact["result_count"],
        "requested_make": manufacturer or None,
    }

    try:
        alldata = await _search_alldata(browser, query)
    except Exception as exc:  # noqa: BLE001
        alldata = {
            "attempted": True,
            "searched": False,
            "verified": False,
            "reason": f"ALLDATA provider error: {type(exc).__name__}: {exc}",
        }
    alldata_ledger = {
        "source": "ALLDATA",
        "attempted": bool(alldata.get("attempted", True)),
        "searched": bool(alldata.get("searched")),
        "verified": bool(alldata.get("verified")),
        "query_submitted": bool(alldata.get("query_submitted")),
        "url": alldata.get("url"),
        "reason": alldata.get("reason"),
        "human_action_required": bool(alldata.get("human_action_required")),
    }

    try:
        public_oem = await _public_oem_research(query, manufacturer or None)
    except Exception as exc:  # noqa: BLE001
        public_oem = {
            "searched": False,
            "verified": False,
            "sources": [],
            "read_results": [],
            "result_count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    public_ledger = {
        "source": "Public OEM web",
        "attempted": True,
        "searched": bool(public_oem.get("searched")),
        "verified": bool(public_oem.get("verified")),
        "result_count": int(public_oem.get("result_count") or 0),
        "reason": public_oem.get("error"),
    }

    captures: list[dict[str, Any]] = []
    if preserve:
        # Capture ALLDATA only when a targeted query was actually submitted and
        # the resulting page contains meaningful query terms. Never archive the
        # provider home page merely because the browser is authenticated.
        if alldata.get("verified") and alldata.get("query_submitted") and int(alldata.get("relevance_score") or 0) >= 2:
            try:
                capture = await browser._capture_to_adas({  # noqa: SLF001 - same operator workflow
                    "vehicle": manufacturer or "Vehicle",
                    "topic": query[:120],
                })
                captures.append({"source": "ALLDATA", **capture})
            except Exception as exc:  # noqa: BLE001
                captures.append({"source": "ALLDATA", "status": "failed", "error": f"{type(exc).__name__}: {exc}"})

        # Preserve only clearly manufacturer-associated public sources. The
        # source URL remains authority; HTML snapshots stay marked derivative.
        for item in (public_oem.get("sources") or [])[:4]:
            if not isinstance(item, dict) or _source_score(item, manufacturer or None) < 8:
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            try:
                capture = await research_capture.public_capture({
                    "url": url,
                    "manufacturer": manufacturer or "OEM",
                    "vehicle": manufacturer or "",
                    "topic": item.get("title") or query[:120],
                }, adas)
                captures.append({"source": "Public OEM web", **capture})
                break
            except Exception as exc:  # noqa: BLE001
                captures.append({
                    "source": "Public OEM web",
                    "url": url,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                })

    ledger = [adas_ledger, alldata_ledger, public_ledger]
    external_verified = bool(alldata_ledger["verified"] or public_ledger["verified"])
    all_lanes_verified = all(item.get("verified") for item in ledger)
    status = "success" if all_lanes_verified else ("partial_success" if external_verified else "failed")
    return {
        "status": status,
        "action": "full_research",
        "query": query,
        "requested_manufacturer": manufacturer or None,
        "preserve_requested": preserve,
        "workflow_complete": all_lanes_verified,
        "external_search_verified": external_verified,
        "source_ledger": ledger,
        "adas_si": adas_compact,
        "alldata": alldata,
        "public_oem": public_oem,
        "captures": captures,
        "authority_note": (
            "OEM/manufacturer requirements, insurer requirements, and legal/regulatory requirements "
            "are separate authorities and must be identified separately."
        ),
    }


def _fixed_summary(result: dict[str, Any]) -> str:
    ledger = {str(item.get("source")): item for item in result.get("source_ledger") or [] if isinstance(item, dict)}
    adas = ledger.get("ADAS SI", {})
    alldata = ledger.get("ALLDATA", {})
    public = ledger.get("Public OEM web", {})
    return (
        f"Research ran with source verification. ADAS SI: searched ({adas.get('result_count', 0)} relevant passages). "
        f"ALLDATA: {'searched' if alldata.get('verified') else 'not verified as searched'}"
        f"{f\" — {alldata.get('reason')}\" if alldata.get('reason') else ''}. "
        f"Public OEM web: {'searched' if public.get('verified') else 'not verified as searched'} "
        f"({public.get('result_count', 0)} source results). Open the research card for the exact source ledger and evidence."
    )


async def _synthesize(orchestrator: Any, user_message: str, result: dict[str, Any]) -> str:
    compact = json.dumps(result, ensure_ascii=False, default=str)
    if len(compact) > 90_000:
        compact = compact[:90_000] + "\n[TRUNCATED FOR SYNTHESIS]"
    messages = [
        {
            "role": "system",
            "content": (
                "You are Xoduz, summarizing a verified post-collision research workflow. "
                "Use ONLY the evidence JSON supplied below. Never claim a source was searched unless its "
                "source_ledger entry has verified=true. Separate local ADAS SI evidence, licensed ALLDATA "
                "evidence, and public OEM/manufacturer evidence. If the evidence does not resolve the user's "
                "question, say that plainly. Prefer OEM/manufacturer requirements over secondary summaries "
                "when both exist. Do not invent citations, page numbers, policies, or source contents. Keep the "
                "answer concise but useful to a collision-repair professional."
            ),
        },
        {
            "role": "user",
            "content": f"Question:\n{user_message}\n\nVerified research result JSON:\n{compact}",
        },
    ]
    try:
        answer = await orchestrator.client.complete(messages, max_tokens=900, temperature=0.1)
    except Exception:  # noqa: BLE001 - ledger remains the authoritative fallback
        answer = ""
    proof = _fixed_summary(result)
    return f"{answer}\n\n{proof}".strip() if answer else proof


def install() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        from ..tools import registry as registry_mod

        schema = registry_mod.TOOL_SCHEMAS.get("collision_research", {})
        action_schema = schema.get("parameters", {}).get("properties", {}).get("action", {})
        enum = action_schema.get("enum")
        if isinstance(enum, list) and "full_research" not in enum:
            enum.append("full_research")
        properties = schema.get("parameters", {}).get("properties", {})
        properties.setdefault("preserve", {"type": "boolean", "description": "Preserve relevant newly found evidence into ADAS SI."})

        previous_init = registry_mod.Registry.__init__
        if not getattr(previous_init, "_xomni_full_research", False):
            def registry_init(self, *args, **kwargs):
                previous_init(self, *args, **kwargs)
                prior_handler = self._handlers.get("collision_research")  # noqa: SLF001

                async def handler(tool_args: dict[str, Any]):
                    if str(tool_args.get("action") or "").casefold() != "full_research":
                        if prior_handler is None:
                            raise ValueError("Collision research operator is unavailable.")
                        value = prior_handler(tool_args)
                        if hasattr(value, "__await__"):
                            value = await value
                        return value
                    from ..config import Settings
                    from . import adas_si as adas_si_mod
                    settings = Settings.load()
                    adas = adas_si_mod.AdasSI(
                        settings.adas_si_root,
                        settings.root / "data" / "capabilities" / "adas_si" / "index.sqlite",
                    )
                    browser = ro.get_browser(settings.root, adas=adas)
                    return await full_research(tool_args, settings=settings, adas=adas, browser=browser)

                self.register("collision_research", handler)

            registry_init._xomni_full_research = True  # type: ignore[attr-defined]
            registry_mod.Registry.__init__ = registry_init

        try:
            from ..orchestrator import loop as loop_mod
            previous_run = loop_mod.Orchestrator._run
            if not getattr(previous_run, "_xomni_full_research", False):
                async def run(self, conversation_id, user_message, approved_tool, approval_context):
                    if approved_tool or not full_research_request(user_message):
                        async for event in previous_run(
                            self, conversation_id, user_message, approved_tool, approval_context
                        ):
                            yield event
                        return

                    if self.registry.tier("collision_research") != "operator_authorized":
                        async for event in previous_run(
                            self, conversation_id, user_message, approved_tool, approval_context
                        ):
                            yield event
                        return

                    history = self.store.get_messages(conversation_id)
                    message_id = next(
                        (
                            message.get("id") for message in reversed(history)
                            if isinstance(message, dict)
                            and message.get("role") == "user"
                            and isinstance(message.get("id"), int)
                        ),
                        None,
                    )
                    call_id = f"routed_collision_research_full_{conversation_id}_{len(history)}"
                    context = approval_context if isinstance(approval_context, dict) else {}
                    args = {
                        "action": "full_research",
                        "query": str(user_message or "")[:2000],
                        "preserve": preserve_requested(user_message),
                    }
                    yield {"type": "tool_start", "name": "collision_research", "args": args}
                    try:
                        result = await self.registry.invoke(
                            "collision_research",
                            args,
                            message_id=message_id,
                            conversation_id=conversation_id,
                            tool_call_id=call_id,
                            user_id=context.get("user_id"),
                            role=context.get("role"),
                        )
                    except Exception as exc:  # noqa: BLE001
                        result = {
                            "status": "failed",
                            "action": "full_research",
                            "query": _focused_query(user_message),
                            "workflow_complete": False,
                            "external_search_verified": False,
                            "source_ledger": [],
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    artifact = {"type": "research_provider", "data": result}
                    yield {"type": "tool_result", "name": "collision_research", "result": result}
                    yield {"type": "artifact", "artifact": artifact}

                    summary = await _synthesize(self, user_message, result)
                    message_id_out = self.store.add_message(
                        conversation_id,
                        "assistant",
                        summary,
                        worker_used=self.router.active_name,
                        artifacts=[artifact],
                    )
                    if len(history) <= 1 and summary:
                        try:
                            self.store.touch_conversation(conversation_id, title=str(user_message)[:60])
                        except Exception:  # noqa: BLE001
                            pass
                    yield {"type": "token", "text": summary}
                    yield {
                        "type": "done",
                        "message_id": message_id_out,
                        "worker": self.router.active_name,
                        "artifacts": [artifact],
                    }

                run._xomni_full_research = True  # type: ignore[attr-defined]
                loop_mod.Orchestrator._run = run
        except Exception:  # noqa: BLE001 - isolated unit imports may load loop later
            pass

        _INSTALLED = True
