"""Cooperative ALLDATA ADAS Quick Reference acquisition for Calibration IQ ROs.

This is deliberately not a crawler and not another vehicle-selection agent.
The operator selects the exact vehicle in the already-authenticated ALLDATA
Collision session. X then proves that selected vehicle matches the Calibration IQ
repair order, opens ADAS Quick Reference, enumerates its procedure hyperlinks,
and captures them sequentially into ADAS SI.

Acquisition is conservative:
* one browser/page, one procedure at a time; no parallel requests
* exact CIQ vehicle must be proved from a bounded ALLDATA vehicle UI signal
* only ALLDATA Repair/Collision links from ADAS Quick Reference are eligible
* duplicate prevention is source-first: a known canonical ALLDATA procedure URL
  is skipped without another capture; new URLs are then checked against the
  whole ADAS SI PDF SHA-256 index and a conservative same-vehicle/title fallback
* existing evidence is never overwritten
* every saved PDF is passed through the existing ADAS SI native/OCR page path
  and then searched back out of ADAS SI before it is reported retrievable

The result intentionally recommends the existing Calibration IQ ``research_ro``
operator as the next step. That existing path owns CIQ workspace import, evidence
linking, optimistic concurrency, receipts, and research-completion truth; this
collector does not duplicate or bypass that contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from . import calibration_iq
from . import research_alldata_navigation as nav
from . import research_operator as ro

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False
MAX_QUICK_REFERENCE_LINKS = 40
MIN_INTER_DOCUMENT_DELAY_SECONDS = 1.25
MAX_INTER_DOCUMENT_DELAY_SECONDS = 5.0

_NAV_LABELS = {
    "home", "help", "help & feedback", "bookmarks", "library", "convert",
    "reference - collision", "change", "change vehicle", "select vehicle",
    "vehicle information search", "adas quick reference", "logout", "phone",
    "back", "next", "previous", "print", "save", "close",
}
_ADAS_LINK_RE = re.compile(
    r"\b(?:adas|camera|radar|blind\s*spot|\bbsm\b|\bbsd\b|lane|cruise|"
    r"parking|park\s*assist|ultrasonic|sonar|occupant|steering|sensor|"
    r"calibrat\w*|recalibrat\w*|align\w*|aim\w*|adjust\w*|initializ\w*|"
    r"relearn\w*|reset\w*|setup|program\w*|configur\w*|monitor|"
    r"forward\s+collision|pre[-\s]?collision|driver\s+assist)\b",
    re.IGNORECASE,
)
_ARTICLE_RE = re.compile(r"(?:^|/)(?:article|guid)/([^/?#]+)", re.IGNORECASE)
_SELECTED_VEHICLE_SELECTORS = (
    "[data-testid*='vehicle' i]",
    "[data-test*='vehicle' i]",
    "[aria-label*='vehicle' i]",
    "[id*='vehicle' i]",
    "[class*='vehicle' i]",
)


def _safe_filename(value: object, fallback: str = "ADAS procedure") -> str:
    try:
        return ro._safe_filename(str(value or ""), fallback)  # noqa: SLF001
    except TypeError:
        cleaned = re.sub(r"[^A-Za-z0-9._()\- ]+", " ", str(value or ""))
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
        return cleaned[:150] or fallback


def _canonical_alldata_url(raw: object) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if not ro._is_alldata_url(value):  # noqa: SLF001
        return ""
    host = parsed.hostname.casefold()
    if parsed.port:
        host = f"{host}:{parsed.port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    fragment = parsed.fragment.rstrip("/")
    # Query strings are ignored for duplicate identity; the SPA hash route is
    # retained because article/guid identity lives there.
    return urlunsplit((parsed.scheme.casefold(), host, path, "", fragment))


def _article_id(url: object) -> Optional[str]:
    canonical = _canonical_alldata_url(url)
    if not canonical:
        return None
    parsed = urlsplit(canonical)
    route = f"{parsed.path}/{parsed.fragment}"
    match = _ARTICLE_RE.search(route)
    if not match:
        return None
    value = re.sub(r"[^A-Za-z0-9_-]", "", match.group(1))
    return value[:80] or None


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plain_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _procedure_title_key(title: object, vehicle_label: object = "") -> str:
    text = str(title or "")
    vehicle = str(vehicle_label or "")
    if vehicle:
        for token in sorted(vehicle.split(), key=len, reverse=True):
            if len(token) >= 2:
                text = re.sub(
                    rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])",
                    " ",
                    text,
                    flags=re.IGNORECASE,
                )
    text = re.sub(r"\bALLDATA(?:\s+Collision)?\b", " ", text, flags=re.IGNORECASE)
    return _plain_key(text)


def _pdf_for_sidecar(sidecar: Path) -> Path:
    name = sidecar.name
    if name.endswith(".source.json"):
        return sidecar.with_name(name[: -len(".source.json")] + ".pdf")
    return sidecar.with_suffix(".pdf")


def _load_dedupe_index(source_root: Path, vehicle_label: str) -> dict[str, Any]:
    """Index existing ADAS SI evidence without trusting filenames alone."""
    urls: dict[str, dict[str, Any]] = {}
    hashes: dict[str, dict[str, Any]] = {}
    title_keys: dict[str, dict[str, Any]] = {}

    for sidecar in source_root.rglob("*.source.json"):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        canonical = _canonical_alldata_url(
            data.get("canonical_source_url")
            or data.get("source_url")
            or data.get("url")
        )
        pdf_path = _pdf_for_sidecar(sidecar)
        record = {
            "sidecar": sidecar,
            "pdf": pdf_path if pdf_path.is_file() else None,
            "data": data,
        }
        if canonical:
            urls[canonical] = record
        digest = str(
            data.get("saved_pdf_sha256")
            or data.get("pdf_sha256")
            or data.get("sha256")
            or ""
        ).casefold()
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            hashes[digest] = record
        title_key = _procedure_title_key(data.get("title"), data.get("vehicle") or vehicle_label)
        vehicle_key = _plain_key(data.get("vehicle") or "")
        if title_key and vehicle_key and vehicle_key == _plain_key(vehicle_label):
            title_keys[title_key] = record

    # Older ADAS SI PDFs may predate provenance sidecars/hashes. Hash the whole
    # library once per run so byte-identical content cannot be added again.
    for pdf_path in source_root.rglob("*.pdf"):
        try:
            digest = _sha256_file(pdf_path)
        except OSError:
            continue
        hashes.setdefault(digest, {"sidecar": None, "pdf": pdf_path, "data": {}})

    # Conservative same-vehicle/title fallback for manually seeded PDFs that
    # have no provenance sidecar. Ambiguous same-title content is skipped for
    # review instead of automatically creating another file.
    vehicle = nav.vehicle_from_query(vehicle_label)
    requested_year = str(vehicle.get("year") or "")
    requested_make = str(vehicle.get("make") or "").casefold()
    requested_model = next(
        (token.casefold() for token in str(vehicle.get("model_trim") or "").split() if len(token) >= 3),
        "",
    )
    try:
        from . import adas_si as adas_mod
        for descriptor in adas_mod.SourceInventory(source_root).documents():
            if not isinstance(descriptor, dict):
                continue
            year = str(descriptor.get("year") or "")
            make = str(descriptor.get("make") or "").casefold()
            model = str(descriptor.get("model") or "").casefold()
            if requested_year and year and year != requested_year:
                continue
            if requested_make and make and make != requested_make:
                continue
            if requested_model and model and requested_model not in model and model not in requested_model:
                continue
            key = _procedure_title_key(descriptor.get("title"), vehicle_label)
            path = descriptor.get("_path")
            if key and isinstance(path, Path):
                title_keys.setdefault(key, {"sidecar": None, "pdf": path, "data": {}})
    except Exception:
        pass

    return {"urls": urls, "hashes": hashes, "title_keys": title_keys}


def _record_path(record: Optional[dict[str, Any]], source_root: Path) -> Optional[str]:
    if not isinstance(record, dict):
        return None
    pdf = record.get("pdf")
    if not isinstance(pdf, Path):
        return None
    try:
        return str(pdf.relative_to(source_root)).replace("\\", "/")
    except ValueError:
        return str(pdf)


async def _selected_vehicle_signal(page: Any, vehicle: dict[str, Any]) -> dict[str, Any]:
    """Return one bounded selected-vehicle proof; never scan the whole page body."""
    expected = str(vehicle.get("label") or "").strip()
    current = await nav._current_vehicle_label(page)  # noqa: SLF001
    if await nav._confirms_identity(current, vehicle):  # noqa: SLF001
        return {"verified": True, "label": current, "source": "navigation_vehicle_signal"}

    identity_tokens = [
        str(vehicle.get(key) or "").casefold()
        for key in ("year", "make")
        if vehicle.get(key)
    ]
    model_token = next(
        (token.casefold() for token in str(vehicle.get("model_trim") or "").split() if len(token) >= 3),
        "",
    )
    if not identity_tokens:
        return {"verified": False, "label": current, "source": None}

    frames = [page, *list(getattr(page, "frames", []) or [])]
    seen_frames: set[int] = set()
    for frame in frames:
        if id(frame) in seen_frames:
            continue
        seen_frames.add(id(frame))
        for selector in _SELECTED_VEHICLE_SELECTORS:
            try:
                locator = frame.locator(selector)
                count = min(await locator.count(), 80)
            except Exception:
                continue
            for index in range(count):
                item = locator.nth(index)
                try:
                    if not await item.is_visible(timeout=100):
                        continue
                    text = " ".join((await item.inner_text(timeout=500)).split()).strip()
                except Exception:
                    continue
                if not 4 <= len(text) <= 420:
                    continue
                folded = text.casefold()
                if "select vehicle" in folded or folded.count("202") >= 4:
                    continue
                if all(token in folded for token in identity_tokens) and (
                    not model_token or model_token in folded
                ):
                    return {
                        "verified": True,
                        "label": text,
                        "source": f"bounded:{selector}",
                    }
    return {
        "verified": False,
        "label": current,
        "source": None,
        "reason": (
            f"ALLDATA does not currently prove the selected vehicle is {expected}. "
            "Select that exact vehicle before collection."
        ),
    }


async def _open_quick_reference(page: Any) -> dict[str, Any]:
    pattern = re.compile(r"\bADAS\s+Quick\s+Reference\b", re.IGNORECASE)
    frames = [page, *list(getattr(page, "frames", []) or [])]
    for frame in frames:
        try:
            marker = frame.get_by_text(pattern, exact=False).first
            if not await marker.is_visible(timeout=250):
                continue
            label = " ".join((await marker.inner_text(timeout=600)).split()).strip()
            try:
                clickable = marker.locator("xpath=ancestor-or-self::a[1] | ancestor-or-self::button[1]")
                if await clickable.count():
                    await clickable.first.click(timeout=5_000)
                else:
                    await marker.click(timeout=5_000)
            except Exception:
                try:
                    await marker.click(timeout=5_000)
                except Exception:
                    return {
                        "opened": True,
                        "already_open": True,
                        "label": label or "ADAS Quick Reference",
                        "url": str(page.url),
                    }
            await asyncio.sleep(0.8)
            return {
                "opened": True,
                "already_open": False,
                "label": label or "ADAS Quick Reference",
                "url": str(page.url),
            }
        except Exception:
            continue
    return {
        "opened": False,
        "reason": "ADAS Quick Reference was not found on the selected ALLDATA vehicle page.",
        "url": str(page.url),
    }


def _link_score(title: str, url: str) -> int:
    folded_title = title.casefold().strip()
    if not folded_title or folded_title in _NAV_LABELS:
        return 0
    canonical = _canonical_alldata_url(url)
    if not canonical:
        return 0
    parsed = urlsplit(canonical)
    route = f"{parsed.path}#{parsed.fragment}".casefold()
    if "/repair" not in parsed.path.casefold():
        return 0
    score = 0
    if any(marker in route for marker in ("/article/", "/guid/", "component/", "itype/", "nonstandard/")):
        score += 3
    if _ADAS_LINK_RE.search(title):
        score += 3
    if len(title) >= 12:
        score += 1
    return score


async def _enumerate_quick_reference_links(page: Any, limit: int) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    frames = [page, *list(getattr(page, "frames", []) or [])]
    seen_frames: set[int] = set()
    for frame in frames:
        if id(frame) in seen_frames:
            continue
        seen_frames.add(id(frame))
        for selector in ("main a[href], [role='main'] a[href], article a[href]", "a[href]"):
            try:
                links = frame.locator(selector)
                count = min(await links.count(), 500)
            except Exception:
                continue
            for index in range(count):
                item = links.nth(index)
                try:
                    if not await item.is_visible(timeout=80):
                        continue
                    title = " ".join((await item.inner_text(timeout=400)).split()).strip()
                    href = str(await item.get_attribute("href") or "").strip()
                except Exception:
                    continue
                if not title or not href:
                    continue
                absolute = urljoin(str(page.url), href)
                canonical = _canonical_alldata_url(absolute)
                if not canonical or canonical == _canonical_alldata_url(page.url):
                    continue
                score = _link_score(title, canonical)
                if score < 3:
                    continue
                current = found.get(canonical)
                if current is None or score > int(current.get("score") or 0):
                    found[canonical] = {
                        "title": title[:300],
                        "url": canonical,
                        "score": score,
                        "article_id": _article_id(canonical),
                    }
            if found:
                break
    ordered = sorted(
        found.values(),
        key=lambda item: (-int(item.get("score") or 0), str(item.get("title") or "").casefold()),
    )
    return ordered[: max(1, min(int(limit or MAX_QUICK_REFERENCE_LINKS), MAX_QUICK_REFERENCE_LINKS))]


def _capture_folder(source_root: Path, vehicle: dict[str, Any]) -> Path:
    make = _safe_filename(vehicle.get("make") or "Unknown Make", "Unknown Make")
    vehicle_name = _safe_filename(
        vehicle.get("label")
        or " ".join(
            str(vehicle.get(key) or "")
            for key in ("year", "make", "model_trim")
            if vehicle.get(key)
        ),
        "Vehicle",
    )
    return source_root / "Acquired" / "ALLDATA" / make / vehicle_name / "ADAS Quick Reference"


def _save_manifest(folder: Path, payload: dict[str, Any]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "quick-reference-manifest.json"
    temp = folder / ".quick-reference-manifest.json.tmp"
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    temp.replace(target)


async def _capture_one(
    *,
    page: Any,
    adas: Any,
    vehicle: dict[str, Any],
    link: dict[str, Any],
    quick_reference_url: str,
    dedupe: dict[str, Any],
) -> dict[str, Any]:
    target_url = _canonical_alldata_url(link.get("url"))
    if not target_url:
        return {
            "status": "failed",
            "title": link.get("title"),
            "url": str(link.get("url") or ""),
            "reason": "Quick Reference supplied an invalid or non-ALLDATA procedure URL.",
        }

    # Canonical source identity is the strongest duplicate proof and avoids even
    # re-rendering a known procedure. This is intentionally source-first.
    known_source = dedupe["urls"].get(target_url)
    if known_source is not None:
        return {
            "status": "duplicate_skipped",
            "duplicate_reason": "canonical_source_url_already_present",
            "title": link.get("title"),
            "url": target_url,
            "existing_relative_path": _record_path(known_source, Path(adas.source_root)),
        }

    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
        await asyncio.sleep(0.65)
    except Exception as exc:
        return {
            "status": "failed",
            "title": link.get("title"),
            "url": target_url,
            "reason": f"ALLDATA procedure navigation failed: {type(exc).__name__}.",
        }

    vehicle_proof = await _selected_vehicle_signal(page, vehicle)
    if not vehicle_proof.get("verified"):
        return {
            "status": "failed",
            "title": link.get("title"),
            "url": str(page.url),
            "reason": vehicle_proof.get("reason") or "Vehicle identity was lost before capture.",
            "vehicle_proof": vehicle_proof,
        }

    canonical_url = _canonical_alldata_url(page.url) or target_url
    known_source = dedupe["urls"].get(canonical_url)
    if known_source is not None:
        return {
            "status": "duplicate_skipped",
            "duplicate_reason": "canonical_source_url_already_present",
            "title": link.get("title"),
            "url": canonical_url,
            "existing_relative_path": _record_path(known_source, Path(adas.source_root)),
        }

    try:
        body_text = str(await page.locator("body").inner_text(timeout=8_000) or "")
    except Exception:
        body_text = ""
    if len(re.sub(r"\W", "", body_text)) < 80:
        return {
            "status": "failed",
            "title": link.get("title"),
            "url": canonical_url,
            "reason": "The ALLDATA procedure page did not contain enough readable content to preserve.",
        }

    try:
        title = " ".join((await page.title()).split()).strip() or str(link.get("title") or "ADAS procedure")
    except Exception:
        title = str(link.get("title") or "ADAS procedure")
    source_title = str(link.get("title") or title).strip() or title
    title_key = _procedure_title_key(source_title, vehicle.get("label") or "")
    same_title = dedupe["title_keys"].get(title_key) if title_key else None
    if same_title is not None:
        return {
            "status": "possible_duplicate_skipped",
            "duplicate_reason": "same_vehicle_procedure_title_already_present",
            "title": source_title,
            "url": canonical_url,
            "existing_relative_path": _record_path(same_title, Path(adas.source_root)),
        }

    try:
        pdf_bytes = await page.pdf(format="Letter", print_background=True, prefer_css_page_size=True)
    except Exception as exc:
        return {
            "status": "failed",
            "title": source_title,
            "url": canonical_url,
            "reason": f"ALLDATA print capture failed: {type(exc).__name__}.",
        }
    if not pdf_bytes.startswith(b"%PDF") or len(pdf_bytes) < 1000:
        return {
            "status": "failed",
            "title": source_title,
            "url": canonical_url,
            "reason": "ALLDATA did not produce a valid non-empty PDF snapshot.",
        }

    digest = _sha256_bytes(pdf_bytes)
    same_hash = dedupe["hashes"].get(digest)
    if same_hash is not None:
        return {
            "status": "duplicate_skipped",
            "duplicate_reason": "identical_pdf_sha256",
            "title": source_title,
            "url": canonical_url,
            "sha256": digest,
            "existing_relative_path": _record_path(same_hash, Path(adas.source_root)),
        }

    folder = _capture_folder(Path(adas.source_root), vehicle)
    folder.mkdir(parents=True, exist_ok=True)
    article = _article_id(canonical_url)
    base = _safe_filename(
        f"{vehicle.get('label') or 'Vehicle'} {source_title}"
        + (f" article-{article}" if article else f" {digest[:10]}")
    )
    pdf_path = folder / f"{base}.pdf"
    sidecar_path = folder / f"{base}.source.json"
    if pdf_path.exists() or sidecar_path.exists():
        # Never overwrite. A filename collision without a URL/hash match is a
        # review case, not permission to create a numbered duplicate.
        return {
            "status": "possible_duplicate_skipped",
            "duplicate_reason": "destination_filename_already_exists",
            "title": source_title,
            "url": canonical_url,
            "existing_relative_path": str(pdf_path.relative_to(adas.source_root)).replace("\\", "/") if pdf_path.exists() else None,
        }

    pdf_path.write_bytes(pdf_bytes)
    provenance = {
        "provider": ro.PROVIDER_LABEL,
        "source_type": "alldata_adas_quick_reference_procedure",
        "source_url": canonical_url,
        "canonical_source_url": canonical_url,
        "alldata_article_id": article,
        "quick_reference_url": _canonical_alldata_url(quick_reference_url),
        "retrieved_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "vehicle": vehicle.get("label"),
        "title": source_title,
        "page_title": title,
        "saved_pdf_sha256": digest,
        "artifact_kind": "licensed_print_snapshot_pdf",
        "authoritative_artifact": False,
        "source_url_is_authority": True,
        "licensed_access": True,
        "targeted_research": True,
        "selected_vehicle_verified": True,
        "vehicle_proof": vehicle_proof,
        "credential_secret_stored_in_document": False,
    }
    sidecar_path.write_text(json.dumps(provenance, indent=2, ensure_ascii=False), encoding="utf-8")

    try:
        adas.inventory._cache = None  # noqa: SLF001
        pages = adas._pages(pdf_path)  # noqa: SLF001
        readable_pages = sum(1 for _number, text in pages if str(text or "").strip())
    except Exception as exc:
        pdf_path.unlink(missing_ok=True)
        sidecar_path.unlink(missing_ok=True)
        adas.inventory._cache = None  # noqa: SLF001
        return {
            "status": "failed",
            "title": source_title,
            "url": canonical_url,
            "reason": f"ADAS SI could not ingest the captured procedure: {type(exc).__name__}.",
        }
    if readable_pages <= 0:
        pdf_path.unlink(missing_ok=True)
        sidecar_path.unlink(missing_ok=True)
        adas.inventory._cache = None  # noqa: SLF001
        return {
            "status": "failed",
            "title": source_title,
            "url": canonical_url,
            "reason": "The captured PDF was unreadable after native/OCR extraction and was not retained.",
        }

    relative = adas.relative_of(pdf_path)
    retrieval_query = f"{vehicle.get('label') or ''} {source_title}".strip()
    try:
        retrieval = adas.search({"query": retrieval_query})
    except Exception:
        retrieval = {}
    retrieval_paths = {
        str(item.get("relative_path") or "")
        for item in [
            *(retrieval.get("results") or []),
            *(retrieval.get("matched_documents") or []),
        ]
        if isinstance(item, dict)
    }
    retrieval_verified = relative in retrieval_paths

    record = {"sidecar": sidecar_path, "pdf": pdf_path, "data": provenance}
    dedupe["urls"][canonical_url] = record
    dedupe["hashes"][digest] = record
    if title_key:
        dedupe["title_keys"][title_key] = record

    return {
        "status": "captured",
        "title": source_title,
        "url": canonical_url,
        "article_id": article,
        "relative_path": relative,
        "source_sidecar": adas.relative_of(sidecar_path),
        "sha256": digest,
        "pages": len(pages),
        "readable_pages": readable_pages,
        "retrieval_verified": retrieval_verified,
        "retrieval_query": retrieval_query,
    }


async def collect_for_calibration_iq_ro(settings: Any, adas: Any, args: dict[str, Any]) -> dict[str, Any]:
    ro_identifier = str(args.get("repair_order_id") or "").strip()
    if not ro_identifier:
        return {
            "status": "invalid_input",
            "success": False,
            "verified": False,
            "message": "repair_order_id is required. Use the Calibration IQ RO id or exact displayed RO number.",
        }

    ro_result = await calibration_iq.get_repair_order(settings, {"repair_order_id": ro_identifier})
    if ro_result.get("status") != "verified":
        return {
            "status": "ciq_unavailable",
            "success": False,
            "verified": False,
            "repair_order_id": ro_identifier,
            "message": ro_result.get("message") or "Calibration IQ did not return a verified repair order.",
            "calibration_iq": ro_result,
        }
    snapshot = ro_result.get("raw") if isinstance(ro_result.get("raw"), dict) else {}
    vehicle_label = calibration_iq._research_vehicle_label(snapshot)  # noqa: SLF001
    if not vehicle_label:
        vehicle_label = calibration_iq._research_vehicle_label(ro_result.get("repair_order") or {})  # noqa: SLF001
    vehicle = nav.vehicle_from_query(vehicle_label)
    if not vehicle.get("year") or not vehicle.get("make") or not vehicle.get("model_trim"):
        return {
            "status": "vehicle_identity_missing",
            "success": False,
            "verified": False,
            "repair_order_id": ro_identifier,
            "vehicle": vehicle_label or None,
            "message": "Calibration IQ did not provide enough year/make/model identity to collect ALLDATA SI safely.",
        }

    browser = ro.get_browser(Path(settings.root), adas=adas)
    state = await browser.start(auto_login=False)
    if not state.get("authenticated"):
        return {
            "status": "human_action_required",
            "success": False,
            "verified": False,
            "repair_order_id": ro_identifier,
            "vehicle": vehicle_label,
            "message": "Open the ALLDATA session and sign in, then select this Calibration IQ vehicle.",
            "provider_status": state,
        }
    page = browser._page  # noqa: SLF001
    if page is None:
        return {
            "status": "browser_unavailable",
            "success": False,
            "verified": False,
            "repair_order_id": ro_identifier,
            "vehicle": vehicle_label,
            "message": "The authenticated ALLDATA browser page is not active.",
        }

    vehicle_proof = await _selected_vehicle_signal(page, vehicle)
    if not vehicle_proof.get("verified"):
        return {
            "status": "vehicle_selection_required",
            "success": False,
            "verified": False,
            "repair_order_id": ro_identifier,
            "vehicle": vehicle_label,
            "message": vehicle_proof.get("reason") or f"Select {vehicle_label} in ALLDATA before collection.",
            "vehicle_proof": vehicle_proof,
            "provider_status": state,
        }

    quick_ref = await _open_quick_reference(page)
    if not quick_ref.get("opened"):
        return {
            "status": "quick_reference_not_found",
            "success": False,
            "verified": False,
            "repair_order_id": ro_identifier,
            "vehicle": vehicle_label,
            "message": quick_ref.get("reason"),
            "quick_reference": quick_ref,
        }
    quick_reference_url = str(page.url)

    post_navigation_vehicle = await _selected_vehicle_signal(page, vehicle)
    if not post_navigation_vehicle.get("verified"):
        return {
            "status": "vehicle_context_lost",
            "success": False,
            "verified": False,
            "repair_order_id": ro_identifier,
            "vehicle": vehicle_label,
            "message": "ALLDATA lost the selected CIQ vehicle while opening ADAS Quick Reference. Nothing was captured.",
            "vehicle_proof": post_navigation_vehicle,
        }

    max_documents = max(1, min(int(args.get("max_documents") or MAX_QUICK_REFERENCE_LINKS), MAX_QUICK_REFERENCE_LINKS))
    links = await _enumerate_quick_reference_links(page, max_documents)
    if not links:
        return {
            "status": "no_quick_reference_links",
            "success": False,
            "verified": False,
            "repair_order_id": ro_identifier,
            "vehicle": vehicle_label,
            "quick_reference_url": _canonical_alldata_url(quick_reference_url),
            "message": "ADAS Quick Reference opened, but no eligible ALLDATA procedure hyperlinks were found.",
        }

    delay_seconds = float(args.get("delay_seconds") or MIN_INTER_DOCUMENT_DELAY_SECONDS)
    delay_seconds = max(MIN_INTER_DOCUMENT_DELAY_SECONDS, min(delay_seconds, MAX_INTER_DOCUMENT_DELAY_SECONDS))
    dedupe = _load_dedupe_index(Path(adas.source_root), vehicle_label)
    results: list[dict[str, Any]] = []
    for index, link in enumerate(links):
        if index:
            await asyncio.sleep(delay_seconds)
        outcome = await _capture_one(
            page=page,
            adas=adas,
            vehicle=vehicle,
            link=link,
            quick_reference_url=quick_reference_url,
            dedupe=dedupe,
        )
        results.append(outcome)
        if outcome.get("status") == "failed" and "vehicle" in str(outcome.get("reason") or "").casefold():
            break

    try:
        if _canonical_alldata_url(page.url) != _canonical_alldata_url(quick_reference_url):
            await page.goto(quick_reference_url, wait_until="domcontentloaded", timeout=45_000)
    except Exception:
        pass

    captured = [item for item in results if item.get("status") == "captured"]
    exact_duplicates = [item for item in results if item.get("status") == "duplicate_skipped"]
    possible_duplicates = [item for item in results if item.get("status") == "possible_duplicate_skipped"]
    failures = [item for item in results if item.get("status") == "failed"]
    retrieval_failures = [item for item in captured if item.get("retrieval_verified") is not True]
    all_links_accounted_for = len(results) == len(links)
    success = bool(links and all_links_accounted_for and not failures and not retrieval_failures)
    status = "success" if success else ("partial_success" if results else "failed")

    try:
        requirements = calibration_iq._research_calibrations(snapshot, {})  # noqa: SLF001
    except Exception:
        requirements = []
    requirement_summary = [
        {"id": item.get("id") or None, "label": item.get("label")}
        for item in requirements
        if isinstance(item, dict)
    ]

    manifest = {
        "schema_version": 1,
        "repair_order_id": ro_identifier,
        "vehicle": vehicle_label,
        "quick_reference_url": _canonical_alldata_url(quick_reference_url),
        "collected_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "links_found": links,
        "results": results,
        "required_calibrations": requirement_summary,
        "summary": {
            "procedure_links": len(links),
            "captured": len(captured),
            "exact_duplicates_skipped": len(exact_duplicates),
            "possible_duplicates_skipped": len(possible_duplicates),
            "failures": len(failures),
            "retrieval_failures": len(retrieval_failures),
        },
    }
    try:
        _save_manifest(_capture_folder(Path(adas.source_root), vehicle), manifest)
    except OSError:
        pass

    return {
        "status": status,
        "action": "collect_alldata_quick_reference",
        "executed": True,
        "success": success,
        "verified": success,
        "partial": not success and bool(results),
        "repair_order_id": ro_identifier,
        "vehicle": vehicle_label,
        "vehicle_proof": vehicle_proof,
        "quick_reference_url": _canonical_alldata_url(quick_reference_url),
        "procedure_links_found": len(links),
        "captured_count": len(captured),
        "exact_duplicates_skipped": len(exact_duplicates),
        "possible_duplicates_skipped": len(possible_duplicates),
        "failure_count": len(failures),
        "retrieval_failure_count": len(retrieval_failures),
        "captured": captured,
        "duplicates": [*exact_duplicates, *possible_duplicates],
        "failures": failures,
        "required_calibrations": requirement_summary,
        "ciq_research_ro_ready": bool(captured or exact_duplicates or possible_duplicates),
        "next_action": {
            "tool": "calibration_iq_operator",
            "repair_order_id": ro_identifier,
            "operation": "research_ro",
            "arguments": {"complete_research": False},
            "reason": (
                "Re-run the existing Calibration IQ research_ro operator now so it searches the "
                "updated ADAS SI library, imports/links matching OEM evidence into this RO workspace, "
                "and reports any documentation still missing."
            ),
        },
        "message": (
            f"ALLDATA ADAS Quick Reference accounted for all {len(links)} procedure link(s): "
            f"{len(captured)} new capture(s) and "
            f"{len(exact_duplicates) + len(possible_duplicates)} duplicate(s) skipped. "
            "Run Calibration IQ research_ro next to link the updated ADAS SI evidence to this RO."
            if success
            else (
                f"ALLDATA ADAS Quick Reference processed {len(results)} of {len(links)} link(s), "
                f"captured {len(captured)}, skipped {len(exact_duplicates) + len(possible_duplicates)} "
                f"duplicate(s), and had {len(failures)} capture failure(s) / "
                f"{len(retrieval_failures)} ADAS SI retrieval verification failure(s). Review before "
                "treating this RO as SI-ready."
            )
        ),
    }


def install() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        from ..tools import registry as registry_mod

        schema = registry_mod.TOOL_SCHEMAS.get("collision_research")
        if isinstance(schema, dict):
            parameters = schema.setdefault("parameters", {})
            properties = parameters.setdefault("properties", {})
            action = properties.setdefault("action", {"type": "string", "enum": []})
            enum = action.setdefault("enum", [])
            if isinstance(enum, list) and "collect_alldata_quick_reference" not in enum:
                enum.append("collect_alldata_quick_reference")
            properties.setdefault(
                "repair_order_id",
                {
                    "type": "string",
                    "description": (
                        "Calibration IQ repair-order UUID or exact displayed RO number. For "
                        "collect_alldata_quick_reference, X reads the CIQ vehicle from this RO and "
                        "requires the operator to have selected that exact vehicle in ALLDATA first."
                    ),
                },
            )
            properties.setdefault(
                "max_documents",
                {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_QUICK_REFERENCE_LINKS,
                    "description": "Optional safety cap; default 40. Collection is sequential, never parallel.",
                },
            )
            properties.setdefault(
                "delay_seconds",
                {
                    "type": "number",
                    "minimum": MIN_INTER_DOCUMENT_DELAY_SECONDS,
                    "maximum": MAX_INTER_DOCUMENT_DELAY_SECONDS,
                    "description": "Optional delay between procedure pages. Values below 1.25 seconds are clamped upward.",
                },
            )
            description = str(schema.get("description") or "")
            addition = (
                " Use collect_alldata_quick_reference only when the operator has manually selected "
                "the Calibration IQ vehicle in ALLDATA. It opens that vehicle's ADAS Quick Reference, "
                "captures its procedure links one at a time into ADAS SI with source URL/hash/title "
                "dedupe, and proves new PDFs are searchable. After it returns, call "
                "calibration_iq_operator research_ro for the same RO to import/link the updated evidence."
            )
            if "collect_alldata_quick_reference" not in description:
                schema["description"] = description + addition

        previous_init = registry_mod.Registry.__init__
        if not getattr(previous_init, "_xomni_alldata_quick_reference", False):
            def registry_init(self, *args, **kwargs):
                previous_init(self, *args, **kwargs)
                prior = self._handlers.get("collision_research")  # noqa: SLF001

                async def handler(tool_args: dict[str, Any]):
                    if str(tool_args.get("action") or "").casefold() != "collect_alldata_quick_reference":
                        if prior is None:
                            raise ValueError("Collision research operator is unavailable.")
                        result = prior(tool_args)
                        return await result if hasattr(result, "__await__") else result
                    from ..config import Settings
                    from . import adas_si as adas_si_mod

                    settings = Settings.load()
                    adas = adas_si_mod.AdasSI(
                        settings.adas_si_root,
                        settings.root / "data" / "capabilities" / "adas_si" / "index.sqlite",
                    )
                    return await collect_for_calibration_iq_ro(settings, adas, tool_args)

                self.register("collision_research", handler)

            registry_init._xomni_alldata_quick_reference = True  # type: ignore[attr-defined]
            registry_mod.Registry.__init__ = registry_init

        _INSTALLED = True
