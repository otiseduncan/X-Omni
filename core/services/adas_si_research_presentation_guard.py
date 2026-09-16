"""Keep failed SI objective rows identifiable in the structured card payload.

A failed Navigator objective may preserve a source URL even when the provider
returns no usable page title.  The card previously rendered that as an external-
link icon with no text, leaving two failed objectives visually anonymous.  This
adapter adds only presentation identity already present in the objective: the
calibration name plus "last reviewed candidate".  It does not change research
outcomes, reviewer decisions, URLs, or attachment truth.
"""

from __future__ import annotations

from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_si_research_presentation_guard_v1__"
_FAILURE_OUTCOMES = frozenset({"not_found", "uncertain", "incomplete", "found_not_captured"})


def _clean(value: Any, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def _repair_row(row: Any) -> None:
    if not isinstance(row, dict):
        return
    title = _clean(row.get("title"))
    if title:
        row["title"] = title
        return
    if not _clean(row.get("source_url"), 1000):
        return

    calibration = _clean(row.get("calibration"), 180) or "Research objective"
    row["title"] = f"{calibration} — last reviewed candidate"

    # ResearchObjectiveRow normally shows `reason` only when there is no title.
    # Preserve the failure explanation after adding the fallback title by also
    # carrying it in the always-rendered incomplete-reasons list.
    if str(row.get("outcome") or "") in _FAILURE_OUTCOMES:
        reason = _clean(row.get("reason"), 300)
        reasons = [
            _clean(item, 300)
            for item in (row.get("incomplete_reasons") or [])
            if _clean(item, 300)
        ]
        if reason and reason not in reasons:
            reasons.append(reason)
        row["incomplete_reasons"] = reasons


def install(research_module: Any) -> None:
    if getattr(research_module, _INSTALLED_ATTR, False):
        return

    service_class = research_module.AdasSiResearchService
    original = service_class.public_view

    @wraps(original)
    def public_view_with_identified_failures(self: Any, record: dict[str, Any]):
        view = original(self, record)
        for row in view.get("objectives") or []:
            _repair_row(row)
        for group in view.get("groups") or []:
            if not isinstance(group, dict):
                continue
            for row in group.get("objectives") or []:
                _repair_row(row)
        return view

    service_class.public_view = public_view_with_identified_failures
    setattr(research_module, _INSTALLED_ATTR, True)
