"""Compatibility install hook for retired text-rewriting continuity routing.

Conversation continuity is represented as structured active-subject context.
This module intentionally does not inspect or rewrite user messages and does
not patch :class:`core.orchestrator.loop.Orchestrator`.
"""

from __future__ import annotations

import threading

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def install() -> None:
    """Retain import compatibility without installing conversational routing."""

    global _INSTALLED
    with _INSTALL_LOCK:
        _INSTALLED = True
