"""Conversation-first presentation for verified collision research.

The research engine can be exhaustive; the chat answer should not be. This
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


def _compact_finding(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": finding.get("title"),
        "url": finding.get("url"),
        "page": finding.get("page"),
        "excerpt": str(finding.get("excerpt") or "")[:1800],
        "authority": finding.get("authority"),
        "finding_kind": finding.get("finding_kind"),
        "matched_term": finding.get("matched_term"),
        "text_extraction": finding.get("text_extraction"),
    }


def _distill(result: dict[str, Any]) -> dict[str, Any]:
    public = result.get("public_oem") if isinstance(result.get("public_oem"), dict) else {}

    policy_findings = [
        _compact_finding(item)
        for item in (public.get("policy_findings") or [])[:4]
        if isinstance(item, dict)
    ]
    calibration_findings = [
        _compact_finding(item)
        for item in (public.get("calibration_findings") or [])[:6]
        if isinstance(item, dict)
    ]

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
            "deep_calibration_rule": hit.get("deep_calibration_rule") is True,
            "matched_rule": hit.get("matched_rule"),
        })
        if len(adas_hits) >= 4:
            break

    alldata = result.get("alldata") if isinstance(result.get("alldata"), dict) else {}
    all_data_compact = {
        "verified": alldata.get("verified") is True,
        "searched": alldata.get("searched") is True,
        "reason": alldata.get("reason"),
        "url": alldata.get("url"),
        "title": alldata.get("title"),
        "page_text": str(alldata.get("page_text") or "")[:1600],
        "vehicle": alldata.get("vehicle"),
        "topic": alldata.get("topic"),
        "result_title": alldata.get("result_title"),
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
        "calibration_findings": calibration_findings,
        "adas_si": adas_hits,
        "alldata": all_data_compact,
        "public_sources": public_sources,
        "deep_read": {
            "calibration": public.get("deep_calibration_read") is True,
            "policy": public.get("deep_policy_read") is True,
            "metrics": public.get("deep_read_metrics") or {},
        },
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


def _plain_chat(text: str) -> str:
    """Remove Markdown control syntax from conversational research prose.

    X's chat stream is intentionally plain conversational text. Models may
    still emit Markdown emphasis even when asked not to; normalize it before
    persistence so users neither see nor hear literal asterisks/backticks.
    Source URLs remain visible after Markdown link labels are flattened.
    """
    value = str(text or "")
    value = re.sub(r"!\[([^\]]*)\]\(([^)]*)\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 — \2", value)
    value = re.sub(r"```[^\n]*\n?", "", value)
    value = value.replace("```", "")
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = re.sub(r"^\s{0,3}#{1,6}\s+", "", value, flags=re.MULTILINE)
    value = re.sub(r"^\s*>+\s?", "", value, flags=re.MULTILINE)
    value = re.sub(r"^\s*[-+*]\s+", "", value, flags=re.MULTILINE)
    value = value.replace("***", "").replace("___", "")
    value = value.replace("**", "").replace("__", "").replace("~~", "")
    value = value.replace("*", "")
    return value.strip()


def _trim(text: str) -> str:
    text = _plain_chat(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) <= MAX_CHAT_CHARS:
        return text
    clipped = text[:MAX_CHAT_CHARS]
    boundary = max(
        clipped.rfind(". "),
        clipped.rfind(".\n"),
        clipped.rfind("? "),
        clipped.rfind("! "),
    )
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
                "raw excerpts, tool output, result counts, or capture details. Do not use Markdown, section headings, bullets, bold markers, "
                "asterisks, or code formatting. Manufacturer collision policy and explicit manufacturer calibration-trigger language outrank "
                "inference from a service procedure. A calibration requirement may be buried in a sidebar, warning, applicability note, or late "
                "PDF page; use deep calibration findings when present. Never infer that transferring data from an old module means a recycled "
                "module is approved. State uncertainty plainly when needed. You may include at most two useful source URLs inline. "
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
        findings = [
            *(distilled.get("policy_findings") or []),
            *(distilled.get("calibration_findings") or []),
        ]
        if findings:
            first = findings[0]
            page = f" page {first.get('page')}" if first.get("page") else ""
            url = f" {first.get('url')}" if first.get("url") else ""
            answer = (
                f"The strongest manufacturer evidence I found is in {first.get('title') or 'the OEM source'}{page}. "
                f"It directly addresses the repair/calibration requirement, so I would rely on that manufacturer statement rather than infer the answer from a generic procedure.{url}"
            )
        else:
            answer = (
                "I completed the research, but the verified evidence does not support a confident manufacturer conclusion yet. "
                "The source details are available below if you want to inspect them."
            )
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
