"""Route ordinary calibration questions into the verified deep-research lane.

The user should not have to say "search the web" every time.  If a question is
about calibration/aiming requirements, X should automatically use the same
ADAS SI -> ALLDATA -> public OEM workflow unless the request is explicitly
local-only.  Calibration IQ repair-order research remains on its dedicated
operator lane.
"""

from __future__ import annotations

import re
import threading

from . import adas_calibration_depth, research_workflow

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False

_CIQ_RE = re.compile(
    r"\b(?:calibration\s+iq|ciq)\b[^.!?\n]{0,140}\b(?:repair\s+order|ro)\b"
    r"|\b(?:repair\s+order|ro)\b[^.!?\n]{0,140}\b(?:calibration\s+iq|ciq)\b",
    re.IGNORECASE,
)
_LOCAL_ONLY_RE = re.compile(
    r"\b(?:adas\s+si|local\s+(?:database|library|data))\s+only\b"
    r"|\b(?:do\s+not|don't|without)\b[^.!?]{0,60}\b(?:web|internet|alldata|external|oem\s+site)\b",
    re.IGNORECASE,
)
_QUESTION_OR_RESEARCH_RE = re.compile(
    r"\b(?:research|find|verify|check|look\s*up|investigate|what|which|when|where|"
    r"does|do|is|are|should|must|need|needs|required|requirement|requirements)\b",
    re.IGNORECASE,
)


def install() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        previous = research_workflow.full_research_request
        if not getattr(previous, "_xomni_calibration_route", False):
            def calibration_research_request(message: object) -> bool:
                text = str(message or "").strip()
                if not text:
                    return False
                if previous(message):
                    return True
                if _CIQ_RE.search(text) or _LOCAL_ONLY_RE.search(text):
                    return False
                return bool(
                    adas_calibration_depth.calibration_intent(text)
                    and _QUESTION_OR_RESEARCH_RE.search(text)
                )

            calibration_research_request._xomni_calibration_route = True  # type: ignore[attr-defined]
            research_workflow.full_research_request = calibration_research_request
        _INSTALLED = True
