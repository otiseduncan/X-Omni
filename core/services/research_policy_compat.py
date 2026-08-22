"""Compatibility shims for the generalized deep manufacturer reader."""

from __future__ import annotations

import threading
from typing import Any, Optional

from . import research_policy_depth

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


async def _read_policy_source(source: dict[str, Any], make: Optional[str]):
    """Legacy helper retained for tests and callers from the policy-only phase."""
    findings, _reads, _pages, _links = await research_policy_depth._read_deep_source(  # noqa: SLF001
        source,
        make,
        calibration_mode=False,
    )
    return findings


def install() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        if not hasattr(research_policy_depth, "_read_policy_source"):
            research_policy_depth._read_policy_source = _read_policy_source  # type: ignore[attr-defined]
        _INSTALLED = True
