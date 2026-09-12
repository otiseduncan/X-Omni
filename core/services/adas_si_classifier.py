"""Deterministic content classification for PDFs dropped into ADAS SI's root.

This classifier deliberately chooses broad storage classes. A multi-model
calibration matrix is reference material; it must never be invented into one
Year/Make/Model application. Unknown documents remain visible for review.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable


_RO_RE = re.compile(r"\bRO\s*(?:Number|#)?\s*[:#-]?\s*(\d{6,20})\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\bYear\s*:\s*((?:19|20)\d{2})\b", re.IGNORECASE)
_MAKE_RE = re.compile(
    r"\bMake\s*:\s*([^\r\n]{1,64}?)"
    r"(?=\s+(?:Last Updated|Model|VIN|Insurance)\s*:|[\r\n]|$)",
    re.IGNORECASE,
)
_MODEL_RE = re.compile(
    r"\bModel\s*:\s*([^\r\n]{1,100}?)"
    r"(?=\s+(?:Estimate Source|Estimator|VIN)\s*:|[\r\n]|$)",
    re.IGNORECASE,
)


def _text(pages: Iterable[tuple[int, str]]) -> str:
    value = "\n".join(str(text or "") for _page, text in pages)
    value = (
        value.replace("\x00", "")
        .replace("\ufb01", "fi")
        .replace("\ufb02", "fl")
        .replace("\ufffd", " ")
        .replace("\xa0", " ")
    )
    return re.sub(r"[ \t]+", " ", value)


def _line_value(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    return " ".join(match.group(1).split()).strip(" .:-") or None


_REFERENCE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Hyundai Kia Genesis", ("hyundai", "kia", "genesis")),
    ("Volkswagen Audi", ("volkswagen", "vw/audi", "vw audi", "audi")),
    ("Toyota Lexus", ("toyota", "lexus")),
    ("Nissan Infiniti", ("nissan", "infiniti")),
    ("Jaguar Land Rover", ("jaguar", "land rover")),
    ("Honda Acura", ("honda", "acura")),
    ("Ford Lincoln", ("ford", "lincoln")),
    ("General Motors", ("general motors", "buick", "cadillac", "chevrolet", " gmc ")),
    ("Volvo", ("volvo",)),
    ("Subaru", ("subaru", "eyesight")),
    ("Mitsubishi", ("mitsubishi",)),
    ("Mazda", ("mazda",)),
    ("BMW", ("bmw",)),
)


def _reference_group(folded: str) -> tuple[str | None, list[str]]:
    best: tuple[str, list[str]] | None = None
    for label, markers in _REFERENCE_GROUPS:
        found = [marker.strip() for marker in markers if marker in folded]
        if found and (best is None or len(found) > len(best[1])):
            best = (label, found)
    if best is None:
        # One captured Kia BSM sheet has its title clipped, but its model list
        # is still specific enough to identify the make without guessing.
        kia_models = ("cadenza", "carnival", "ev6", "ev9", "forte", "telluride")
        present = [model for model in kia_models if model in folded]
        if len(present) >= 3 and "bumper mount" in folded:
            return "Kia", present + ["bumper mount"]
        return None, []
    # Prefer a family named directly by two or more markers. Ties retain the
    # curated order above, preventing a stray footer link from taking over.
    return best


def _reference_topic(folded: str) -> str:
    if "360 image" in folded or "360 camera" in folded:
        return "360 Camera Calibration Matrix"
    radar = "front radar" in folded or "adaptive cruise control module" in folded
    blind_spot = "blind spot" in folded or "blindspot" in folded
    camera = "windshield camera" in folded or "front camera" in folded
    park = "park assist" in folded or "surround view" in folded
    if radar and not any((blind_spot, camera, park)):
        return "Front Radar Bumper Requirements"
    if blind_spot and not any((radar, camera, park)):
        return "Blind Spot Monitor Bumper Requirements"
    if "bumper mount" in folded and not any((radar, camera, park)):
        return "Blind Spot Monitor Bumper Requirements"
    if sum(bool(value) for value in (radar, blind_spot, camera, park)) >= 2:
        return "ADAS Calibration Requirements"
    return "ADAS Calibration Matrix"


def classify_root_pdf(path: Path, pages: Iterable[tuple[int, str]]) -> dict[str, Any]:
    """Return a supported storage classification, or an honest review result."""

    text = _text(pages)
    folded = text.casefold()

    if "adas map" in folded:
        ro_number = _line_value(_RO_RE, text)
        if ro_number:
            vehicle = {
                "year": _line_value(_YEAR_RE, text),
                "make": _line_value(_MAKE_RE, text),
                "model": _line_value(_MODEL_RE, text),
            }
            return {
                "storage_class": "adas_map_report",
                "ro_number": ro_number,
                "vehicle": vehicle,
                "title": f"{ro_number} ADAS Map",
                "filename": f"{ro_number} ADAS Map.pdf",
                "confidence": "high",
                "evidence": ["ADAS MAP heading", f"RO Number {ro_number}"],
            }

    if "front camera calculator" in folded:
        return {
            "storage_class": "reference",
            "category": "Calculators",
            "group": "General",
            "title": "Front Camera Calculator",
            "filename": "Front Camera Calculator.pdf",
            "confidence": "high",
            "evidence": ["Front Camera Calculator heading"],
        }
    if "blind spot monitor" in folded and "distance calculator" in folded:
        return {
            "storage_class": "reference",
            "category": "Calculators",
            "group": "General",
            "title": "Blind Spot Monitor Distance Calculator",
            "filename": "Blind Spot Monitor Distance Calculator.pdf",
            "confidence": "high",
            "evidence": ["Blind Spot Monitor (BSM) Distance Calculator heading"],
        }

    group, group_evidence = _reference_group(folded)
    reference_signals = (
        "calibration requirements" in folded
        or "view only" in folded
        or "bumper mount" in folded
        or "front radar" in folded
        or "windshield camera" in folded
        or "blind spot monitor" in folded
        or "360 image" in folded
    )
    if group and reference_signals:
        topic = _reference_topic(folded)
        return {
            "storage_class": "reference",
            "category": "Calibration Requirements",
            "group": group,
            "title": f"{group} {topic}",
            "filename": f"{group} {topic}.pdf",
            "confidence": "high" if "calibration requirements" in folded else "supported",
            "evidence": group_evidence[:4] + [topic],
        }

    return {
        "storage_class": "needs_review",
        "title": path.stem,
        "filename": path.name,
        "confidence": "unresolved",
        "evidence": ["No exact repair order, vehicle identity, or supported reference class found"],
    }
