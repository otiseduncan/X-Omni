"""Backfill page-level OCR for the existing ADAS SI library.

Run from the X Omni repo after setup:
    .\.venv\Scripts\python.exe .\scripts\backfill-adas-ocr.py

The original OEM PDFs are never modified.  The script fills only the local
SQLite OCR cache used by the normal ADAS SI search/research path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import Settings  # noqa: E402
from core.services import adas_ocr, adas_si  # noqa: E402


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

    summary = adas_ocr.backfill(service)
    summary["source_root"] = str(settings.adas_si_root)
    summary["cache_path"] = str(service.cache_path)
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
