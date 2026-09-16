"""Keep supporting documents from satisfying a primary SI objective.

The independent reviewer is allowed to classify a page as a
``REQUIRED_SUPPORTING_PROCEDURE`` because dependencies genuinely exist. But a
supporting page is not the primary calibration/aiming/initialization procedure.
The live Tacoma run exposed the missing boundary when an Occupant Classification
System initialization/support page was allowed to close the primary ``Seat Belt``
objective.

A second live Tacoma run exposed the opposite classification risk: Toyota's Blind
Spot Monitor ``Operation Check`` is itself an executable beam-axis inspection /
confirmation procedure. The title does not make it supporting material. The
reviewer must classify from the page's actual steps and role in the requested
operation, not from labels such as ``Operation Check`` or ``Inspection``.

This guard does not decide page meaning. It trusts the reviewer's own
classification and applies the workflow role Core already knows:

* primary task -> only ``ACTUAL_PROCEDURE`` may be accepted;
* dependency task -> ``REQUIRED_SUPPORTING_PROCEDURE`` may be accepted because
  that task was created specifically to resolve a required supporting document.

A primary supporting-page acceptance is downgraded to ``CONTINUE_SEARCH`` so X
keeps navigating instead of stopping on useful-but-insufficient evidence.
"""

from __future__ import annotations

from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_primary_procedure_guard_v1__"
_ACCEPTING = frozenset({"ACCEPT", "ACCEPT_WITH_DEPENDENCIES"})
_SUPPORTING = "REQUIRED_SUPPORTING_PROCEDURE"

_PROMPT_SUFFIX = (
    " Workflow role matters. When the evidence packet has no dependency_context, you are "
    "reviewing the PRIMARY research objective: REQUIRED_SUPPORTING_PROCEDURE is useful "
    "supporting evidence but cannot satisfy that primary objective by itself. For a primary "
    "task, ACCEPT/ACCEPT_WITH_DEPENDENCIES only when classification is ACTUAL_PROCEDURE; if "
    "the page is only supporting material, use CONTINUE_SEARCH (or FOLLOW_DEPENDENCY only "
    "when the page itself names the actual required procedure). Do NOT decide that a page is "
    "merely supporting material because its OEM title says Operation Check, Inspection, Beam "
    "Axis Inspection, Confirmation, Verification, or another check-oriented label. If that "
    "page itself contains the executable setup, scan-tool actions, target/measurement steps, "
    "beam-axis work, adjustment/confirmation steps, and completion criteria that directly "
    "perform or verify the requested calibration/aiming/initialization operation, classify "
    "it as ACTUAL_PROCEDURE. Judge the page's functional role from its steps, not its title. "
    "A check page that only tells the technician whether another procedure is needed remains "
    "supporting material. When dependency_context is present, the task exists specifically "
    "to retrieve a required supporting document, so a REQUIRED_SUPPORTING_PROCEDURE may be "
    "accepted there."
)


def apply_primary_procedure_boundary(verdict: Any, *, objective: Any) -> Any:
    if not isinstance(verdict, dict):
        return verdict
    if verdict.get("decision") not in _ACCEPTING:
        return verdict
    if verdict.get("classification") != _SUPPORTING:
        return verdict
    if isinstance(objective, dict) and str(objective.get("dependency_context") or "").strip():
        return verdict

    out = dict(verdict)
    original = str(out.get("decision") or "")
    out["original_decision"] = out.get("original_decision", original)
    out["decision"] = "CONTINUE_SEARCH"
    inconsistent = list(out.get("inconsistent") or [])
    inconsistent.append(
        "a REQUIRED_SUPPORTING_PROCEDURE cannot satisfy the primary service-information objective"
    )
    out["inconsistent"] = inconsistent
    out["primary_procedure_check"] = {
        "classification": out.get("classification"),
        "dependency_task": False,
        "veto_only": True,
    }
    prior = str(out.get("evidence_summary") or "").strip()
    out["evidence_summary"] = (
        "This page is supporting material, not the primary actual procedure. "
        "Keep searching for the requested calibration/aiming/initialization procedure."
        + (f" Reviewer evidence: {prior}" if prior else "")
    )[:1200]
    return out


def install(review_module: Any, navigator_module: Any | None = None) -> None:
    if getattr(review_module, _INSTALLED_ATTR, False):
        if navigator_module is not None:
            navigator_module.review_candidate = review_module.review_candidate
        return

    prompt = str(getattr(review_module, "REVIEW_SYSTEM_PROMPT", ""))
    if _PROMPT_SUFFIX.strip() not in prompt:
        review_module.REVIEW_SYSTEM_PROMPT = prompt + _PROMPT_SUFFIX

    original = review_module.review_candidate

    @wraps(original)
    async def review_candidate_with_primary_boundary(*args: Any, **kwargs: Any):
        verdict = await original(*args, **kwargs)
        return apply_primary_procedure_boundary(
            verdict,
            objective=kwargs.get("objective"),
        )

    review_module.review_candidate = review_candidate_with_primary_boundary
    setattr(review_module, _INSTALLED_ATTR, True)

    # Navigator imports review_candidate by name, so refresh its bound reference
    # after wrapping the semantic-review module.
    if navigator_module is not None:
        navigator_module.review_candidate = review_module.review_candidate
