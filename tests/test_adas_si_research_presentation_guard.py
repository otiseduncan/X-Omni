from __future__ import annotations

from core.services import adas_si_research_presentation_guard as guard


def test_failed_candidate_with_url_gets_calibration_identity_and_reason():
    row = {
        "calibration": "Blind Spot Monitor",
        "outcome": "not_found",
        "title": "\u200b",
        "source_url": "https://my.alldata.com/repair/#/candidate",
        "reason": "Semantic review did not accept the candidate.",
        "incomplete_reasons": [],
    }

    guard._repair_row(row)

    assert row["title"] == "Blind Spot Monitor — last reviewed candidate"
    assert row["incomplete_reasons"] == [
        "Semantic review did not accept the candidate."
    ]


def test_successful_titled_row_is_not_rewritten():
    row = {
        "calibration": "Front Radar",
        "outcome": "attached",
        "title": "Millimeter Wave Radar Sensor - Adjustment",
        "source_url": "https://my.alldata.com/repair/#/radar",
    }

    guard._repair_row(row)

    assert row["title"] == "Millimeter Wave Radar Sensor - Adjustment"
    assert "incomplete_reasons" not in row
