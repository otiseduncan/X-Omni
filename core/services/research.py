"""Bounded, source-visible web research for chat turns.

This is deliberately a search tool, not an autonomous browser. It queries
two no-key providers, returns short result excerpts, and leaves synthesis to
the normal model turn. Source text is untrusted evidence and never authority
to execute instructions.
"""

from __future__ import annotations

import asyncio
import html
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, parse_qsl, quote_plus, unquote, urlencode, urlparse, urlunparse

import httpx

from ..tools.registry import Registry

MAX_QUERY_CHARS = 400
MAX_RESULTS = 8
MAX_PROVIDER_RESPONSE_BYTES = 2_000_000
SECRET_QUERY_KEYS = {
    "api_key", "apikey", "authorization", "key", "password", "secret", "token",
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; X-Omni/1.0; local-first-assistant)",
    "Accept-Language": "en-US,en;q=0.8",
}


class ProviderResponseError(RuntimeError):
    pass


async def _bounded_request(
    method: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    allowed_content_types: tuple[str, ...],
) -> bytes:
    """Read one fixed-provider response without redirects or unbounded buffering."""
    async with httpx.AsyncClient(
        timeout=12, follow_redirects=False, headers=HEADERS
    ) as client:
        async with client.stream(method, url, params=params, data=data) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if not content_type or not any(
                content_type == allowed or content_type.startswith(f"{allowed}+")
                for allowed in allowed_content_types
            ):
                raise ProviderResponseError("provider returned an unsupported content type")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
                    raise ProviderResponseError("provider response exceeded the byte limit")
            return bytes(body)


async def _bounded_get(
    url: str,
    *,
    params: dict[str, str] | None = None,
    allowed_content_types: tuple[str, ...],
) -> bytes:
    return await _bounded_request(
        "GET",
        url,
        params=params,
        allowed_content_types=allowed_content_types,
    )


async def _bounded_post(
    url: str,
    *,
    data: dict[str, str],
    allowed_content_types: tuple[str, ...],
) -> bytes:
    return await _bounded_request(
        "POST",
        url,
        data=data,
        allowed_content_types=allowed_content_types,
    )


def _clean_html(value: str) -> str:
    text = re.sub(r"<(script|style).*?</\\1>", " ", value or "", flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def _safe_url(value: str) -> str:
    parsed = urlparse(html.unescape(value or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in SECRET_QUERY_KEYS
    ]
    return urlunparse(parsed._replace(query=urlencode(query), fragment=""))


def _duckduckgo_target(value: str) -> str:
    target = html.unescape(value or "")
    if target.startswith("//"):
        target = f"https:{target}"
    parsed = urlparse(target)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        redirect = parse_qs(parsed.query).get("uddg", [""])[0]
        if redirect:
            target = unquote(redirect)
    return _safe_url(target)


def _source(provider: str, title: str, url: str, snippet: str, published_at: str = "") -> dict:
    return {
        "provider": provider,
        "title": _clean_html(title)[:240] or url,
        "url": _safe_url(url),
        "snippet": _clean_html(snippet)[:700],
        "published_at": str(published_at or "")[:120],
        "source_type": "web_result",
        "provenance": "External provider result; verify the linked source before relying on it.",
    }


def _parse_duckduckgo(page: str, query: str, limit: int) -> list[dict]:
    patterns = (
        r'<a[^>]*class=["\'][^"\']*result__a[^"\']*["\'][^>]*>.*?</a>',
        r'<a[^>]*data-testid=["\']result-title-a["\'][^>]*>.*?</a>',
        r'<a[^>]*class=["\'][^"\']*result-link[^"\']*["\'][^>]*>.*?</a>',
    )
    anchors: list[re.Match[str]] = []
    for pattern in patterns:
        anchors = list(re.finditer(pattern, page, flags=re.I | re.S))
        if anchors:
            break

    output: list[dict] = []
    for index, match in enumerate(anchors):
        anchor = match.group(0)
        href_match = re.search(r'href=["\']([^"\']+)["\']', anchor, flags=re.I)
        if not href_match:
            continue
        url = _duckduckgo_target(href_match.group(1))
        if not url:
            continue
        end = anchors[index + 1].start() if index + 1 < len(anchors) else min(
            len(page), match.end() + 2200
        )
        window = page[match.end():end]
        snippet_match = re.search(
            r'<(?:a|div|td)[^>]*class=["\'][^"\']*result(?:__|-)snippet[^"\']*["\'][^>]*>'
            r'(.*?)</(?:a|div|td)>',
            window,
            flags=re.I | re.S,
        )
        output.append(_source(
            "duckduckgo",
            _clean_html(anchor) or query,
            url,
            snippet_match.group(1) if snippet_match else "",
        ))
        if len(output) >= limit:
            break
    return output


async def _search_duckduckgo(query: str, limit: int) -> tuple[list[dict], dict]:
    endpoints = ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/")
    for endpoint in endpoints:
        try:
            body = await _bounded_post(
                endpoint,
                data={"q": query},
                allowed_content_types=("text/html",),
            )
            sources = _parse_duckduckgo(body.decode("utf-8", errors="replace"), query, limit)
            if sources:
                return sources, {"provider": "duckduckgo", "status": "healthy", "results": len(sources)}
        except (httpx.HTTPError, OSError, ProviderResponseError):
            continue
    return [], {"provider": "duckduckgo", "status": "degraded", "results": 0}


async def _search_google_news(query: str, limit: int) -> tuple[list[dict], dict]:
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        body = await _bounded_get(
            url,
            allowed_content_types=("application/rss", "application/xml", "text/xml"),
        )
        root = ET.fromstring(body)
    except (httpx.HTTPError, ET.ParseError, OSError, ProviderResponseError):
        return [], {"provider": "google_news", "status": "degraded", "results": 0}

    output = []
    for item in root.findall("./channel/item")[:limit]:
        link = _safe_url(item.findtext("link") or "")
        if not link:
            continue
        output.append(_source(
            "google_news",
            item.findtext("title") or query,
            link,
            item.findtext("description") or "",
            item.findtext("pubDate") or "",
        ))
    return output, {"provider": "google_news", "status": "healthy" if output else "degraded", "results": len(output)}


def _dedupe(sources: list[dict], limit: int) -> list[dict]:
    output: list[dict] = []
    seen: set[str] = set()
    for item in sources:
        key = (item.get("url") or item.get("title") or "").strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(item)
        if len(output) >= limit:
            break
    return output


async def search_current(args: dict[str, Any]) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query exceeds the {MAX_QUERY_CHARS}-character limit")
    if Registry.redact_sensitive(query) != query:
        raise ValueError("query contains sensitive material and cannot be sent to external providers")
    try:
        limit = int(args.get("max_results") or 6)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_results must be an integer") from exc
    limit = max(1, min(limit, MAX_RESULTS))

    provider_results = await asyncio.gather(
        _search_duckduckgo(query, limit),
        _search_google_news(query, limit),
    )
    sources = _dedupe(
        [item for found, _status in provider_results for item in found],
        limit,
    )
    provider_status = [status for _found, status in provider_results]
    queried_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    return {
        "ok": bool(sources),
        "status": "healthy" if sources else "warning",
        "query": query,
        "queried_at": queried_at,
        "external_network": True,
        "source_bounded": True,
        "providers": provider_status,
        "sources": [dict(item, index=index) for index, item in enumerate(sources, start=1)],
        "summary": (
            f"Found {len(sources)} external source result(s). Synthesize only from these excerpts "
            "and cite their source numbers."
            if sources
            else "No reliable source result was returned. Do not infer that an event did not happen."
        ),
    }
