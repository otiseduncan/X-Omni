"""Preserve authoritative public post-collision research into ADAS SI.

A manufacturer collision page or public OEM PDF is useful only if X can retain
it, read it again later, and trace the evidence back to its source. This module
adds ``public_capture`` to the collision research operator without weakening the
licensed ALLDATA browser boundary.

Public PDFs are preserved byte-for-byte. Public HTML pages are fetched through
a bounded SSRF-safe client, reduced to a source-visible printable snapshot PDF,
and stored with a provenance sidecar. The source URL remains the authority; the
PDF snapshot is explicitly identified as a captured derivative rather than an
OEM-original PDF.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from . import adas_storage
from . import research_operator as ro

MAX_CAPTURE_BYTES = 32 * 1024 * 1024
MAX_HTML_CHARS = 2_000_000
_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def _safe_filename(value: str, fallback: str = "OEM research") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._() -]+", " ", str(value or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned[:150] or fallback).strip()


def _html_title(document: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", document, flags=re.I | re.S)
    if not match:
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", match.group(1))).split())[:300]


def _visible_text(document: str) -> str:
    text = re.sub(
        r"<(script|style|noscript|template)[^>]*>.*?</\1>",
        " ",
        document,
        flags=re.I | re.S,
    )
    text = re.sub(r"<(br|p|div|li|tr|h[1-6])\b[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    lines = []
    for raw in html.unescape(text).splitlines():
        line = " ".join(raw.split()).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)[:MAX_HTML_CHARS]


async def _bounded_public_fetch(url: str) -> tuple[str, bytes, str]:
    """Fetch one public source, validating every redirect hop against SSRF."""
    current = str(url or "").strip()
    if len(current) > ro.MAX_URL_CHARS:
        raise ValueError("url is too long")
    ro._public_host(current)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; X-Omni/1.0; post-collision-research)",
        "Accept-Language": "en-US,en;q=0.8",
    }
    async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers=headers) as client:
        for _hop in range(5):
            async with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = response.headers.get("location", "")
                    if not target:
                        raise ValueError("Public source redirected without a target.")
                    next_url = urljoin(current, target)
                    ro._public_host(next_url)
                    current = next_url
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_CAPTURE_BYTES:
                        raise ValueError("Public source exceeds the capture byte limit.")
                return current, bytes(body), content_type
    raise ValueError("Public source exceeded the redirect limit.")


async def _html_to_pdf(root: Path, title: str, source_url: str, visible_text: str) -> bytes:
    """Render a text-faithful derivative PDF without executing source scripts."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise ro.BrowserUnavailable("Playwright is required to create a searchable OEM web snapshot.") from exc

    heading = html.escape(title or "Public OEM research source")
    source = html.escape(source_url)
    escaped = html.escape(visible_text)
    body = escaped.replace("\n", "<br>")
    safe_html = f"""<!doctype html><html><head><meta charset='utf-8'><style>
body{{font-family:Arial,sans-serif;color:#111;margin:34px;font-size:10.5pt;line-height:1.35}}
h1{{font-size:16pt}}.source{{font-size:8.5pt;color:#555;word-break:break-all;margin-bottom:22px}}
.content{{white-space:normal}}
</style></head><body><h1>{heading}</h1><div class='source'>Captured from: {source}</div><div class='content'>{body}</div></body></html>"""
    async with async_playwright() as runner:
        browser = None
        try:
            try:
                browser = await runner.chromium.launch(channel="chrome", headless=True)
            except Exception:  # noqa: BLE001 - bundled Chromium is supported fallback
                browser = await runner.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(safe_html, wait_until="load")
            return await page.pdf(format="Letter", print_background=True)
        finally:
            if browser is not None:
                await browser.close()


