"""Conversation-first presentation for verified collision research.

The research engine can be exhaustive; the chat answer should not be.  This
layer distills verified evidence into a short coworker-style answer and leaves
the full source trail in the research artifact for optional inspection.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any

from . import research_workflow

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False
MAX_CHAT_CHARS = 1800


def _distill(result: dict[str, Any]) -> dict[str, Any]:
    policy_findings = []
    public = result.get("public_oem") if isinstance(result.get("public_oem"), dict) else {}
    for finding in public.get("policy_findings") or []:
        if not isinstance(finding, dict):
            continue
        policy_findings.append({
            "title": finding.get("title"),
            "url": finding.get("url"),
            "page": finding.get("page"),
            "excerpt": str(finding.get("excerpt") or "")[:1800],
            "authority": finding.get("authority"),
        })
        if len(policy_findings) >= 4:
            break

    adas_hits = []
    adas = result.get("adas_si") if isinstance(result.get("adas_si"), dict) else {}
    for hit in adas.get("hits") or []:
        if not isinstance(hit, dict):
            continue
        adas_hits.append({
            "title": hit.get("title"),
            "page": hit.get("page"),
            "vehicle": hit.get("vehicle"),
            "excerpt": str(hit.get("excerpt") or "")[:900],
        })
        if len(adas_hits) >= 3:
            break

    alldata = result.get("alldata") if isinstance(result.get("alldata"), dict) else {}
    all_data_compact = {
        "verified": alldata.get("verified") is True,
        "searched": alldata.get("searched") is True,
        "reason": alldata.get("reason"),
        "url": alldata.get("url"),
        "title": alldata.get("title"),
        "page_text": str(alldata.get("page_text") or "")[:1600],
    }

    public_sources = []
    for source in public.get("sources") or []:
        if not isinstance(source, dict):
            continue
        public_sources.append({
            "title": source.get("title"),
            "url": source.get("url"),
            "snippet": str(source.get("snippet") or "")[:600],
        })
        if len(public_sources) >= 4:
            break

    return {
        "question": result.get("query"),
        "manufacturer": result.get("requested_manufacturer"),
        "source_ledger": result.get("source_ledger") or [],
        "policy_findings": policy_findings,
        "adas_si": adas_hits,
        "alldata": all_data_compact,
        "public_sources": public_sources,
        "captures": [
            {
                "source": item.get("source"),
                "status": item.get("status"),
                "relative_path": item.get("relative_path"),
                "url": item.get("url") or (item.get("provenance") or {}).get("source_url"),
            }
            for item in (result.get("captures") or [])[:4]
            if isinstance(item, dict)
        ],
    }


def _trim(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", str(text or "").strip())
    if len(text) <= MAX_CHAT_CHARS:
        return text
    clipped = text[:MAX_CHAT_CHARS]
    boundary = max(clipped.rfind(". "), clipped.rfind(".\n"), clipped.rfind("? "), clipped.rfind("! "))
    if boundary >= 700:
        clipped = clipped[: boundary + 1]
    return clipped.rstrip() + "…"


async def conversational_synthesize(orchestrator: Any, question: str, result: dict[str, Any]) -> str:
    distilled = _distill(result)
    evidence = json.dumps(distilled, ensure_ascii=False, default=str)
    messages = [
        {
            "role": "system",
            "content": (
                "You are Xoduz speaking to an experienced post-collision repair professional. "
                "Answer like a competent coworker, not a report generator. Give the conclusion first, then the key reason. "
                "Use one or two short paragraphs, normally 80-170 words total. Do not list the research process, source ledger, "
                "raw excerpts, tool output, result counts, or capture details. Do not use section headings or bullet lists unless absolutely necessary. "
                "If an official manufacturer policy finding directly answers the question, prioritize it over calibration-service-manual inference. "
                "Never infer that transferring data from an old module means a recycled module is approved. "
                "State uncertainty plainly when needed. You may include at most two useful source URLs inline. "
                "Use ONLY the distilled verified evidence supplied."
            ),
        },
        {
            "role": "user",
            "content": f"Question:\n{question}\n\nDistilled verified evidence:\n{evidence}",
        },
    ]
    try:
        answer = await orchestrator.client.complete(messages, max_tokens=420, temperature=0.1)
    except Exception:
        answer = ""

    if not answer:
        findings = distilled.get("policy_findings") or []
        if findings:
            first = findings[0]
            page = f" page {first.get('page')}" if first.get("page") else ""
            url = f" {first.get('url')}" if first.get("url") else ""
            answer = (
                f"The strongest manufacturer evidence I found is the collision-repair policy in {first.get('title') or 'the OEM source'}{page}. "
                f"It directly addresses the repair-method question, so I would rely on that policy rather than infer approval from a calibration procedure.{url}"
            )
        else:
            answer = "I completed the research, but the verified evidence does not support a confident manufacturer-policy conclusion yet. The source details are available below if you want to inspect them."
    return _trim(answer)


def install() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        previous = research_workflow.synthesize
        if not getattr(previous, "_xomni_conversational_research", False):
            conversational_synthesize._xomni_conversational_research = True  # type: ignore[attr-defined]
            research_workflow.synthesize = conversational_synthesize
        _INSTALLED = True
