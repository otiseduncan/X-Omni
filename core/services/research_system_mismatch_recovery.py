"""Give X an explicit recovery move after a reviewer-proven system mismatch.

The semantic system guard correctly vetoes a camera-for-radar or radar-for-
camera acceptance, but the generic Navigator instruction says only "keep
searching from the current page".  On a provider tree that can leave the model
inside the wrong sensor's sub-tree, where every subsequent article is still the
wrong family.  The 2026-09-14 Nissan rear-side-radar QA run demonstrated this:
X reached the front ICC Distance Sensor alignment procedure, correctly rejected
it, but never escaped that family to reach the rear blind-spot radar.

This module does not choose a menu, label, ref, OEM path, or document.  It turns
X's own structured reviewer fact (``system_family_check``) into a navigation
constraint: leave the current sensor-family branch and return to a live page
where alternative ADAS systems/components can be chosen, then reason again.
"""

from __future__ import annotations

from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_system_mismatch_recovery_installed__"
_PROMPT_SUFFIX = (
    "\n\nSYSTEM-FAMILY RECOVERY CONTRACT: If independent review says the candidate is "
    "for a different ADAS sensor family than the objective, that is a branch-level "
    "correction, not an invitation to inspect more documents under the same sensor. "
    "Do not extract that page again and do not keep drilling deeper in the rejected "
    "family. Use the live controls to backtrack until you reach a page that exposes "
    "alternative ADAS systems/components, then choose the requested family from the "
    "rendered state. You still decide the path from what is actually on screen; never "
    "invent provider-specific labels or refs."
)


def _mismatch(review: Any) -> dict[str, Any] | None:
    if not isinstance(review, dict) or review.get("decision") != "CONTINUE_SEARCH":
        return None
    check = review.get("system_family_check")
    if not isinstance(check, dict):
        return None
    expected = str(check.get("objective_family") or "").strip()
    candidate = str(check.get("candidate_family") or "").strip()
    if not expected or not candidate or expected == candidate:
        return None
    return check


def install(module: Any) -> None:
    """Install prompt and per-review recovery guidance for wrong-family pages."""
    if getattr(module, _INSTALLED_ATTR, False):
        return

    original_instruction = module._next_instruction_for_review
    original_prompt = module._system_prompt

    @wraps(original_instruction)
    def next_instruction_with_family_recovery(review: dict[str, Any]) -> str:
        base = original_instruction(review)
        check = _mismatch(review)
        if check is None:
            return base
        expected = str(check.get("objective_family") or "requested")
        candidate = str(check.get("candidate_family") or "different")
        return (
            f"{base} The reviewer proved a SENSOR-FAMILY MISMATCH: the current branch is "
            f"{candidate}, while the objective is {expected}. Leave this sensor-family branch "
            "now. Backtrack using the live page until you can see alternative ADAS "
            "systems/components, then choose the requested family from that rendered state. "
            "Do not continue deeper under this rejected family and do not extract this page again."
        )

    @wraps(original_prompt)
    def system_prompt_with_family_recovery(*args: Any, **kwargs: Any) -> str:
        text = str(original_prompt(*args, **kwargs))
        if _PROMPT_SUFFIX.strip() in text:
            return text
        return text + _PROMPT_SUFFIX

    module._next_instruction_for_review = next_instruction_with_family_recovery
    module._system_prompt = system_prompt_with_family_recovery
    setattr(module, _INSTALLED_ATTR, True)
