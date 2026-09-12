"""Manufacturer reference sheets have to survive naming a vehicle.

Otis filed bumper requirement sheets for a dozen manufacturers into
X:\\ADAS SI. Asking X about a bumper on a Honda then went out to the web,
because the sheets were invisible to the search: they are filed by make group
and carry no parsed year, make or model, so the vehicle-identity guard
discarded every one of them the moment a vehicle was named. "bumper
requirements" found eight sheets; "Honda bumper requirements" found none.

Two separate failures produced that, and both are asserted here: the guard
rejected them on identity, and the ranking gave them nothing to compete with
so they fell off the end of the candidate list.
"""

import pytest

from core.services import adas_identity_guard as guard
from core.services import adas_si


def _sheet(group: str, title: str) -> dict:
    return {
        "storage_class": "reference",
        "relative_path": f"Reference\\Calibration Requirements\\{group}\\{title}.pdf",
        "title": title,
        "year": None,
        "make": None,
        "model": None,
    }


def _calculator(title: str) -> dict:
    return {
        "storage_class": "reference",
        "relative_path": f"Reference\\Calculators\\General\\{title}.pdf",
        "title": title,
        "year": None,
        "make": None,
        "model": None,
    }


def _vehicle_doc() -> dict:
    return {
        "storage_class": "service_information",
        "relative_path": "2023\\Toyota\\Camry\\2023 Toyota Camry SE Front Radar.pdf",
        "title": "2023 Toyota Camry SE Front Radar",
        "year": 2023,
        "make": "Toyota",
        "model": "Camry",
    }


# ------------------------------------------------------- the identity gate


@pytest.mark.parametrize(
    "group,make",
    [
        ("Honda Acura", "Honda"),
        ("Honda Acura", "Acura"),
        ("Toyota Lexus", "Toyota"),
        ("Toyota Lexus", "Lexus"),
        ("Hyundai Kia Genesis", "Kia"),
        ("Nissan Infiniti", "Infiniti"),
        ("Ford Lincoln", "Lincoln"),
        ("Jaguar Land Rover", "Land Rover"),
        ("Volkswagen Audi", "Audi"),
        ("Subaru", "Subaru"),
    ],
)
def test_a_sheet_clears_the_gate_for_every_make_its_group_names(group, make):
    descriptor = _sheet(group, f"{group} Front Radar Bumper Requirements")
    assert guard.reference_covers_make(descriptor, make, adas_si) is True


@pytest.mark.parametrize("make", ["Chevrolet", "GMC", "Buick", "Cadillac"])
def test_general_motors_covers_its_divisions(make):
    # "General Motors" is the one group folder that does not spell out the
    # makes it covers, so the alias table carries them.
    descriptor = _sheet("General Motors", "General Motors Front Radar Bumper Requirements")
    assert guard.reference_covers_make(descriptor, make, adas_si) is True


def test_a_sheet_does_not_clear_the_gate_for_an_unrelated_make():
    # The guard exists so another vehicle's evidence never reaches the field.
    # Widening it for reference sheets must not widen it to everything.
    descriptor = _sheet("Honda Acura", "Honda Acura ADAS Calibration Requirements")
    assert guard.reference_covers_make(descriptor, "Toyota", adas_si) is False
    assert guard.reference_covers_make(descriptor, "Subaru", adas_si) is False


def test_a_general_calculator_is_not_make_gated():
    # The calculators take their inputs from whatever manual is in hand, so
    # they are not tied to a make; relevance scoring still decides them.
    descriptor = _calculator("Front Camera Calculator")
    assert guard.reference_makes(descriptor, adas_si) == frozenset()
    assert guard.reference_covers_make(descriptor, "Toyota", adas_si) is True


def test_a_vehicle_document_is_never_treated_as_a_reference_sheet():
    assert guard.is_reference_document(_vehicle_doc()) is False
    assert guard.reference_covers_make(_vehicle_doc(), "Toyota", adas_si) is False


def test_year_and_model_do_not_gate_a_sheet_that_spans_them():
    # These sheets are deliberately multi-year and cover a whole family;
    # holding them to a model year is what hid them.
    descriptor = _sheet("Toyota Lexus", "Toyota Lexus Blind Spot Monitor Bumper Requirements")
    assert guard.descriptor_matches_query(
        descriptor, "2023 Toyota Camry front bumper removal", adas_si
    ) is True


# ----------------------------------------------------------- the ranking


@pytest.mark.parametrize(
    "group,query",
    [
        ("Honda Acura", "honda front bumper removal requirements"),
        ("Toyota Lexus", "2023 toyota camry front bumper requirement"),
        ("General Motors", "chevrolet front radar bumper requirements"),
        ("General Motors", "gmc front radar bumper requirements"),
        ("Subaru", "subaru bumper requirements"),
    ],
)
def test_naming_the_make_earns_a_sheet_real_identity_credit(group, query):
    # Without this the sheet scores two or three on title words alone, against
    # the fifteen a vehicle manual collects, and never reaches the candidate
    # cut on a make with a deep library.
    sheet = _sheet(group, f"{group} Front Radar Bumper Requirements")
    assert adas_si._reference_make_bonus(sheet, query) > 0


def test_a_sheet_earns_nothing_from_an_unrelated_make():
    sheet = _sheet("Honda Acura", "Honda Acura ADAS Calibration Requirements")
    assert adas_si._reference_make_bonus(sheet, "subaru bumper requirements") == 0


def test_a_general_calculator_earns_no_make_credit():
    assert adas_si._reference_make_bonus(_calculator("Front Camera Calculator"), "toyota camry") == 0


def test_a_vehicle_document_earns_no_reference_credit():
    assert adas_si._reference_make_bonus(_vehicle_doc(), "2023 toyota camry") == 0


def test_the_group_folder_is_what_identifies_a_sheet():
    assert adas_si.reference_group_of(_sheet("Ford Lincoln", "x")) == "ford lincoln"
    assert adas_si.reference_group_of(_calculator("Front Camera Calculator")) == ""
    assert adas_si.reference_group_of(_vehicle_doc()) == ""


def test_the_gate_and_the_ranking_read_the_same_alias_table():
    # They disagreed once: the gate let a Chevrolet query through to the
    # General Motors sheet while the ranking gave it nothing, so it passed the
    # filter and then fell off the list anyway.
    for group, aliases in adas_si.REFERENCE_MAKE_ALIASES.items():
        sheet = _sheet(group.title(), f"{group.title()} Requirements")
        for make in aliases:
            assert guard.reference_covers_make(sheet, make, adas_si) is True
            assert adas_si._reference_make_bonus(sheet, f"{make} bumper requirements") > 0


def test_the_candidate_cap_leaves_room_for_the_identity_filter():
    # The guard asks for 40 candidates so its vehicle filter has something
    # left after discarding other makes; a base cap of 25 silently overrode
    # that and starved the filter on any make with a deep library. Read the
    # base implementation from the file: at runtime the attribute is the
    # guard's wrapper, not this function.
    import pathlib
    import re

    source = pathlib.Path(adas_si.__file__).read_text(encoding="utf-8")
    body = source.split("def matching_documents", 1)[1].split("\n    def ", 1)[0]
    cap = re.search(r"scored\[: max\(1, min\(limit, (\d+)\)\)\]", body)
    assert cap, "the candidate cap moved; check it still exceeds the guard's request"
    assert int(cap.group(1)) >= 40, (
        f"base cap {cap.group(1)} is below the 40 the identity guard requests"
    )
