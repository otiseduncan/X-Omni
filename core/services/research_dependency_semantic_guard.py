"""Clarify reviewer semantics for required SI dependencies.

The independent semantic reviewer remains the only layer that decides whether a
candidate page requires another document. This installer adds no keyword
classifier, browser routing, or Python promotion/rejection rule. It only sharpens
the reviewer's existing contract after a live Toyota OCS run treated conditional
DTC/removal/installation repair branches as mandatory dependencies of the
requested calibration work.
"""

from __future__ import annotations

from typing import Any

_INSTALLED_ATTR = "__xomni_dependency_semantic_guard_installed__"

_PROMPT_SUFFIX = (
    " Dependency scope is the NORMAL PATH needed to complete the research objective, "
    "not every document named anywhere on the page. Use FOLLOW_DEPENDENCY or "
    "ACCEPT_WITH_DEPENDENCIES only when the candidate's own text unconditionally directs "
    "the technician to another named document as the requested procedure itself or as an "
    "unavoidable step in the normal completion path. Conditional fault-handling and repair "
    "branches are not dependencies merely because they are referenced: for example, a DTC "
    "check/clear, diagnostic branch, removal, installation, replacement, or repair article "
    "that applies only if an inspection finds a fault does not have to be collected to "
    "satisfy a normal calibration/initialization objective. A general inspection, system "
    "description, or related-information page that merely points to those repair branches "
    "should normally be CONTINUE_SEARCH or REJECT rather than FOLLOW_DEPENDENCY. Preserve "
    "a dependency only when the quoted sentence itself proves that the other document must "
    "be performed or consulted for the ordinary successful procedure, not just for an "
    "abnormal condition or optional repair path."
)


def install(review_module: Any) -> None:
    """Append the normal-path dependency contract exactly once."""
    if getattr(review_module, _INSTALLED_ATTR, False):
        return
    prompt = str(getattr(review_module, "REVIEW_SYSTEM_PROMPT", ""))
    if _PROMPT_SUFFIX.strip() not in prompt:
        review_module.REVIEW_SYSTEM_PROMPT = prompt + _PROMPT_SUFFIX
    setattr(review_module, _INSTALLED_ATTR, True)
