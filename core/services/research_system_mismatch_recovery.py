"""Give X an explicit recovery move after a reviewer-proven system mismatch.

A rejected candidate can be wrong at two different levels:

* the coarse sensor family is wrong (camera vs radar), which the semantic
  system guard records in ``system_family_check``; or
* the candidate is a real calibration procedure for the exact vehicle, but
  X's independent reviewer says it does not satisfy the requested objective.
  The 2026-09-14 Nissan rear-side-radar QA run demonstrated this second form:
  X reached the front ICC Distance Sensor alignment procedure, correctly did
  not accept it, but then stayed in that component branch instead of returning
  to the ADAS component choice and finding rear blind-spot radar.

This module never infers those meanings from titles, keywords, OEMs, or menu
paths. It reacts only to the reviewer's structured semantic result and turns
that result into a navigation constraint: leave the rejected procedure/component
branch, return to a live page where alternatives can be chosen, and reason again.
"""

from __future__ import annotations

from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_system_mismatch_recovery_installed__"
_BRANCH_EXIT_DECISIONS = frozenset({"CONTINUE_SEARCH", "REJECT"})
_PROMPT_SUFFIX = (
    "\n\nWRONG-PROCEDURE RECOVERY CONTRACT: When independent review says a candidate is "
    "a real procedure but does not satisfy the requested system/component, or proves "
    "that it belongs to a different ADAS sensor family, treat that as a branch-level "
    "correction. Do not extract it again and do not keep drilling deeper under the "
    "rejected component. Use the live controls to backtrack until you reach a page "
    "that exposes alternative ADAS systems/components, then choose the requested "
    "component from the rendered state. You still decide the route from what is "
    "actually on screen; never invent provider-specific labels, refs, or a fixed path."
)


def _family_mismatch(review: dict[str, Any]) -> dict[str, Any] | None:
    check = review.get("system_family_check")
    if not isinstance(check, dict):
        return None
    expected = str(check.get("objective_family") or "").strip()
    candidate = str(check.get("candidate_family") or "").strip()
    if not expected or not candidate or expected == candidate:
        return None
    return check


def _branch_mismatch(review: Any) -> dict[str, Any] | None:
    """Return model-owned facts proving this procedure branch is not the goal.

    Python does not re-judge the candidate. ``ACTUAL_PROCEDURE`` plus a
    non-accepting branch-exit decision means the independent reviewer itself
    concluded that a genuine procedure does not satisfy the objective. That
    catches component-level mismatches such as front ICC radar vs rear BSM
    radar even though both share the coarse ``radar`` family.
    """
    if not isinstance(review, dict):
        return None
    decision = str(review.get("decision") or "")
    if decision not in _BRANCH_EXIT_DECISIONS:
        return None

    family = _family_mismatch(review)
    if family is not None:
        return {
            "kind": "sensor_family",
            "expected": str(family.get("objective_family") or "requested component"),
            "candidate": str(family.get("candidate_family") or "different component"),
        }

    if str(review.get("classification") or "") == "ACTUAL_PROCEDURE":
        return {
            "kind": "procedure_component",
            "expected": "the requested system/component",
            "candidate": "a different procedure/component",
        }
    return None


def install(module: Any) -> None:
    """Install prompt and per-review branch-exit guidance."""
    if getattr(module, _INSTALLED_ATTR, False):
        return

    original_instruction = module._next_instruction_for_review
    original_prompt = module._system_prompt

    @wraps(original_instruction)
    def next_instruction_with_branch_recovery(review: dict[str, Any]) -> str:
        mismatch = _branch_mismatch(review)
        if mismatch is None:
            return original_instruction(review)
        expected = mismatch["expected"]
        candidate = mismatch["candidate"]
        reviewer_summary = " ".join(str(review.get("evidence_summary") or "").split())[:360]
        label = (
            "SENSOR-FAMILY MISMATCH"
            if mismatch["kind"] == "sensor_family"
            else "WRONG PROCEDURE/COMPONENT BRANCH"
        )
        return (
            f"Independent review proved a {label}: this candidate is {candidate}, while "
            f"the objective is {expected}. "
            + (f"Reviewer: {reviewer_summary} " if reviewer_summary else "")
            + "Do not extract this page again and do not continue deeper under this rejected "
            "procedure/component. Leave this branch now. Backtrack using only the live "
            "rendered controls until you reach a page that exposes alternative ADAS "
            "systems/components, then choose the requested component from that observed "
            "state. Do not invent provider-specific labels, refs, or a fixed menu path."
        )

    @wraps(original_prompt)
    def system_prompt_with_branch_recovery(*args: Any, **kwargs: Any) -> str:
        text = str(original_prompt(*args, **kwargs))
        if _PROMPT_SUFFIX.strip() in text:
            return text
        return text + _PROMPT_SUFFIX

    module._next_instruction_for_review = next_instruction_with_branch_recovery
    module._system_prompt = system_prompt_with_branch_recovery
    setattr(module, _INSTALLED_ATTR, True)
