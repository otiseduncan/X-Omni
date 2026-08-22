"""Deep policy-source reading for post-collision research.

Broad search snippets are useful for discovery, but collision policy answers can
live in a sidebar or on a later PDF page.  This layer adds targeted policy
queries and page-level PDF inspection so X does not stop at the article title or
first page when the question is about recycled/aftermarket/used parts.
"""

from __future__ import annotations

import io
import re
import threading
from typing import Any, Optional
from urllib.parse import urlparse

from pypdf import PdfReader

from . import research_capture
from . import research_operator as ro
from . import research_workflow

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False
_POLICY_RE = re.compile(
    r"\b(?:recycled|aftermarket|salvage|used\s+(?:part|parts|module|sensor)|"
    r"remanufactured|non[-\s]?oem|genuine\s+parts?)\b",
    re.IGNORECASE,
)
_POLICY_TERMS = (
    "methods not approved",
    "not approved",
    "recycled parts",
    "aftermarket and recycled",
    "aftermarket parts",
    "genuine oem parts",
    "genuine parts",
    "used parts",
)


def _policy_score(source: dict[str, Any], make: Optional[str]) -> int:
    score = research_workflow._source_score(source, make)  # noqa: SLF001
    text = f"{source.get('title') or ''} {source.get('snippet') or ''}".casefold()
    url = str(source.get("url") or "")
    host = (urlparse(url).hostname or "").casefold()
    for term in _POLICY_TERMS:
        if term in text:
            score += 14
    if make and make.casefold() == "toyota" and "toyotapartsandservice.com" in host:
        score += 24
    if "collision" in text or "collision" in host:
        score += 6
    return score


def _excerpt(text: str, start: int, length: int = 1700) -> str:
    begin = max(0, start - 500)
    return " ".join(text[begin:begin + length].split())


def _page_findings(text: str, *, page: int | None, url: str, title: str, make: Optional[str]) -> list[dict[str, Any]]:
    folded = text.casefold()
    hits: list[tuple[int, str]] = []
    for term in _POLICY_TERMS:
        pos = folded.find(term)
        if pos >= 0:
            hits.append((pos, term))
    if not hits:
        return []
    hits.sort()
    pos, term = hits[0]
    host = (urlparse(url).hostname or "").casefold()
    authority = (
        "official_manufacturer"
        if make and make.casefold() == "toyota" and "toyotapartsandservice.com" in host
        else "manufacturer_or_collision_source"
    )
    return [{
        "title": title or url,
        "url": url,
        "page": page,
        "matched_term": term,
        "excerpt": _excerpt(text, pos),
        "authority": authority,
    }]


async def _read_policy_source(source: dict[str, Any], make: Optional[str]) -> list[dict[str, Any]]:
    url = str(source.get("url") or "").strip()
    title = str(source.get("title") or "").strip()
    if not url:
        return []

    try:
        final_url, raw, content_type = await research_capture._bounded_public_fetch(url)  # noqa: SLF001
    except Exception:
        # Fall back to the normal bounded reader for HTML sources.
        try:
            read = await ro.public_read({"url": url})
        except Exception:
            return []
        return _page_findings(
            str(read.get("page_text") or ""),
            page=None,
            url=str(read.get("url") or url),
            title=str(read.get("title") or title),
            make=make,
        )

    is_pdf = content_type == "application/pdf" or raw.startswith(b"%PDF")
    if not is_pdf:
        try:
            document = raw.decode("utf-8", errors="replace")
            text = research_capture._visible_text(document)  # noqa: SLF001
        except Exception:
            return []
        return _page_findings(text, page=None, url=final_url, title=title, make=make)

    findings: list[dict[str, Any]] = []
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        for page_number, page in enumerate(reader.pages, 1):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            findings.extend(
                _page_findings(
                    text,
                    page=page_number,
                    url=final_url,
                    title=title,
                    make=make,
                )
            )
            if len(findings) >= 4:
                break
    except Exception:
        return []
    return findings


async def deep_search_public_oem(query: str, make: Optional[str]) -> dict[str, Any]:
    """Run the normal OEM search plus focused collision-policy discovery."""
    primary = await _PREVIOUS_SEARCH(query, make)
    merged: dict[str, dict[str, Any]] = {}
    for item in primary.get("sources") or []:
        if isinstance(item, dict) and item.get("url"):
            merged[str(item["url"])] = dict(item)

    if _POLICY_RE.search(query):
        focused_queries = [
            f"{make or ''} Collision Pros approved repair methods recycled parts",
            f"{make or ''} methods not approved aftermarket recycled parts collision repair",
        ]
        if (make or "").casefold() == "toyota":
            focused_queries.append(
                "Toyota Lexus approved repair methods installing aftermarket recycled parts Collision Pros"
            )
        for focused in focused_queries:
            try:
                result = await ro.public_search({"query": focused.strip(), "manufacturer": make or ""})
            except Exception:
                continue
            for item in result.get("sources") or []:
                if not isinstance(item, dict) or not item.get("url"):
                    continue
                url = str(item["url"])
                current = merged.get(url)
                if current is None or _policy_score(item, make) > _policy_score(current, make):
                    merged[url] = dict(item)

    sources = list(merged.values())
    sources.sort(key=lambda item: _policy_score(item, make), reverse=True)

    policy_findings: list[dict[str, Any]] = []
    read_results = list(primary.get("read_results") or [])
    for source in sources[:6]:
        if _policy_score(source, make) < 8:
            continue
        findings = await _read_policy_source(source, make)
        if findings:
            policy_findings.extend(findings)
        if len(policy_findings) >= 6:
            break

    result = dict(primary)
    result["sources"] = sources[:10]
    result["result_count"] = len(sources)
    result["read_results"] = read_results
    result["policy_findings"] = policy_findings[:6]
    result["deep_policy_read"] = True
    return result


_PREVIOUS_SEARCH = research_workflow.search_public_oem


def install() -> None:
    global _INSTALLED, _PREVIOUS_SEARCH
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        previous = research_workflow.search_public_oem
        _PREVIOUS_SEARCH = previous
        if not getattr(previous, "_xomni_deep_policy", False):
            deep_search_public_oem._xomni_deep_policy = True  # type: ignore[attr-defined]
            research_workflow.search_public_oem = deep_search_public_oem
        _INSTALLED = True
