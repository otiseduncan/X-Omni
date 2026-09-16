"""Global scope rules for Calibration IQ-bound service-information research.

Some Calibration IQ requirements are field checks rather than document-backed
service-information objectives.  ``Seat Belt`` is one of them: the technician
performs the physical belt/tug inspection; there is no calibration/aiming/
initialization procedure to retrieve.  This rule is universal across vehicle
makes/models, so it belongs at objective creation rather than in OEM navigation
or ScrapeX.

The exclusion is deliberately exact.  A specifically named operation such as
``Seat Belt Pretensioner Initialization`` is not silently discarded; only the
generic inspection requirement is removed from the SI denominator.
"""

from __future__ import annotations

import re
from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_si_scope_guard_v1__"

_NON_PROCEDURAL_REQUIREMENTS = frozenset(
    {
        "seat belt",
        "seat belts",
        "seat belt inspection",
        "seat belts inspection",
        "seat belt tug test",
        "seat belts tug test",
    }
)


def _fold_label(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold())
    return " ".join(text.split())


def requires_written_si(title: Any) -> bool:
    """Whether this requirement belongs in procedure-retrieval research."""

    return _fold_label(title) not in _NON_PROCEDURAL_REQUIREMENTS


def install(research_module: Any) -> None:
    if getattr(research_module, _INSTALLED_ATTR, False):
        return

    original = research_module.objectives_for

    @wraps(original)
    def objectives_for_with_nonprocedural_exclusions(*args: Any, **kwargs: Any):
        objectives = original(*args, **kwargs)
        return [
            objective
            for objective in objectives
            if requires_written_si(objective.get("calibration_title"))
        ]

    research_module.objectives_for = objectives_for_with_nonprocedural_exclusions
    setattr(research_module, _INSTALLED_ATTR, True)
