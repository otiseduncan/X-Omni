"""A Calibration IQ status filter must come from Otis, never from the model.

Observed live: "how many cars are in phase 5" answered "0 cars ... with the
status CALIBRATION_IN_PROGRESS". The model had volunteered a status the
request never mentioned. Nothing failed -- CALIBRATION_IN_PROGRESS is a legal
value that currently matches nothing -- so a filter nobody asked for silently
hid all 36 phase-5 ROs behind a confident zero.

That is the same class of defect the phase filter already had, and it gets the
same remedy: the schema states the never-infer contract explicitly, and the
enum removes the guesswork that produced a wasted HTTP 422 round trip on
"IN_PROGRESS" before the model retried.

Deliberately not fixed by inspecting the user's words. Core cannot know what
Otis meant; it can only make the contract unambiguous to the model that does.
"""

from __future__ import annotations

from core.services import calibration_iq as ciq
from core.tools.registry import TOOL_SCHEMAS

FILTER_READS = ("calibration_iq_summary", "calibration_iq_read")


def _status_param(tool: str) -> dict:
    return TOOL_SCHEMAS[tool]["parameters"]["properties"]["status"]


def test_status_vocabulary_matches_the_service_contract() -> None:
    """registry mirrors the service constant; drift here is a silent bug.

    They are duplicated rather than imported because importing the service
    into the registry pulls in services/__init__, whose install() hooks import
    the registry back.
    """
    for tool in FILTER_READS:
        assert _status_param(tool)["enum"] == list(ciq.WORKFLOW_STATUSES)


def test_advertised_statuses_are_the_ones_calibration_iq_accepts() -> None:
    # Proved against the live service's own HTTP 422 enum rejection.
    assert ciq.WORKFLOW_STATUSES[0] == "NEW_ARRIVAL"
    assert "CALIBRATION_IN_PROGRESS" in ciq.WORKFLOW_STATUSES
    assert "REPAIR_IN_PROGRESS" in ciq.WORKFLOW_STATUSES
    assert "ARCHIVED" in ciq.WORKFLOW_STATUSES
    # "IN_PROGRESS" is the value the model invented; it is a 422, not a status.
    assert "IN_PROGRESS" not in ciq.WORKFLOW_STATUSES


def test_status_carries_the_same_never_infer_contract_as_phase() -> None:
    for tool in FILTER_READS:
        status_text = _status_param(tool)["description"].casefold()
        assert "only when the user's current request names one" in status_text
        assert "never infer or default a status" in status_text
        # The specific wrong instinct that caused the bug: reaching for a
        # status to express "active" when the read already excludes finished
        # work by default.
        assert "active" in status_text
        assert "include_completed" in status_text

        phase_text = TOOL_SCHEMAS[tool]["parameters"]["properties"]["phase"][
            "description"
        ].casefold()
        assert "never infer or default a phase" in phase_text


def test_status_is_optional_so_an_unqualified_count_reads_the_whole_board() -> None:
    for tool in FILTER_READS:
        assert "status" not in (TOOL_SCHEMAS[tool]["parameters"].get("required") or [])


def test_status_remains_a_pass_through_filter_at_the_service_boundary() -> None:
    """CIQ stays the authority on status values.

    The enum is advertised to the model, not enforced here: a status the
    service adds later must still reach the API and be judged there.
    """
    assert "status" in ciq.READ_PARAMS
