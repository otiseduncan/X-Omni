"""Global scope rules for Calibration IQ-bound service-information work.

Some Calibration IQ requirements are field checks rather than document-backed
service-information objectives. ``Seat Belt`` is one of them: the technician
performs the physical belt/tug inspection; there is no calibration/aiming/
initialization procedure to retrieve. This rule is universal across vehicle
makes/models, so it belongs at SI scope creation rather than in OEM navigation
or ScrapeX.

The same exclusion is applied to background research and SI readiness/coverage.
Seat Belt remains a real Calibration IQ / ADAS Map requirement; it simply does
not create a missing-document obligation or an ALLDATA acquisition target.

The exclusion is deliberately exact. A specifically named operation such as
``Seat Belt Pretensioner Initialization`` is not silently discarded; only the
generic inspection requirement is removed from the SI denominator.
"""

from __future__ import annotations

import re
from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_si_scope_guard_v1__"
_WORK_PREP_INSTALLED_ATTR = "__xomni_si_scope_work_prep_guard_v1__"

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
    """Whether this requirement belongs in procedure-retrieval/coverage work."""

    return _fold_label(title) not in _NON_PROCEDURAL_REQUIREMENTS


def install(research_module: Any, work_prep_module: Any | None = None) -> None:
    if not getattr(research_module, _INSTALLED_ATTR, False):
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

    if work_prep_module is None or getattr(
        work_prep_module, _WORK_PREP_INSTALLED_ATTR, False
    ):
        return

    original_catalog_coverage = work_prep_module._catalog_coverage  # noqa: SLF001
    original_adas_coverage = work_prep_module._adas_coverage  # noqa: SLF001

    @wraps(original_catalog_coverage)
    async def catalog_coverage_without_nonprocedural_requirements(
        catalog: Any,
        snapshot: dict[str, Any],
        map_info: dict[str, Any],
    ):
        coverage = await original_catalog_coverage(catalog, snapshot, map_info)
        return [
            item
            for item in coverage
            if isinstance(item, dict)
            and requires_written_si(item.get("calibration"))
        ]

    @wraps(original_adas_coverage)
    async def legacy_adas_coverage_without_nonprocedural_requirements(
        adas: Any,
        vehicle: str,
        requirements: list[dict[str, Any]],
    ):
        filtered = [
            item
            for item in requirements
            if isinstance(item, dict)
            and requires_written_si(work_prep_module._requirement_label(item))  # noqa: SLF001
        ]
        return await original_adas_coverage(adas, vehicle, filtered)

    work_prep_module._catalog_coverage = (  # noqa: SLF001
        catalog_coverage_without_nonprocedural_requirements
    )
    work_prep_module._adas_coverage = (  # noqa: SLF001
        legacy_adas_coverage_without_nonprocedural_requirements
    )
    setattr(work_prep_module, _WORK_PREP_INSTALLED_ATTR, True)
