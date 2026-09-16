from __future__ import annotations

from core.services import adas_si_research
from core.services import adas_si_research_scope_guard as guard


def _target():
    return {
        "ro_number": "2400911761",
        "repair_order_id": "ro-1",
        "vehicle": {"year": 2023, "make": "Toyota", "model": "Tacoma"},
        "vehicle_label": "2023 Toyota Tacoma",
        "vin": "3TMCZ5AN3PM559656",
        "calibrations": [
            {"id": "cal-seat", "title": "Seat Belt"},
            {"id": "cal-radar", "title": "Front Radar"},
            {"id": "cal-pretensioner", "title": "Seat Belt Pretensioner Initialization"},
        ],
    }


def test_generic_seat_belt_requirement_is_not_an_si_objective():
    objectives = adas_si_research.objectives_for(_target())
    assert [item["calibration_title"] for item in objectives] == [
        "Front Radar",
        "Seat Belt Pretensioner Initialization",
    ]


def test_explicit_generic_seat_belt_request_is_still_excluded():
    objectives = adas_si_research.objectives_for(
        _target(), systems=["Seat Belt", "Blind Spot Monitor"]
    )
    assert [item["calibration_title"] for item in objectives] == ["Blind Spot Monitor"]


def test_exclusion_is_exact_not_a_broad_seat_belt_substring_rule():
    assert guard.requires_written_si("Seat Belt") is False
    assert guard.requires_written_si("Seat Belt Inspection") is False
    assert guard.requires_written_si("Seat Belt Pretensioner Initialization") is True
    assert guard.requires_written_si("Seat Belt Buckle Switch Diagnosis") is True