async def public_capture(args: dict[str, Any], adas: Any) -> dict[str, Any]:
    if adas is None or not adas.available():
        raise ValueError("ADAS SI is unavailable; cannot preserve the research source.")
    url = str(args.get("url") or "").strip()
    if not url:
        raise ValueError("url is required")
    manufacturer = _safe_filename(args.get("manufacturer") or "OEM", "OEM")
    vehicle_label = _safe_filename(args.get("vehicle") or "", "")
    vehicle_identity = adas_storage.normalize_vehicle_identity(
        {
            "year": args.get("vehicle_year"),
            "make": args.get("vehicle_make"),
            "model": args.get("vehicle_model"),
        }
    )
    if vehicle_identity is None:
        # Preserve only vehicle-specific SI. The public_capture action shares
        # the collision_research schema, so callers can always provide the
        # explicit structured vehicle fields when the display label is not
        # sufficient to distinguish model from trim.
        from . import research_alldata_navigation as nav

        parsed = nav.vehicle_from_query(vehicle_label)
        vehicle_identity = adas_storage.normalize_vehicle_identity(
            {
                "year": parsed.get("year"),
                "make": parsed.get("make"),
                "model": parsed.get("model_trim"),
            }
        )
    if vehicle_identity is None:
        raise ValueError(
            "Saving public OEM material into ADAS SI requires exact year, make, and model."
        )
    topic_hint = _safe_filename(args.get("topic") or "Research", "Research")

    final_url, raw, content_type = await _bounded_public_fetch(url)
    retrieved_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    source_sha256 = hashlib.sha256(raw).hexdigest()
    is_pdf = content_type == "application/pdf" or raw.startswith(b"%PDF")

    if is_pdf:
        title = topic_hint
        pdf_bytes = raw
        artifact_kind = "original_public_pdf"
        authoritative_artifact = True
        text_preview = ""
    else:
        charset = "utf-8"
        try:
            document = raw.decode(charset, errors="replace")
        except Exception:  # noqa: BLE001
            document = raw.decode("utf-8", errors="replace")
        title = _safe_filename(_html_title(document) or topic_hint, topic_hint)
        text_preview = _visible_text(document)
        if len(re.sub(r"\W", "", text_preview)) < 20:
            raise ValueError("Public page did not contain enough readable text to preserve.")
        pdf_bytes = await _html_to_pdf(Path(adas.source_root), title, final_url, text_preview)
        artifact_kind = "rendered_text_snapshot_pdf"
        authoritative_artifact = False

    folder = adas_storage.service_information_directory(
        Path(adas.source_root),
        vehicle_identity,
        "Public OEM",
        manufacturer,
    )
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    vehicle = " ".join(
        str(vehicle_identity[key]) for key in ("year", "make", "model")
    )
    base_parts = [part for part in (vehicle, manufacturer, title, stamp) if part]
    base = _safe_filename(" ".join(base_parts), f"{manufacturer} OEM research {stamp}")
    pdf_path = folder / f"{base}.pdf"
    sidecar = folder / f"{base}.source.json"
    pdf_path.write_bytes(pdf_bytes)
    provenance = {
        "provider": manufacturer,
        "source_type": "public_oem_or_manufacturer_source",
        "source_url": final_url,
        "retrieved_at": retrieved_at,
        "content_type": content_type or ("application/pdf" if is_pdf else "text/html"),
        "source_sha256": source_sha256,
        "saved_pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "artifact_kind": artifact_kind,
        "authoritative_artifact": authoritative_artifact,
        "source_url_is_authority": True,
        "targeted_research": True,
        "storage_policy": "year/make/model",
        "vehicle": vehicle_identity,
        "credential_secret_stored_in_document": False,
    }
    sidecar.write_text(json.dumps(provenance, indent=2), encoding="utf-8")

    adas.inventory._cache = None  # noqa: SLF001 - intentional ingestion invalidation
    pages = adas._pages(pdf_path)
    readable_pages = sum(1 for _number, text in pages if str(text or "").strip())
    if readable_pages == 0:
        raise ValueError("Captured source was saved, but ADAS SI could not extract readable page text.")

    return {
        "status": "success",
        "action": "public_capture",
        "saved": True,
        "provider": manufacturer,
        "relative_path": adas.relative_of(pdf_path),
        "source_sidecar": adas.relative_of(sidecar),
        "pages": len(pages),
        "readable_pages": readable_pages,
        "source_url": final_url,
        "artifact_kind": artifact_kind,
        "authoritative_artifact": authoritative_artifact,
        "provenance": provenance,
        "text_preview": text_preview[:3000] if text_preview else "",
    }


def install() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        from ..tools import registry as registry_mod

        schema = registry_mod.TOOL_SCHEMAS.get("collision_research", {})
        action_schema = (
            schema.get("parameters", {})
            .get("properties", {})
            .get("action", {})
        )
        enum = action_schema.get("enum")
        if isinstance(enum, list) and "public_capture" not in enum:
            enum.append("public_capture")

        previous_init = registry_mod.Registry.__init__
        if not getattr(previous_init, "_xomni_public_oem_capture", False):
            def registry_init(self, *args, **kwargs):
                previous_init(self, *args, **kwargs)
                prior_handler = self._handlers.get("collision_research")  # noqa: SLF001

                async def handler(tool_args: dict[str, Any]):
                    if str(tool_args.get("action") or "").casefold() != "public_capture":
                        if prior_handler is None:
                            raise ValueError("Collision research operator is unavailable.")
                        result = prior_handler(tool_args)
                        if hasattr(result, "__await__"):
                            result = await result
                        return result
                    from ..config import Settings
                    from . import adas_si as adas_si_mod
                    settings = Settings.load()
                    adas = adas_si_mod.AdasSI(
                        settings.adas_si_root,
                        settings.root / "data" / "capabilities" / "adas_si" / "index.sqlite",
                    )
                    return await public_capture(tool_args, adas)

                self.register("collision_research", handler)

            registry_init._xomni_public_oem_capture = True  # type: ignore[attr-defined]
            registry_mod.Registry.__init__ = registry_init

        _INSTALLED = True
