from __future__ import annotations

from core.orchestrator import loop
from core.services import adas_si_background_truth_guard as guard


def test_background_review_contract_uses_matching_si_status_read():
    assert "kind=adas_si_research" in loop.BACKGROUND_REVIEW_PREFIX
    assert "kind=adas_map_sweep" in loop.BACKGROUND_REVIEW_PREFIX
    assert "Never say background work is completed while the record says it is still running" in (
        loop.BACKGROUND_REVIEW_PREFIX
    )


def test_running_status_arms_language_truth_review():
    token = guard._STATUS_RESULT.set(None)
    try:
        guard.note_status(
            {
                "service": "X Omni",
                "action": "adas_si_research_status",
                "status": "running",
                "message": "Still running.",
            }
        )
        assert loop.calibration_iq_mutation_truth_review_required([], []) is True
    finally:
        guard._STATUS_RESULT.reset(token)


def test_unrelated_result_does_not_arm_background_truth_review():
    token = guard._STATUS_RESULT.set(None)
    try:
        guard.note_status(
            {"action": "adas_map_sweep_status", "status": "running"}
        )
        assert guard._STATUS_RESULT.get() is None
    finally:
        guard._STATUS_RESULT.reset(token)


def test_fail_closed_running_status_never_says_complete():
    text = guard._fail_closed_status(
        {
            "action": "adas_si_research_status",
            "status": "running",
            "message": (
                "Service-information research for RO 2400911761 is running: "
                "1 of 5 procedure objectives finished."
            ),
        }
    )
    assert "running" in text.casefold()
    assert "1 of 5" in text
    assert "completed" not in text.casefold()
