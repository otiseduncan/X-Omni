"""Local OCR augmentation for the ADAS SI PDF library.

The authoritative artifact remains the original OEM PDF.  This module only
adds a page-level reading layer for pages whose embedded text is missing or
not useful.  OCR text is cached beside the existing ADAS SI extraction cache,
keyed by source path + page + source mtime + OCR pipeline version.

The integration is deliberately transparent: ``install_class`` wraps
``AdasSI._pages`` so existing search/research code receives readable page text
without a second OCR-specific tool path.  ``open_document`` is also wrapped so
opening a page gives X the extracted text in model context while the UI still
shows the real original PDF/page image.
"""

from __future__ import annotations

import io
import logging
import math
import re
import sqlite3
import threading
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Optional

from PIL import Image, ImageOps

log = logging.getLogger("xomni.adas_ocr")

OCR_PIPELINE_VERSION = "1"
OCR_RENDER_WIDTH = 2000
MIN_NATIVE_ALNUM = 24
MIN_NATIVE_WORDS = 4
MIN_OCR_ALNUM = 12
MIN_OCR_CONFIDENCE = 0.35
MAX_OCR_CHARS = 250_000

_ENGINE: Any = None
_ENGINE_ERROR: Optional[str] = None
_ENGINE_LOCK = threading.Lock()
_PATCHED_CLASSES: set[type] = set()


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unknown"


def _engine() -> Any:
    """Create RapidOCR lazily so Core startup and the 30B worker stay light."""
    global _ENGINE, _ENGINE_ERROR
    if _ENGINE is not None:
        return _ENGINE
    if _ENGINE_ERROR:
        raise RuntimeError(_ENGINE_ERROR)
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return _ENGINE
        if _ENGINE_ERROR:
            raise RuntimeError(_ENGINE_ERROR)
        try:
            from rapidocr import RapidOCR
        except ImportError as exc:
            _ENGINE_ERROR = (
                "Local ADAS OCR is not installed. Run X Omni setup so rapidocr and "
                "onnxruntime are installed."
            )
            raise RuntimeError(_ENGINE_ERROR) from exc
        try:
            # Keep OCR on CPU.  The conversation model already owns the GPUs and
            # OCR should never evict or starve it merely to read a service page.
            _ENGINE = RapidOCR()
        except Exception as exc:  # noqa: BLE001 - normalize third-party init failures
            _ENGINE_ERROR = f"RapidOCR failed to initialize: {type(exc).__name__}: {exc}"
            raise RuntimeError(_ENGINE_ERROR) from exc
    return _ENGINE


def ocr_status() -> dict[str, Any]:
    """Return dependency/runtime status without forcing model initialization."""
    try:
        rapidocr_version = metadata.version("rapidocr")
    except metadata.PackageNotFoundError:
        rapidocr_version = None
    try:
        ort_version = metadata.version("onnxruntime")
    except metadata.PackageNotFoundError:
        ort_version = None
    return {
        "available": bool(rapidocr_version and ort_version),
        "engine": "RapidOCR/ONNX Runtime",
        "rapidocr_version": rapidocr_version,
        "onnxruntime_version": ort_version,
        "pipeline_version": OCR_PIPELINE_VERSION,
        "execution_provider": "CPU",
        "initialization_error": _ENGINE_ERROR,
    }


def _alnum_count(text: str) -> int:
    return sum(ch.isalnum() for ch in str(text or ""))


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", str(text or ""), flags=re.UNICODE))


def _usable_native_text(text: str) -> bool:
    cleaned = str(text or "").strip()
    return (
        _alnum_count(cleaned) >= MIN_NATIVE_ALNUM
        and _word_count(cleaned) >= MIN_NATIVE_WORDS
    )


def _usable_ocr_text(text: str, confidence: Optional[float]) -> bool:
    cleaned = str(text or "").strip()
    if _alnum_count(cleaned) < MIN_OCR_ALNUM:
        return False
    return confidence is None or confidence >= MIN_OCR_CONFIDENCE


def _box_metrics(box: Any) -> tuple[float, float, float, float]:
    """Return x-left, y-top, y-center, height for one OCR quadrilateral."""
    try:
        points = box.tolist() if hasattr(box, "tolist") else box
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
        top, bottom = min(ys), max(ys)
        return min(xs), top, (top + bottom) / 2.0, max(1.0, bottom - top)
    except Exception:  # noqa: BLE001 - layout is optional, OCR text is not
        return 0.0, 0.0, 0.0, 1.0


