"""Shared evidence fixtures for the evidence-interpretation tests.

``hkg_bumper_charts_ocr.json`` is real: the region-OCR page text of the two
Hyundai/Kia/Genesis bumper requirement charts in the ADAS SI library, captured
on 2026-09-17, plus the flattened excerpt X actually received in conversation
257 before the fix. The staged Kia radar procedure below is a written fixture
in the shape of Kia service information; it is labelled as a fixture and never
stored in the library.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.services.adas_si import page_excerpt

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "evidence"
CHART_TITLE = "Hyundai Kia Genesis Front Radar Bumper Requirements"
CHART_PATH = f"Reference/Calibration Requirements/Hyundai Kia Genesis/{CHART_TITLE}.pdf"


def bumper_chart_pages() -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / "hkg_bumper_charts_ocr.json").read_text(encoding="utf-8"))


def chart_search_hit(query_tokens: list[str]) -> dict[str, Any]:
    """The ADAS SI search hit production builds from the real chart page."""

    text = bumper_chart_pages()["pages"][CHART_TITLE]
    excerpt, truncated = page_excerpt(text, query_tokens)
    return {
        "source": f"{CHART_TITLE}.pdf",
        "title": CHART_TITLE,
        "page": 1,
        "relative_path": CHART_PATH,
        "url": "/api/adas-si/document?path=Reference/Calibration%20Requirements/Hyundai%20Kia%20Genesis/Hyundai%20Kia%20Genesis%20Front%20Radar%20Bumper%20Requirements.pdf",
        "excerpt": excerpt,
        "excerpt_truncated": truncated,
        "text_extraction": {
            "method": "ocr",
            "status": "success",
            "confidence": 0.93,
            "pipeline_version": "1+region",
            "source_is_original_pdf": True,
        },
    }


STAGED_PROCEDURE_TITLE = "2023 Kia Sportage (NQ5) Front Radar Service Information (fixture)"
STAGED_PROCEDURE_TEXT = "\n".join(
    [
        "FRONT RADAR (ADAS) - ADJUSTMENT",
        "Front Radar Mounting Inspection",
        "1. Remove the front bumper cover. (Refer to Body - Front Bumper Cover)",
        "2. Inspect the front radar bracket and its mounting points for deformation or damage.",
        "3. Verify the radar is fully seated in the bracket and the connector is locked.",
        "4. Reinstall the front bumper cover and tighten all fasteners.",
        "Front Radar Calibration - Stop Mode",
        "Check the following before performing the calibration:",
        "- The front bumper cover is installed and all fasteners are tightened.",
        "- Park the vehicle on flat ground with the tires pointed straight ahead.",
        "- Set the tire pressure to specification.",
        "5. Install the reflector in the designated position in front of the vehicle.",
        "6. With IG ON, perform with KDS: S/W Management > Front Radar > "
        "Inspection/correction of the front radar mounting angle > C1 (Stop mode).",
    ]
)


def staged_procedure_hit() -> dict[str, Any]:
    return {
        "source": "2023 Kia Sportage Front Radar Service Information.pdf",
        "title": STAGED_PROCEDURE_TITLE,
        "page": 3,
        "relative_path": "2023/Kia/Sportage/2023 Kia Sportage Front Radar Service Information.pdf",
        "url": "/api/adas-si/document?path=2023/Kia/Sportage/2023%20Kia%20Sportage%20Front%20Radar%20Service%20Information.pdf",
        "excerpt": STAGED_PROCEDURE_TEXT,
        "text_extraction": {"method": "native", "status": "success", "source_is_original_pdf": True},
    }


# Authoritative evidence that contradicts the common assumption that a rear
# corner radar is calibrated statically with a target.
DYNAMIC_ONLY_TITLE = "2024 Hyundai Tucson (NX4) Rear Corner Radar Service Information (fixture)"
DYNAMIC_ONLY_TEXT = "\n".join(
    [
        "REAR CORNER RADAR (BCW) - ADJUSTMENT",
        "After the rear corner radar or rear bumper is removed, reinstalled, or replaced,",
        "no static calibration is performed and no target or reflector is used.",
        "Rear Corner Radar Auto Calibration (Driving)",
        "1. Clear DTCs with KDS.",
        "2. Drive straight at 30 km/h (19 mph) or more for at least 10 minutes on a road with guardrails.",
        "3. Confirm the BCW warning light turns off and no DTC returns.",
    ]
)


def dynamic_only_hit() -> dict[str, Any]:
    return {
        "source": "2024 Hyundai Tucson Rear Corner Radar Service Information.pdf",
        "title": DYNAMIC_ONLY_TITLE,
        "page": 2,
        "relative_path": "2024/Hyundai/Tucson/2024 Hyundai Tucson Rear Corner Radar Service Information.pdf",
        "url": "/api/adas-si/document?path=2024/Hyundai/Tucson/2024%20Hyundai%20Tucson%20Rear%20Corner%20Radar%20Service%20Information.pdf",
        "excerpt": DYNAMIC_ONLY_TEXT,
        "text_extraction": {"method": "native", "status": "success", "source_is_original_pdf": True},
    }


def raw_row_lines(text: str, *, minimum_cells: int = 4) -> list[str]:
    """Lines of an OCR table that are machine rows, for dump checks."""

    rows = []
    for line in text.splitlines():
        cells = [cell for cell in line.split("    ") if cell.strip()]
        if len(cells) >= minimum_cells:
            rows.append(" ".join(cells[-minimum_cells:]))
    return rows
