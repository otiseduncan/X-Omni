"""Deep manufacturer-source reading for post-collision calibration research.

Search snippets are discovery aids, not evidence boundaries.  Calibration rules
can live in a one-line note, sidebar, later PDF page, or secondary page on an OEM
collision site.  This layer therefore deep-reads relevant public OEM sources for
*all* calibration-related research, not only recycled-parts policy questions.

Behavior is intentionally bounded and respectful: targeted public search,
complete page-by-page reading of relevant PDFs (with local OCR fallback), and at
most one hop into same-host calibration/collision links on relevant HTML pages.
It does not bypass authentication, robots, paywalls, or access controls.
"""

from __future__ import annotations

import html
import io
import re
import threading
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from pypdf import PdfReader

from . import adas_calibration_depth
from . import adas_ocr
from . import research_capture
from . import research_operator as ro
from . import research_workflow

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False

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
_CALIBRATION_DISCOVERY_TERMS = (
    "calibration required",
    "calibration is required",
    "recalibration required",
    "must be calibrated",
    "must calibrate",
    "after any collision",
    "after all collisions",
    "following a collision",
    "following any collision",
    "beam axis confirmation",
    "beam axis adjustment",
    "camera aiming",
    "radar aiming",
    "target placement",
    "position statement",
    "collision repair",
)
_LINK_HINT_RE = re.compile(
    r"\b(?:calibrat|recalibrat|adas|eyesight|blind\s+spot|radar|camera|collision|"
    r"repair|position\s+statement|service\s+bulletin|technical\s+bulletin|aim|alignment)\b",
    re.IGNORECASE,
)
_ANCHOR_RE = re.compile(
    r"<a\b[^>]*?href\s*=\s*([\"'])(?P<href>.*?)\1[^>]*>(?P<label>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)


def _policy_score(source: dict[str, Any], make: Optional[str]) -> int:
    score = research_workflow._source_score(source, make)  # noqa: SLF001
    text = f"{source.get('title') or ''} {source.get('snippet') or ''}".casefold()
    url = str(source.get("url") or "")
    host = (urlparse(url).hostname or "").casefold()
    for term in _POLICY_TERMS:
        if term in text:
            score += 14
    for term in _CALIBRATION_DISCOVERY_TERMS:
        if term in text:
            score += 8
    if make and make.casefold() == "toyota" and "toyotapartsandservice.com" in host:
        score += 24
    if "collision" in text or "collision" in host:
        score += 6
    if "position" in text and "statement" in text:
        score += 6
    return score


def _excerpt(text: str, start: int, length: int = 1900) -> str:
    begin = max(0, int(start) - 520)
    return " ".join(str(text)[begin:begin + length].split())


def _policy_hit(text: str) -> tuple[int, str] | None:
    folded = text.casefold()
    hits = [(folded.find(term), term) for term in _POLICY_TERMS if folded.find(term) >= 0]
    return min(hits) if hits else None


def _calibration_hit(text: str) -> tuple[int, str] | None:
    matches = adas_calibration_depth._rule_matches(str(text or ""))  # noqa: SLF001
    if matches:
        label, position, _weight = matches[0]
        return int(position), str(label)
    folded = str(text or "").casefold()
    hits = [
        (folded.find(term), term)
        for term in _CALIBRATION_DISCOVERY_TERMS
        if folded.find(term) >= 0
    ]
    return min(hits) if hits else None


def _page_findings(
    text: str,
    *,
    page: int | None,
    url: str,
    title: str,
    make: Optional[str],
    calibration_mode: bool,
    extraction: str = "native",
) -> list[dict[str, Any]]:
    candidates: list[tuple[int, str, str]] = []
    policy = _policy_hit(text)
    if policy is not None:
        candidates.append((policy[0], policy[1], "repair_policy"))
    if calibration_mode:
        calibration = _calibration_hit(text)
        if calibration is not None:
            candidates.append((calibration[0], calibration[1], "calibration_requirement"))
    if not candidates:
        return []

    candidates.sort(key=lambda item: item[0])
    host = (urlparse(url).hostname or "").casefold()
    authority = (
        "official_manufacturer"
        if make and (
            make.casefold() in host
            or (make.casefold() == "toyota" and "toyotapartsandservice.com" in host)
        )
        else "manufacturer_or_collision_source"
    )
    output = []
    seen: set[tuple[str, str]] = set()
    for position, matched_term, kind in candidates:
        key = (matched_term.casefold(), kind)
        if key in seen:
            continue
        seen.add(key)
        output.append({
            "title": title or url,
            "url": url,
            "page": page,
            "matched_term": matched_term,
            "finding_kind": kind,
            "excerpt": _excerpt(text, position),
            "authority": authority,
            "text_extraction": extraction,
        })
    return output


def _usable_text(text: str) -> bool:
    cleaned = str(text or "").strip()
    return sum(ch.isalnum() for ch in cleaned) >= 24 and len(re.findall(r"\b\w+\b", cleaned)) >= 4


def _ocr_pdf_page(raw: bytes, page_index: int) -> str:
    """OCR one public PDF page when embedded text is absent or useless."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return ""
    document = None
    page = None
    try:
        document = pdfium.PdfDocument(raw)
        page = document[int(page_index)]
        width = page.get_size()[0] or 612
        image = page.render(scale=adas_ocr.OCR_RENDER_WIDTH / width).to_pil()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        result = adas_ocr._ocr_png(buffer.getvalue())  # noqa: SLF001 - shared local OCR engine
        return str(result.get("text") or "")
    except Exception:
        return ""
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        if document is not None:
            try:
                document.close()
            except Exception:
                pass


def _same_host_deep_links(document: str, base_url: str) -> list[tuple[str, str]]:
    """Return at most five relevant same-host links for one-hop deep reading."""
    host = (urlparse(base_url).hostname or "").casefold()
    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _ANCHOR_RE.finditer(document):
        href = html.unescape(match.group("href") or "").strip()
        label = " ".join(
            html.unescape(re.sub(r"<[^>]+>", " ", match.group("label") or "")).split()
        )
        context = f"{label} {href}"
        if not href or not _LINK_HINT_RE.search(context):
            continue
        target = urljoin(base_url, href)
        parsed = urlparse(target)
        if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").casefold() != host:
            continue
        normalized = target.split("#", 1)[0]
        if normalized in seen:
            continue
        seen.add(normalized)
        output.append((normalized, label or normalized))
        if len(output) >= 5:
            break
    return output


async def _read_source_url(
    url: str,
    *,
    title: str,
    make: Optional[str],
    calibration_mode: bool,
    follow_same_host_links: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    """Deep-read one source and optionally one level of same-host links.

    Returns findings, read-results, PDF pages inspected, and child links read.
    """
    try:
        final_url, raw, content_type = await research_capture._bounded_public_fetch(url)  # noqa: SLF001
    except Exception:
        try:
            read = await ro.public_read({"url": url})
        except Exception:
            return [], [], 0, 0
        text = str(read.get("page_text") or "")
        findings = _page_findings(
            text,
            page=None,
            url=str(read.get("url") or url),
            title=str(read.get("title") or title),
            make=make,
            calibration_mode=calibration_mode,
        )
        return findings, [read], 0, 0

    is_pdf = content_type == "application/pdf" or raw.startswith(b"%PDF")
    if is_pdf:
        findings: list[dict[str, Any]] = []
        pages_inspected = 0
        read_summary = {
            "url": final_url,
            "title": title,
            "content_type": "application/pdf",
            "pages": 0,
            "deep_read": True,
        }
        try:
            reader = PdfReader(io.BytesIO(raw), strict=False)
            read_summary["pages"] = len(reader.pages)
            for page_number, page in enumerate(reader.pages, 1):
                pages_inspected += 1
                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""
                extraction = "native"
                if not _usable_text(text):
                    ocr_text = _ocr_pdf_page(raw, page_number - 1)
                    if _usable_text(ocr_text):
                        text = ocr_text
                        extraction = "ocr"
                findings.extend(
                    _page_findings(
                        text,
                        page=page_number,
                        url=final_url,
                        title=title,
                        make=make,
                        calibration_mode=calibration_mode,
                        extraction=extraction,
                    )
                )
        except Exception:
            return [], [read_summary], pages_inspected, 0
        return findings, [read_summary], pages_inspected, 0

    try:
        document = raw.decode("utf-8", errors="replace")
        text = research_capture._visible_text(document)  # noqa: SLF001
    except Exception:
        return [], [], 0, 0

    findings = _page_findings(
        text,
        page=None,
        url=final_url,
        title=title,
        make=make,
        calibration_mode=calibration_mode,
    )
    read_results: list[dict[str, Any]] = [{
        "url": final_url,
        "title": title,
        "content_type": content_type or "text/html",
        "page_text": text[:12_000],
        "deep_read": True,
    }]
    links_read = 0
    if follow_same_host_links and calibration_mode:
        for child_url, child_title in _same_host_deep_links(document, final_url):
            child_findings, child_reads, child_pages, _ = await _read_source_url(
                child_url,
                title=child_title,
                make=make,
                calibration_mode=True,
                follow_same_host_links=False,
            )
            findings.extend(child_findings)
            read_results.extend(child_reads)
            links_read += 1
            # Child PDF pages are represented in their read-results/findings;
            # the aggregate page count is intentionally returned to the caller.
            if child_pages:
                read_results.append({
                    "url": child_url,
                    "title": child_title,
                    "deep_child_pdf_pages": child_pages,
                })
    return findings, read_results, 0, links_read


async def _read_deep_source(
    source: dict[str, Any],
    make: Optional[str],
    *,
    calibration_mode: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    url = str(source.get("url") or "").strip()
    title = str(source.get("title") or "").strip()
    if not url:
        return [], [], 0, 0
    return await _read_source_url(
        url,
        title=title,
        make=make,
        calibration_mode=calibration_mode,
        follow_same_host_links=True,
    )


def _focused_queries(
    query: str,
    make: Optional[str],
    *,
    calibration_mode: bool,
    policy_mode: bool,
) -> list[str]:
    output: list[str] = []
    prefix = (make or "").strip()
    if policy_mode:
        output.extend([
            f"{prefix} Collision Pros approved repair methods recycled parts",
            f"{prefix} methods not approved aftermarket recycled parts collision repair",
        ])
        if prefix.casefold() == "toyota":
            output.append(
                "Toyota Lexus approved repair methods installing aftermarket recycled parts Collision Pros"
            )

    if calibration_mode:
        topic = " ".join(query.split())[:240]
        output.extend([
            f"{prefix} {topic} calibration requirements collision repair position statement",
            f"{prefix} {topic} calibration required after collision replacement removal repair",
            f"{prefix} ADAS calibration requirements after any collision position statement",
            f"{prefix} ADAS calibration requirements after all collisions",
            f"{prefix} collision repair calibration precautions service manual technical bulletin",
            f"{prefix} {topic} when calibration is required OEM collision",
        ])
    # Keep order but remove duplicates/empties.
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in output:
        normalized = " ".join(item.split()).strip()
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            cleaned.append(normalized[:400])
    return cleaned


async def deep_search_public_oem(
    query: str,
    make: Optional[str],
    *,
    source_depth: str = "standard",
) -> dict[str, Any]:
    """Run normal OEM discovery plus exhaustive calibration/policy reading."""
    # The previous implementation is the standard bounded discovery path.
    # Passing the nonstandard depth back into it would delegate straight back
    # to this function and recurse forever.
    primary = await _PREVIOUS_SEARCH(query, make, source_depth="standard")
    calibration_mode = source_depth == "calibration_requirements"
    policy_mode = source_depth == "repair_policy"

    merged: dict[str, dict[str, Any]] = {}
    for item in primary.get("sources") or []:
        if isinstance(item, dict) and item.get("url"):
            merged[str(item["url"])] = dict(item)

    for focused in _focused_queries(
        query,
        make,
        calibration_mode=calibration_mode,
        policy_mode=policy_mode,
    ):
        try:
            result = await ro.public_search({"query": focused, "manufacturer": make or ""})
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

    all_findings: list[dict[str, Any]] = []
    read_results = list(primary.get("read_results") or [])
    pdf_pages_inspected = 0
    same_host_links_read = 0
    # Read more sources for calibration questions because a decisive trigger
    # can be buried in a general collision manual rather than the top search hit.
    source_limit = 10 if calibration_mode else 6
    for source in sources[:source_limit]:
        if _policy_score(source, make) < 6 and not calibration_mode:
            continue
        findings, reads, pages, links = await _read_deep_source(
            source,
            make,
            calibration_mode=calibration_mode,
        )
        all_findings.extend(findings)
        read_results.extend(reads)
        pdf_pages_inspected += pages
        same_host_links_read += links

    # Deduplicate findings without discarding a later-page rule.
    unique: list[dict[str, Any]] = []
    seen_findings: set[tuple[str, int | None, str, str]] = set()
    for finding in all_findings:
        key = (
            str(finding.get("url") or ""),
            finding.get("page") if isinstance(finding.get("page"), int) else None,
            str(finding.get("matched_term") or "").casefold(),
            str(finding.get("finding_kind") or ""),
        )
        if key in seen_findings:
            continue
        seen_findings.add(key)
        unique.append(finding)

    policy_findings = [item for item in unique if item.get("finding_kind") == "repair_policy"]
    calibration_findings = [
        item for item in unique if item.get("finding_kind") == "calibration_requirement"
    ]

    result = dict(primary)
    result["sources"] = sources[:12]
    result["result_count"] = len(sources)
    result["read_results"] = read_results[:30]
    result["policy_findings"] = policy_findings[:12]
    result["calibration_findings"] = calibration_findings[:20]
    result["deep_policy_read"] = policy_mode
    result["deep_calibration_read"] = calibration_mode
    result["deep_read_metrics"] = {
        "full_pdf_pages_inspected": pdf_pages_inspected,
        "same_host_links_read": same_host_links_read,
        "source_pages_not_limited_to_first_page": True,
        "ocr_fallback_for_scan_only_pdf_pages": True,
        "targeted_public_search_variants": len(
            _focused_queries(
                query,
                make,
                calibration_mode=calibration_mode,
                policy_mode=policy_mode,
            )
        ),
    }
    return result


_PREVIOUS_SEARCH = research_workflow.search_public_oem


def install() -> None:
    """Keep legacy deep-read helpers available without patching source routing."""

    global _INSTALLED
    with _INSTALL_LOCK:
        _INSTALLED = True
