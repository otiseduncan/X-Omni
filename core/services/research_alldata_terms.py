"""OEM terminology expansion for ALLDATA calibration searches."""

from __future__ import annotations

import re
import threading

from . import research_alldata_navigation as nav

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def expanded_topic_variants(topic: str) -> list[str]:
    base = " ".join(str(topic or "").split()).strip()
    output = list(_PREVIOUS(base))

    def add(value: str) -> None:
        value = " ".join(value.split()).strip()
        if value and value.casefold() not in {item.casefold() for item in output}:
            output.append(value[:220])

    folded = base.casefold()
    if re.search(r"forward(?:-facing|\s+facing)?|front\s+(?:camera|view)", folded) and re.search(
        r"calibrat|align|aim", folded
    ):
        add("Forward Facing Camera Calibration")
        add("Forward Camera Alignment")
        add("Image Processing Module A Camera Alignment")
        add("IPMA Camera Alignment")
        add("Lane Keeping System Camera Alignment")

    if "camera" in folded and re.search(r"calibrat|align|aim", folded):
        add("Camera Alignment")
        add("Camera Calibration")

    if re.search(r"blind\s+spot|\bbsm\b|\bbsd\b", folded):
        add("Blind Spot Monitor Calibration")
        add("Blind Spot Detection Calibration")
        add("Blind Spot Sensor Adjustment")
        add("Blind Spot Module Programming Calibration")

    return output[:8]


def install() -> None:
    global _INSTALLED, _PREVIOUS
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        _PREVIOUS = nav.topic_variants
        nav.topic_variants = expanded_topic_variants
        _INSTALLED = True


_PREVIOUS = nav.topic_variants
