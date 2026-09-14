"""Veto internally contradictory ADAS semantic-review acceptances.

X remains the semantic judge.  The independent reviewer already declares a
``procedure_type`` such as STATIC_RADAR or DYNAMIC_CAMERA.  This guard never
promotes a candidate and never chooses a browser path; it only refuses an
acceptance when that declared type directly contradicts an explicit system
family in the research objective.

The motivating live failure was a 2023 Honda Accord front millimeter-wave radar
objective that successfully captured ``Multipurpose Camera Aiming (Dynamic
Aiming)``.  The review layer could call that page an actual procedure and accept
it because its consistency checks covered vehicle, classification, and
execution steps, but not requested system versus declared procedure type.
"""

from __future__ import annotations

from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_semantic_system_guard_installed__"

_RADAR_TYPES = frozenset({"STATIC_RADAR", "DYNAMIC_RADAR", "BLIND_SPOT_RADAR"})
_CAMERA_TYPES = frozenset({"STATIC_CAMERA", "DYNAMIC_CAMERA", "SURROUND_VIEW"})
_ACCEPTING = frozenset({"ACCEPT", "ACCEPT_WITH_DEPENDENCIES"})

_PROMPT_SUFFIX = (
    " Before accepting, explicitly compare the requested ADAS system/component with the "
    "candidate procedure type. Radar and camera are different sensor families: a "
    "forward-looking/multipurpose/windshield camera aiming procedure does NOT satisfy a "
    "millimeter-wave/front-radar objective, and a radar aiming procedure does NOT satisfy "
    "a camera objective. Set procedure_type to what the candidate actually calibrates, "
    "not to what the objective asked for. If the candidate is for a different ADAS sensor "
    "family, use CONTINUE_SEARCH or REJECT rather than ACCEPT."
)


def _fold(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def objective_family(objective: Any) -> str | None:
    """Return only an explicitly stated radar/camera family.

    This is intentionally conservative and veto-only.  Ambiguous objectives
    return None, which means Python makes no semantic decision at all.
    """

    if not isinstance(objective, dict):
        return None
    text = " ".join(
        _fold(objective.get(key))
        for key in ("objective", "system", "component")
        if objective.get(key) not in (None, "")
    )
    if any(term in text for term in ("radar", "millimeter wave", "millimetre wave")):
        return "radar"
    if any(term in text for term in ("camera", "windshield imager", "vision sensor")):
        return "camera"
    return None


def candidate_family(procedure_type: Any) -> str | None:
    value = str(procedure_type or "").strip().upper()
    if value in _RADAR_TYPES:
        return "radar"
    if value in _CAMERA_TYPES:
        return "camera"
    return None


def apply_system_consistency(verdict: Any, *, objective: Any) -> Any:
    """Downgrade only a direct objective/procedure-family contradiction."""

    if not isinstance(verdict, dict):
        return verdict
    if verdict.get("decision") not in _ACCEPTING:
        return verdict

    expected = objective_family(objective)
    candidate = candidate_family(verdict.get("procedure_type"))
    if expected is None or candidate is None or expected == candidate:
        return verdict

    out = dict(verdict)
    original = str(out.get("decision") or "")
    out["original_decision"] = out.get("original_decision", original)
    out["decision"] = "UNCERTAIN"
    inconsistent = list(out.get("inconsistent") or [])
    inconsistent.append(
        "decision accepts a candidate whose declared procedure_type "
        f"{out.get('procedure_type')} is {candidate}, while the explicit research "
        f"objective is {expected}"
    )
    out["inconsistent"] = inconsistent
    out["system_family_check"] = {
        "objective_family": expected,
        "candidate_family": candidate,
        "procedure_type": out.get("procedure_type"),
        "veto_only": True,
    }
    return out


def install(review_module: Any, navigator_module: Any | None = None) -> None:
    """Install the veto and refresh Navigator's imported reviewer reference."""

    if getattr(review_module, _INSTALLED_ATTR, False):
        if navigator_module is not None:
            navigator_module.review_candidate = review_module.review_candidate
        return

    prompt = str(getattr(review_module, "REVIEW_SYSTEM_PROMPT", ""))
    if _PROMPT_SUFFIX.strip() not in prompt:
        review_module.REVIEW_SYSTEM_PROMPT = prompt + _PROMPT_SUFFIX

    original = review_module.review_candidate

    @wraps(original)
    async def review_candidate_with_system_guard(*args: Any, **kwargs: Any):
        verdict = await original(*args, **kwargs)
        return apply_system_consistency(verdict, objective=kwargs.get("objective"))

    review_module.review_candidate = review_candidate_with_system_guard
    setattr(review_module, _INSTALLED_ATTR, True)

    # research_navigator_agent imports review_candidate by name, so refresh
    # that bound reference after wrapping the semantic-review module.
    if navigator_module is not None:
        navigator_module.review_candidate = review_module.review_candidate