def _layout_text(txts: Any, boxes: Any) -> str:
    """Preserve reading order and coarse table columns using OCR boxes."""
    texts = [str(value).strip() for value in (txts or [])]
    if not texts:
        return ""
    if boxes is None:
        return "\n".join(text for text in texts if text)

    items: list[dict[str, Any]] = []
    try:
        box_values = list(boxes)
    except TypeError:
        box_values = []
    for index, text in enumerate(texts):
        if not text:
            continue
        metrics = _box_metrics(box_values[index] if index < len(box_values) else None)
        items.append(
            {
                "text": text,
                "x": metrics[0],
                "top": metrics[1],
                "y": metrics[2],
                "height": metrics[3],
            }
        )
    if not items:
        return ""

    heights = sorted(item["height"] for item in items)
    median_height = heights[len(heights) // 2]
    row_tolerance = max(5.0, median_height * 0.55)
    items.sort(key=lambda item: (item["y"], item["x"]))

    rows: list[list[dict[str, Any]]] = []
    row_centers: list[float] = []
    for item in items:
        if not rows or abs(item["y"] - row_centers[-1]) > row_tolerance:
            rows.append([item])
            row_centers.append(item["y"])
        else:
            rows[-1].append(item)
            row_centers[-1] = sum(entry["y"] for entry in rows[-1]) / len(rows[-1])

    lines: list[str] = []
    for row in rows:
        row.sort(key=lambda item: item["x"])
        lines.append("    ".join(item["text"] for item in row))
    return "\n".join(lines).strip()


def _candidate_from_result(result: Any, rotation: int) -> dict[str, Any]:
    txts = getattr(result, "txts", None)
    boxes = getattr(result, "boxes", None)
    scores = getattr(result, "scores", None)
    text = _layout_text(txts, boxes)[:MAX_OCR_CHARS]
    score_values: list[float] = []
    for value in scores or []:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            score_values.append(max(0.0, min(parsed, 1.0)))
    confidence = (
        sum(score_values) / len(score_values) if score_values else None
    )
    return {
        "text": text,
        "confidence": confidence,
        "line_count": len([line for line in text.splitlines() if line.strip()]),
        "rotation": rotation,
        "alnum": _alnum_count(text),
    }


def _candidate_rank(candidate: dict[str, Any]) -> tuple[int, float, int]:
    confidence = candidate.get("confidence")
    confidence_value = float(confidence) if confidence is not None else 0.0
    usable = _usable_ocr_text(candidate.get("text") or "", confidence)
    return (1 if usable else 0, confidence_value, int(candidate.get("alnum") or 0))


def _ocr_png(png_bytes: bytes) -> dict[str, Any]:
    """OCR one rendered page, retrying whole-page 90-degree rotation if weak."""
    with Image.open(io.BytesIO(png_bytes)) as opened:
        base = ImageOps.exif_transpose(opened).convert("RGB")
        base = ImageOps.autocontrast(base)

    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("RapidOCR requires numpy but numpy is not installed.") from exc

    engine = _engine()
    candidates: list[dict[str, Any]] = []
    for rotation in (0, 90, 270):
        image = base if rotation == 0 else base.rotate(rotation, expand=True)
        try:
            result = engine(np.asarray(image))
        except Exception as exc:  # noqa: BLE001 - try alternate orientation first
            log.warning("ADAS OCR pass at %s degrees failed: %s", rotation, type(exc).__name__)
            continue
        candidate = _candidate_from_result(result, rotation)
        candidates.append(candidate)
        if rotation == 0 and _candidate_rank(candidate) >= (1, 0.72, 80):
            break

    if not candidates:
        raise RuntimeError("RapidOCR produced no usable OCR pass for the rendered page.")
    best = max(candidates, key=_candidate_rank)
    return {
        "text": best["text"],
        "confidence": best["confidence"],
        "line_count": best["line_count"],
        "rotation": best["rotation"],
        "engine": "rapidocr-onnxruntime",
        "engine_version": _package_version("rapidocr"),
        "pipeline_version": OCR_PIPELINE_VERSION,
    }


def _ensure_cache(adas: Any) -> None:
    if getattr(adas, "_xomni_ocr_cache_ready", False):
        return
    cache_path = Path(adas.cache_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(cache_path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS ocr_pages("
            "path TEXT NOT NULL, page INTEGER NOT NULL, source_mtime_ns INTEGER NOT NULL, "
            "text TEXT NOT NULL, status TEXT NOT NULL, confidence REAL, line_count INTEGER, "
            "rotation INTEGER NOT NULL DEFAULT 0, engine TEXT NOT NULL, "
            "engine_version TEXT NOT NULL, pipeline_version TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, PRIMARY KEY(path, page))"
        )
        current = db.execute(
            "SELECT value FROM meta WHERE key='ocr_pipeline_version'"
        ).fetchone()
        if current is None or str(current[0]) != OCR_PIPELINE_VERSION:
            db.execute("DELETE FROM ocr_pages")
        db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('ocr_pipeline_version', ?)",
            (OCR_PIPELINE_VERSION,),
        )
    adas._xomni_ocr_cache_ready = True


def _cached_page(adas: Any, path: Path, page: int, mtime: int) -> Optional[dict[str, Any]]:
    _ensure_cache(adas)
    with sqlite3.connect(adas.cache_path) as db:
        row = db.execute(
            "SELECT text, status, confidence, line_count, rotation, engine, "
            "engine_version, pipeline_version FROM ocr_pages "
            "WHERE path=? AND page=? AND source_mtime_ns=?",
            (str(path), int(page), int(mtime)),
        ).fetchone()
    if row is None:
        return None
    return {
        "text": str(row[0] or ""),
        "status": str(row[1] or "empty"),
        "confidence": row[2],
        "line_count": int(row[3] or 0),
        "rotation": int(row[4] or 0),
        "engine": str(row[5] or "rapidocr-onnxruntime"),
        "engine_version": str(row[6] or "unknown"),
        "pipeline_version": str(row[7] or OCR_PIPELINE_VERSION),
        "cached": True,
    }


def _store_page(
    adas: Any,
    path: Path,
    page: int,
    mtime: int,
    result: dict[str, Any],
    status: str,
) -> None:
    _ensure_cache(adas)
    with sqlite3.connect(adas.cache_path) as db:
        db.execute(
            "INSERT OR REPLACE INTO ocr_pages("
            "path, page, source_mtime_ns, text, status, confidence, line_count, rotation, "
            "engine, engine_version, pipeline_version, updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(path),
                int(page),
                int(mtime),
                str(result.get("text") or "")[:MAX_OCR_CHARS],
                status,
                result.get("confidence"),
                int(result.get("line_count") or 0),
                int(result.get("rotation") or 0),
                str(result.get("engine") or "rapidocr-onnxruntime"),
                str(result.get("engine_version") or "unknown"),
                OCR_PIPELINE_VERSION,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def _page_metadata(adas: Any, path: Path, page: int) -> dict[str, Any]:
    """Return how a page was read without changing original-document provenance."""
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {"method": "unknown", "pipeline_version": OCR_PIPELINE_VERSION}
    cached = _cached_page(adas, path, page, mtime)
    if cached is not None:
        return {
            "method": "ocr",
            "status": cached["status"],
            "confidence": cached["confidence"],
            "line_count": cached["line_count"],
            "rotation": cached["rotation"],
            "engine": cached["engine"],
            "engine_version": cached["engine_version"],
            "pipeline_version": cached["pipeline_version"],
            "source_is_original_pdf": True,
        }
    return {
        "method": "native",
        "status": "success",
        "pipeline_version": OCR_PIPELINE_VERSION,
        "source_is_original_pdf": True,
    }


def install_class(adas_cls: type) -> None:
    """Add page-level OCR to an AdasSI class exactly once."""
    if adas_cls in _PATCHED_CLASSES:
        return
    original_pages: Callable[..., Any] = adas_cls._pages
    original_search: Callable[..., Any] = adas_cls.search
    original_open: Callable[..., Any] = adas_cls.open_document

    def pages_with_ocr(self: Any, path: Path) -> list[tuple[int, str]]:
        native_pages = original_pages(self, path)
        if not native_pages or Path(path).suffix.lower() != ".pdf":
            return native_pages

        try:
            mtime = Path(path).stat().st_mtime_ns
        except OSError:
            return native_pages

        output: list[tuple[int, str]] = []
        for page_number, native_text in native_pages:
            native_text = str(native_text or "")
            if _usable_native_text(native_text):
                output.append((int(page_number), native_text))
                continue

            cached = _cached_page(self, Path(path), int(page_number), int(mtime))
            if cached is not None:
                cached_text = str(cached.get("text") or "")
                output.append(
                    (
                        int(page_number),
                        cached_text if _usable_ocr_text(cached_text, cached.get("confidence")) else native_text,
                    )
                )
                continue

            try:
                png = self.render_page(Path(path), int(page_number), width=OCR_RENDER_WIDTH)
                result = _ocr_png(png)
            except Exception as exc:  # noqa: BLE001 - search must keep the original honest result
                log.warning(
                    "ADAS OCR failed for %s page %s: %s",
                    Path(path).name,
                    page_number,
                    exc,
                )
                output.append((int(page_number), native_text))
                continue

            ocr_text = str(result.get("text") or "")
            good = _usable_ocr_text(ocr_text, result.get("confidence"))
            _store_page(
                self,
                Path(path),
                int(page_number),
                int(mtime),
                result,
                "success" if good else "low_quality",
            )
            output.append((int(page_number), ocr_text if good else native_text))
        return output

    def search_with_ocr(self: Any, args: dict) -> dict[str, Any]:
        result = original_search(self, args)
        if not isinstance(result, dict):
            return result
        for hit in result.get("results") or []:
            if not isinstance(hit, dict):
                continue
            relative = hit.get("relative_path")
            page = hit.get("page")
            if not relative or not page:
                continue
            try:
                source = self.resolve_relative(str(relative))
                hit["text_extraction"] = _page_metadata(self, source, int(page))
            except (OSError, ValueError, TypeError):
                continue
        if result.get("status") == "partial_success":
            result["message"] = (
                "The document matched, but native extraction and local OCR did not produce "
                "usable page text. Open the original source directly; do not treat this as "
                "the procedure being absent."
            )
        result["ocr"] = ocr_status()
        return result

    def open_with_text(self: Any, args: dict) -> dict[str, Any]:
        result = original_open(self, args)
        if not isinstance(result, dict) or result.get("status") != "success":
            return result
        document = result.get("document")
        if not isinstance(document, dict):
            return result
        relative = document.get("relative_path")
        page_number = int(document.get("page") or 1)
        if not relative:
            return result
        try:
            source = self.resolve_relative(str(relative))
            pages = self._pages(source)
            page_text = next((text for number, text in pages if int(number) == page_number), "")
            document["page_text"] = str(page_text or "")
            document["text_extraction"] = _page_metadata(self, source, page_number)
            document["readable_by_x"] = bool(str(page_text or "").strip())
            result["ocr"] = ocr_status()
        except Exception as exc:  # noqa: BLE001 - page remains displayable even if reading failed
            document["page_text"] = ""
            document["readable_by_x"] = False
            document["text_extraction"] = {
                "method": "unavailable",
                "error": f"{type(exc).__name__}: {exc}",
                "pipeline_version": OCR_PIPELINE_VERSION,
                "source_is_original_pdf": True,
            }
        return result

    adas_cls._pages = pages_with_ocr
    adas_cls.search = search_with_ocr
    adas_cls.open_document = open_with_text
    adas_cls.ocr_status = lambda self: ocr_status()
    adas_cls.page_text_metadata = lambda self, path, page: _page_metadata(
        self, Path(path), int(page)
    )
    _PATCHED_CLASSES.add(adas_cls)


def backfill(adas: Any) -> dict[str, Any]:
    """Read every library PDF once, OCRing only pages without usable native text."""
    documents = adas.inventory.documents()
    summary = {
        "documents_examined": 0,
        "pages_examined": 0,
        "native_pages": 0,
        "ocr_pages": 0,
        "unreadable_pages": 0,
        "errors": [],
        "ocr": ocr_status(),
    }
    for document in documents:
        path = document.get("_path")
        if not isinstance(path, Path):
            continue
        summary["documents_examined"] += 1
        try:
            pages = adas._pages(path)
        except Exception as exc:  # noqa: BLE001 - continue the library backfill
            summary["errors"].append(
                {"document": path.name, "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        for page_number, text in pages:
            summary["pages_examined"] += 1
            metadata_row = _page_metadata(adas, path, int(page_number))
            if metadata_row.get("method") == "ocr":
                summary["ocr_pages"] += 1
            else:
                summary["native_pages"] += 1
            if not str(text or "").strip():
                summary["unreadable_pages"] += 1
    summary["status"] = "success" if not summary["errors"] else "partial_success"
    return summary
