"""ADAS calibration/reset terminology expansion for ALLDATA research.

ALLDATA article titles vary by OEM. The search layer therefore expands the
user's plain-language system into common calibration, aiming, alignment,
initialization, relearn, reset, setup, and programming terminology. OEM-specific
names are optional extra aliases, never the primary routing rule.
"""

from __future__ import annotations

import re
import threading

from . import research_alldata_navigation as nav

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False

_OPERATION_RE = re.compile(
    r"\b(?:calibrat\w*|recalibrat\w*|aim\w*|align\w*|adjust\w*|"
    r"initializ\w*|relearn\w*|reset\w*|setup|set\s+up|program\w*|"
    r"configur\w*|learn\w*|zero[-\s]?point)\b",
    re.IGNORECASE,
)

_SYSTEMS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (
        re.compile(
            r"\b(?:forward(?:-facing|\s+facing)?\s+camera|front\s+camera|"
            r"forward\s+recognition\s+camera|lane\s+(?:keep|keeping|departure).*camera|"
            r"windshield\s+camera|monocular\s+camera)\b",
            re.IGNORECASE,
        ),
        (
            "Forward Facing Camera Calibration",
            "Front Camera Calibration",
            "Forward Camera Alignment",
            "Front Camera Aiming",
            "Forward Recognition Camera Calibration",
            "Lane Keeping Camera Alignment",
            "Lane Departure Camera Calibration",
            "ADAS Camera Calibration",
            "Camera Alignment",
            "Camera Calibration",
            # Common OEM-specific labels are additive aliases only.
            "Image Processing Module A Camera Alignment",
            "IPMA Camera Alignment",
        ),
    ),
    (
        re.compile(
            r"\b(?:blind\s+spot|\bbsm\b|\bbsd\b|rear\s+corner\s+radar|"
            r"side\s+radar|rear\s+side\s+radar|lane\s+change\s+assist)\b",
            re.IGNORECASE,
        ),
        (
            "Blind Spot Monitor Calibration",
            "Blind Spot Detection Calibration",
            "Rear Corner Radar Calibration",
            "Rear Side Radar Calibration",
            "Side Radar Calibration",
            "Blind Spot Sensor Adjustment",
            "Blind Spot Module Initialization",
            "Blind Spot Module Programming",
            "Lane Change Assist Calibration",
            "Rear Radar Calibration",
        ),
    ),
    (
        re.compile(
            r"\b(?:front\s+radar|forward\s+radar|millimeter\s+wave\s+radar|"
            r"distance\s+sensor|adaptive\s+cruise.*radar|cruise\s+control\s+module|\bccm\b)\b",
            re.IGNORECASE,
        ),
        (
            "Forward Radar Calibration",
            "Front Radar Calibration",
            "Millimeter Wave Radar Calibration",
            "Distance Sensor Calibration",
            "Radar Alignment",
            "Radar Aiming",
            "Adaptive Cruise Control Radar Alignment",
            "Cruise Control Module Calibration",
        ),
    ),
    (
        re.compile(
            r"\b(?:rear\s+(?:view\s+)?camera|backup\s+camera|back\s+camera|"
            r"surround\s+view|around\s+view|360\s+camera|parking\s+assist\s+camera)\b",
            re.IGNORECASE,
        ),
        (
            "Rear Camera Calibration",
            "Rear Camera Initialization",
            "Back Camera Position Setting",
            "Parking Aid Camera Initialization",
            "Surround View Camera Calibration",
            "Around View Monitor Calibration",
            "360 Camera Calibration",
            "Camera Optical Axis Adjustment",
        ),
    ),
    (
        re.compile(r"\b(?:steering\s+angle|steering\s+center|sas)\b", re.IGNORECASE),
        (
            "Steering Angle Sensor Calibration",
            "Steering Angle Sensor Initialization",
            "Steering Angle Reset",
            "Steering Center Learn",
            "Steering Center Memorization",
            "Steering Angle Neutral Point Calibration",
        ),
    ),
    (
        re.compile(
            r"\b(?:occupant\s+classification|ocs|passenger\s+presence|weight\s+sensor)\b",
            re.IGNORECASE,
        ),
        (
            "Occupant Classification System Calibration",
            "Occupant Classification System Initialization",
            "Occupant Classification Zero Point Calibration",
            "Passenger Presence System Rezero",
            "Seat Weight Sensor Calibration",
        ),
    ),
    (
        re.compile(
            r"\b(?:parking\s+aid|park\s+assist|ultrasonic|sonar|parking\s+sensor)\b",
            re.IGNORECASE,
        ),
        (
            "Parking Aid Calibration",
            "Park Assist Calibration",
            "Parking Sensor Calibration",
            "Ultrasonic Sensor Calibration",
            "Parking Aid Initialization",
        ),
    ),
)

_GENERIC_ADAS_TERMS = (
    "ADAS Calibration",
    "ADAS Initialization",
    "ADAS Relearn",
    "ADAS Reset",
    "ADAS Setup",
    "Sensor Calibration",
    "Sensor Initialization",
)


def expanded_topic_variants(topic: str) -> list[str]:
    base = " ".join(str(topic or "").split()).strip()
    output = list(_PREVIOUS(base))
    seen = {item.casefold() for item in output}

    def add(value: str) -> None:
        value = " ".join(str(value or "").split()).strip()
        folded = value.casefold()
        if value and folded not in seen:
            output.append(value[:220])
            seen.add(folded)

    folded = base.casefold()
    matched_system = False
    for pattern, aliases in _SYSTEMS:
        if not pattern.search(base):
            continue
        matched_system = True
        for alias in aliases:
            add(alias)

    # Generic calibration/reset vocabulary catches OEMs that file the same
    # operation under initialization/relearn/setup rather than calibration.
    if matched_system or _OPERATION_RE.search(base) or re.search(r"\badas\b", folded):
        for alias in _GENERIC_ADAS_TERMS:
            add(alias)

    # If the user names a system but no operation, still search the standard
    # operation family because ALLDATA may title the article as Initialization,
    # Relearn, Adjustment, or Setup instead of Calibration.
    if matched_system and not _OPERATION_RE.search(base):
        add(f"{base} calibration")
        add(f"{base} initialization")
        add(f"{base} relearn")
        add(f"{base} reset")
        add(f"{base} adjustment")

    # Keep provider navigation bounded while giving enough terminology breadth
    # to survive OEM naming differences.
    return output[:16]


def install() -> None:
    global _INSTALLED, _PREVIOUS
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        _PREVIOUS = nav.topic_variants
        nav.topic_variants = expanded_topic_variants
        _INSTALLED = True


_PREVIOUS = nav.topic_variants
