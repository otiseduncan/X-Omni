r"""Backfill page-level OCR for the existing ADAS SI library.

Run from the X Omni repo after setup:
    .\.venv\Scripts\python.exe .\scripts\backfill-adas-ocr.py

The original OEM PDFs are never modified. The script fills only the local
SQLite OCR cache used by the normal ADAS SI search/research path.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import Settings  # noqa: E402
from core.services import adas_si  # noqa: E402


def main() -> int:
    settings = Settings.load()
    service = adas_si.AdasSI(
        settings.adas_si_root,
        settings.root / "data" / "capabilities" / "adas_si" / "index.sqlite",
    )
    if not service.available():
        print(
            json.dumps(
                {
                    "status": "unavailable",
                    "source_root": str(settings.adas_si_root),
                    "message": "ADAS SI library is not reachable.",
                },
                indent=2,
            )
        )
        return 2

    documents = service.inventory.documents()
    total_documents = len(documents)
    started = time.perf_counter()
    summary = {
        "documents_examined": 0,
        "pages_examined": 0,
        "native_pages": 0,
        "ocr_pages": 0,
        "unreadable_pages": 0,
        "low_confidence_ocr_pages": 0,
        "errors": [],
        "ocr": service.ocr_status(),
        "source_root": str(settings.adas_si_root),
        "cache_path": str(service.cache_path),
    }

    print(f"ADAS OCR backfill: {total_documents} PDF document(s)", flush=True)
    for index, document in enumerate(documents, 1):
        path = document.get("_path")
        if not isinstance(path, Path):
            continue
        print(f"[{index}/{total_documents}] {path.name}", flush=True)
        summary["documents_examined"] += 1
        doc_native = 0
        doc_ocr = 0
        doc_unreadable = 0
        doc_low_confidence = 0
        try:
            pages = service._pages(path)
        except Exception as exc:  # noqa: BLE001 - continue the library backfill
            message = f"{type(exc).__name__}: {exc}"
            summary["errors"].append({"document": path.name, "error": message})
            print(f"    ERROR: {message}", flush=True)
            continue

        for page_number, text in pages:
            summary["pages_examined"] += 1
            metadata = service.page_text_metadata(path, int(page_number))
            if metadata.get("method") == "ocr":
                summary["ocr_pages"] += 1
                doc_ocr += 1
                confidence = metadata.get("confidence")
                if isinstance(confidence, (int, float)) and confidence < 0.50:
                    summary["low_confidence_ocr_pages"] += 1
                    doc_low_confidence += 1
            else:
                summary["native_pages"] += 1
                doc_native += 1
            if not str(text or "").strip():
                summary["unreadable_pages"] += 1
                doc_unreadable += 1

        detail = f"    pages={len(pages)} native={doc_native} ocr={doc_ocr} unreadable={doc_unreadable}"
        if doc_low_confidence:
            detail += f" low_confidence={doc_low_confidence}"
        print(detail, flush=True)

    summary["elapsed_seconds"] = round(time.perf_counter() - started, 2)
    summary["status"] = "success" if not summary["errors"] else "partial_success"

    print("\n=== ADAS OCR BACKFILL SUMMARY ===", flush=True)
    print(json.dumps(summary, indent=2, default=str), flush=True)
    return 0 if summary.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
