"""Route ordinary calibration questions into the verified deep-research lane.

The user should not have to say "search the web" every time. If a question is
about calibration/aiming requirements, X should automatically use the same
ADAS SI -> ALLDATA -> public OEM workflow unless the request is explicitly
scoped to local ADAS SI. Calibration IQ repair-order research remains on its
dedicated operator lane.
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
_EXPLICIT_ADAS_SCOPE_RE = re.compile(
    r"\b(?:what\s+does|what's\s+in|show\s+me|find\s+in|search|check)\b"
    r"[^.!?]{0,45}\badas\s+si\b"
    r"|\b(?:in|from)\s+adas\s+si\b",
    re.IGNORECASE,
)
_EXTERNAL_SCOPE_RE = re.compile(
    r"\b(?:all\s*data|alldata|public\s+oem|official\s+(?:oem|manufacturer|[a-z0-9-]+\s+collision)|"
    r"manufacturer\s+(?:site|website|source|sources)|collision\s+pros?|oem\s+(?:site|website|source|sources)|"
    r"web|internet|external\s+(?:source|sources|research))\b",
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

                # Hard source boundaries always win. "ADAS SI only" and
                # explicit no-web instructions must never be widened by an
                # older broad research matcher.
                if _CIQ_RE.search(text) or _LOCAL_ONLY_RE.search(text):
                    return False

                # A plain request such as "What does ADAS SI say..." is local
                # only when the user did not ALSO ask for ALLDATA/OEM/web
                # research. This distinction matters for prompts like
                # "Check ADAS SI first, then ALLDATA, then Toyota Collision".
                explicit_adas_scope = bool(_EXPLICIT_ADAS_SCOPE_RE.search(text))
                explicit_external_scope = bool(_EXTERNAL_SCOPE_RE.search(text))
                if explicit_adas_scope and not explicit_external_scope:
                    return False

                if previous(message):
                    return True

                return bool(
                    adas_calibration_depth.calibration_intent(text)
                    and _QUESTION_OR_RESEARCH_RE.search(text)
                )

            calibration_research_request._xomni_calibration_route = True  # type: ignore[attr-defined]
            research_workflow.full_research_request = calibration_research_request
        _INSTALLED = True
