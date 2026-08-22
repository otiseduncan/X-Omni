"""Prevent external post-collision routing from hijacking Calibration IQ RO research."""

from __future__ import annotations

import re
import threading

from . import research_workflow

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False
_CIQ_RO_RE = re.compile(
    r"\b(?:calibration\s+iq|ciq)\b[^.!?\n]{0,120}\b(?:repair\s+order|ro)\b"
    r"|\b(?:repair\s+order|ro)\b[^.!?\n]{0,120}\b(?:calibration\s+iq|ciq)\b",
    re.IGNORECASE,
)


def install() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        previous = research_workflow.full_research_request
        if not getattr(previous, "_xomni_ciq_guard", False):
            def guarded(message: object) -> bool:
                text = str(message or "")
                if _CIQ_RO_RE.search(text):
                    return False
                return previous(message)

            guarded._xomni_ciq_guard = True  # type: ignore[attr-defined]
            research_workflow.full_research_request = guarded
        _INSTALLED = True
