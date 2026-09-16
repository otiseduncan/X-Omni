from __future__ import annotations

from core.services import adas_si_research as research


def _row(**values):
    return research._row_identity(dict(values))  # noqa: SLF001


def test_failed_candidate_with_url_gets_calibration_identity_and_reason():
    row = _row(
        calibration="Blind Spot Monitor",
        outcome="not_found",
        title="\u200e\u200b",
        source_url="https://my.alldata.com/repair/#/candidate",
        reason="Semantic review did not accept the candidate.",
        incomplete_reasons=[],
    )
    assert row["title"] == "Blind Spot Monitor — last reviewed candidate"
    assert row["incomplete_reasons"] == ["Semantic review did not accept the candidate."]
    assert row["failure_reason"] == "Semantic review did not accept the candidate."


def test_failed_titled_row_keeps_title_and_surfaces_reason():
    row = _row(
        calibration="Blind Spot Monitor",
        outcome="not_found",
        title="Operation Check",
        source_url="https://my.alldata.com/repair/#/candidate",
        reason="Reviewer classified the page as supporting only.",
        incomplete_reasons=[],
        review={"decision": "CONTINUE_SEARCH", "classification": "REQUIRED_SUPPORTING_PROCEDURE"},
    )
    assert row["title"] == "Operation Check"
    assert row["incomplete_reasons"] == ["Reviewer classified the page as supporting only."]
    assert row["reviewer_result"] == "CONTINUE_SEARCH / REQUIRED_SUPPORTING_PROCEDURE"


def test_a_failure_with_no_reason_still_says_what_happened():
    row = _row(calibration="Front Camera", outcome="uncertain", title=None, source_url=None)
    assert row["title"] is None
    assert row["failure_reason"] == research.OUTCOME_LABELS["uncertain"]


def test_successful_titled_row_is_not_rewritten_and_reports_attachment():
    row = _row(
        calibration="Front Radar",
        outcome="attached",
        title="Millimeter Wave Radar Sensor - Adjustment",
        source_url="https://my.alldata.com/repair/#/radar",
        attachments=[{"attached": True, "calibration_item_id": "cal-radar"}],
        review={"decision": "ACCEPT", "classification": "ACTUAL_PROCEDURE"},
    )
    assert row["title"] == "Millimeter Wave Radar Sensor - Adjustment"
    assert "failure_reason" not in row
    assert row["attachment_status"] == "attached"
    assert row["reviewer_result"] == "ACCEPT / ACTUAL_PROCEDURE"


def test_public_view_rows_and_groups_always_carry_identity():
    service = research.AdasSiResearchService(object(), object(), client=object())
    record = {
        "job_id": "j",
        "state": "completed",
        "scope_label": "RO 1",
        "result": {"counts": {"attached": 1, "not_found": 1}},
        "objectives": [
            {
                "objective_id": "a",
                "calibration_title": "Front Radar",
                "status": "finished",
                "outcome": "attached",
                "result": {"verified": True, "complete": True, "title": "Radar - Adjustment", "source_url": "u", "review": {"decision": "ACCEPT"}},
                "attachments": [{"attached": True}],
            },
            {
                "objective_id": "b",
                "calibration_title": "Blind Spot Monitor",
                "status": "finished",
                "outcome": "not_found",
                "result": {"verified": False, "title": "", "source_url": "https://x/y", "reason": "Not accepted."},
                "attachments": [],
            },
        ],
    }
    view = service.public_view(record)
    grouped = [row for group in view["groups"] for row in group["objectives"]]
    for row in view["objectives"] + grouped:
        assert row["calibration"]
        assert row["outcome_label"]
        assert row["title"] or row["outcome"] != "attached"
    failed = next(row for row in view["objectives"] if row["objective_id"] == "b")
    assert failed["title"] == "Blind Spot Monitor — last reviewed candidate"
    assert failed["failure_reason"] == "Not accepted."
