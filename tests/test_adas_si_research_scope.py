from __future__ import annotations

import pytest

from core.services import adas_si_research
from core.services import calibration_iq_work_prep as work_prep


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
    assert adas_si_research.requires_written_si("Seat Belt") is False
    assert adas_si_research.requires_written_si("Seat Belt Inspection") is False
    assert adas_si_research.requires_written_si("Seat Belt Pretensioner Initialization") is True
    assert adas_si_research.requires_written_si("Seat Belt Buckle Switch Diagnosis") is True


@pytest.mark.asyncio
async def test_generic_seat_belt_is_not_counted_as_missing_si_in_work_prep():
    class Catalog:
        def requirement_coverage(self, labels, **_identity):
            # The field check never reaches library coverage at all.
            assert labels == ["Front Radar"]
            return {
                "requirements": [
                    {"requirement": "Front Radar", "state": "MISSING", "sources": []},
                ]
            }

    coverage = await work_prep._catalog_coverage(  # noqa: SLF001
        Catalog(),
        {
            "repair_order": {"id": "ro-1", "ro_number": "2400911761"},
            "vehicle": {
                "year": 2023,
                "make": "Toyota",
                "model": "Tacoma",
                "vin": "3TMCZ5AN3PM559656",
            },
        },
        {
            "status": "verified",
            "requirements": [
                {"label": "Seat Belt"},
                {"label": "Front Radar"},
            ],
        },
    )

    assert [item["calibration"] for item in coverage] == ["Front Radar"]
    assert coverage[0]["state"] == "MISSING"


def test_objectives_carry_the_shop_label_and_a_goal_free_of_procedure_jargon():
    objectives = adas_si_research.objectives_for(_target())
    radar = objectives[0]
    assert radar["requirement_label"] == "Front Radar"
    assert '"Front Radar"' in radar["topic"]
    # The manufacturer's and ALLDATA's names are X's to work out; the goal
    # must not steer toward a menu with Core's own procedure vocabulary.
    for jargon in ("calibration", "aiming", "initialization"):
        assert jargon not in radar["topic"].casefold()
    pretensioner = objectives[1]
    assert pretensioner["requirement_label"] == "Seat Belt Pretensioner Initialization"


def test_scope_is_native_not_installed():
    import core.services as services_pkg
    from pathlib import Path

    init = (Path(services_pkg.__file__).parent / "__init__.py").read_text(encoding="utf-8")
    for retired in ("scope_guard", "provenance_guard", "presentation_guard", "research_identity", "source_cascade.install"):
        assert retired not in init, retired
