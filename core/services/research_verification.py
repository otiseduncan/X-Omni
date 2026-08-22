"""Canonical evidence contract for externally-researched collision sources.

Domain/session presence is not evidence. A licensed source such as ALLDATA is
only "verified" once: the requested vehicle identity was confirmed through a
bounded UI signal (not a substring match against the whole page, which also
contains a year picker and a Recent Vehicles list and will match almost any
vehicle by luck), a vehicle-scoped query was actually submitted, and the
returned page still carries that same vehicle's identity alongside matched
topic terms.

This is the *only* place allowed to decide the meaning of "verified" for a
licensed-provider research result. Every caller -- the deterministic
vehicle-first search, the model-driven agent loop, and any future navigation
path -- must route its evidence through this module rather than recomputing
its own notion of "verified" against the current URL.
"""

from __future__ import annotations

from typing import Any, Optional


def evaluate_alldata_claim(
    *,
    vehicle: dict[str, Any],
    vehicle_state: dict[str, Any],
    query_submitted: bool,
    matched_terms: Optional[list[str]],
    relevance_score: int,
    result_page_text: str,
    min_relevance: int = 2,
) -> dict[str, Any]:
    """Return {"verified": bool, "reason": str | None} for one ALLDATA attempt.

    Every gate below corresponds to one of the claims a truthful research
    report has to be able to make: the vehicle was selected, a query ran
    against that vehicle's context, the result is on-topic, and the result
    page still describes the requested vehicle rather than having drifted
    (redirect, session reset, stale tab) to something else.
    """
    if not isinstance(vehicle_state, dict) or not vehicle_state.get("selected"):
        reason = (
            (isinstance(vehicle_state, dict) and vehicle_state.get("reason"))
            or "ALLDATA vehicle selection was not confirmed."
        )
        return {"verified": False, "reason": str(reason)}

    if not query_submitted:
        return {"verified": False, "reason": "No vehicle-scoped query was submitted."}

    matched_terms = list(matched_terms or [])
    if not matched_terms:
        return {
            "verified": False,
            "reason": "The result page did not contain any of the requested topic's terms.",
        }

    if int(relevance_score or 0) < min_relevance:
        return {
            "verified": False,
            "reason": f"Relevance score {relevance_score} is below the verification threshold ({min_relevance}).",
        }

    identity_tokens = [
        str(vehicle.get(key) or "").casefold() for key in ("year", "make") if vehicle.get(key)
    ]
    folded_result = str(result_page_text or "").casefold()
    if identity_tokens and not all(token in folded_result for token in identity_tokens):
        return {
            "verified": False,
            "reason": "The result page no longer carries the requested vehicle's identity -- research may have drifted off the selected vehicle.",
        }

    return {"verified": True, "reason": None}


def unselected_source_claim(reason: str) -> dict[str, Any]:
    """Shared shape for a source that never proved vehicle selection at all.

    Used by search paths (e.g. a plain keyword search box) that have no
    concept of vehicle-scoped selection in the first place -- such a search
    can never be more than "attempted", regardless of what the URL says.
    """
    return evaluate_alldata_claim(
        vehicle={},
        vehicle_state={"selected": False, "reason": reason},
        query_submitted=False,
        matched_terms=None,
        relevance_score=0,
        result_page_text="",
    )
