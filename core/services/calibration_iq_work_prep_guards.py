"""Narrow safety corrections for the Calibration IQ work-prep bridge.

Keep ADAS Map authority scoped to the actual ADAS Map subtree/metadata, never a
sibling merely because the parent happens to contain an ``adas_map`` key. Also
accept the observed voice transcription variants of "ADAS Quick Reference" so
the cooperative collector is reachable from field dictation.
"""

from __future__ import annotations

import re
from typing import Any

from . import calibration_iq_work_prep as prep


def _strict_adas_map_marker(value: Any, path: tuple[str, ...]) -> bool:
    if any(prep._ADAS_MAP_MARKER_RE.search(part.replace("_", " ")) for part in path):  # noqa: SLF001
        return True
    if not isinstance(value, dict):
        return False
    for key in (
        "provider",
        "source",
        "source_name",
        "title",
        "document_type",
        "name",
        "label",
    ):
        raw = value.get(key)
        if isinstance(raw, str) and prep._ADAS_MAP_MARKER_RE.search(raw):  # noqa: SLF001
            return True
    return False


def install() -> None:
    # Speech-to-text has produced "8 oz quick reference" in a real field turn.
    # Keep this deliberately specific to Quick Reference; it must not make
    # arbitrary "8 oz" utterances enter the licensed research lane.
    prep._QUICK_REFERENCE_RE = re.compile(  # noqa: SLF001
        r"\b(?:adas|ados|a\s*d\s*a\s*s|8\s*oz)\s+quick\s+reference\b|"
        r"\bquick\s+reference\b.{0,60}\b(?:adas|ados|a\s*d\s*a\s*s)\b",
        re.IGNORECASE | re.DOTALL,
    )
    prep._node_has_adas_map_marker = _strict_adas_map_marker  # noqa: SLF001
