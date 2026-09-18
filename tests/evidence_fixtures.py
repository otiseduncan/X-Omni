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


# --- 2025 Kia K4 field regressions (written fixtures, never stored) --------

K4_2025 = {"year": 2025, "make": "Kia", "model": "K4"}

K4_BSM_TITLE = "2025 Kia K4 (BL3) Blind Spot Collision Warning Radar Calibration (fixture)"
K4_BSM_TEXT = "\n".join(
    [
        "REAR CORNER RADAR (BCW) - CALIBRATION",
        "Applies to: 2025 Kia K4",
        "Perform this calibration after a rear corner radar is removed or replaced, or after",
        "the rear bumper cover is removed and reinstalled.",
        "Before calibration: the rear bumper cover must be installed with all fasteners tightened.",
        "1. Connect KDS and select S/W Management > Rear Corner Radar > Calibration.",
        "2. Place the corner reflector 1.0 m behind the rear bumper at a 45 degree angle from the radar.",
        "3. Start the calibration with IG ON and the engine off.",
        "4. Confirm KDS reports the calibration complete and no BCW DTC returns.",
    ]
)

K4_BUMPER_TITLE = "2025 Kia K4 (BL3) Rear Bumper Cover Removal and Installation (fixture)"
K4_BUMPER_TEXT = "\n".join(
    [
        "REAR BUMPER COVER - REMOVAL AND INSTALLATION",
        "Applies to: 2025 Kia K4",
        "1. Remove the rear combination lamps.",
        "2. Remove the screws and clips and pull the rear bumper cover rearward.",
        "3. Disconnect the rear corner radar and parking sensor connectors.",
        "4. Installation is the reverse of removal.",
    ]
)

K4_FRONT_RADAR_TITLE = "2025 Kia K4 (BL3) Front Radar Bumper Requirement (fixture)"
K4_FRONT_RADAR_TEXT = "\n".join(
    [
        "FRONT RADAR (ADAS) - CALIBRATION CONDITIONS",
        "Applies to: 2025 Kia K4",
        "The front bumper cover must be installed during front radar calibration.",
    ]
)


def _k4_hit(title: str, text: str, relative: str, page: int = 1) -> dict[str, Any]:
    return {
        "source": f"{relative.rsplit('/', 1)[-1]}",
        "title": title,
        "page": page,
        "relative_path": relative,
        "url": f"/api/adas-si/document?path={relative.replace(' ', '%20')}",
        "excerpt": text,
        "text_extraction": {"method": "native", "status": "success", "source_is_original_pdf": True},
        "vehicle": dict(K4_2025),
    }


def k4_bsm_hit() -> dict[str, Any]:
    return _k4_hit(K4_BSM_TITLE, K4_BSM_TEXT, "2025/Kia/K4/2025 Kia K4 Rear Corner Radar Calibration.pdf", 4)


def k4_bumper_hit() -> dict[str, Any]:
    return _k4_hit(K4_BUMPER_TITLE, K4_BUMPER_TEXT, "2025/Kia/K4/2025 Kia K4 Rear Bumper Cover RI.pdf", 2)


def k4_front_radar_hit() -> dict[str, Any]:
    return _k4_hit(K4_FRONT_RADAR_TITLE, K4_FRONT_RADAR_TEXT, "2025/Kia/K4/2025 Kia K4 Front Radar Conditions.pdf")


def k4_front_radar_knowledge_record() -> dict[str, Any]:
    """A verified durable claim, as the knowledge store returns it after promotion."""

    return {
        "id": "akr_k4_front_radar_bumper",
        "lifecycle": "verified",
        "stored_lifecycle": "verified",
        "application": {"manufacturer": "Kia", "model": "K4", "year_start": 2025, "year_end": 2025},
        "system": {"name": "front radar"},
        "requirement": {
            "requirement_type": "calibration",
            "text": "The front bumper cover must be installed during front radar calibration.",
            "applicability_notes": "2025 Kia K4",
        },
        "source_integrity": {"status": "current", "verified_read_allowed": True},
        "evidence": [
            {
                "page_start": 1,
                "excerpt": "The front bumper cover must be installed during front radar calibration.",
                "verification_effective": True,
                "source": {"source_name": K4_FRONT_RADAR_TITLE},
            }
        ],
    }
