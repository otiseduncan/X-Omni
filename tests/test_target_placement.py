"""Target placement geometry, checked against the procedures it came from.

The worked examples below are read out of documents in X:\\ADAS SI. Where a
procedure states its own confirmation dimension, that number is the assertion:
the solver has to reproduce what the OEM says a tech should measure.
"""

import math

import pytest

from core.services import target_placement as tp


# ------------------------------------------------------------------ two arc


def test_toyota_camry_front_camera_reproduces_the_manual_confirmation():
    # 2018-2020 Camry, CRUISE CONTROL: FRONT CAMERA: ADJUSTMENT
    # (SEQUENTIAL RECOGNITION), p.11: point C at 267 mm and point D at 1937 mm
    # ahead of the front centre point, arcs of 1000 mm from each, and
    # "Confirm that the distance between points K and G ... is 550 mm".
    placement = tp.solve_two_arc(near=267, far=1937, arc=1000)

    assert round(placement.along_mm, 1) == 1102.0
    assert abs(placement.offset_mm - 550.0) < 0.5, placement.offset_mm


@pytest.mark.parametrize(
    "label,near,far",
    [
        ("2018-2020 Camry", 267, 1937),
        ("2021-2026 Highlander", 316, 1986),
        ("2020 Tacoma", 446, 2116),
        ("2022 Lexus IS", 259, 1929),
        ("2022 Lexus ES 350", 241, 1911),
        ("2024 Tundra", 248, 1918),
    ],
)
def test_every_documented_vehicle_lands_on_the_same_550mm_offset(label, near, far):
    # Across all six procedures in the library the C-to-D span is 1670 mm with
    # 1000 mm arcs, so the offset is constant and only the distance from the
    # emblem moves. A change here means a procedure was misread.
    placement = tp.solve_two_arc(near=near, far=far, arc=1000)

    assert abs(placement.offset_mm - 550.0) < 0.5, f"{label}: {placement.offset_mm}"
    assert round(placement.along_mm, 1) == round((near + far) / 2, 1)


def test_the_midpoint_really_is_equidistant_from_both_reference_marks():
    # Independent check of the construction rather than of the formula: the
    # computed point must sit on both arcs.
    placement = tp.solve_two_arc(near=267, far=1937, arc=1000)
    for reference in (placement.near_mm, placement.far_mm):
        span = placement.along_mm - reference
        assert abs(math.hypot(span, placement.offset_mm) - 1000.0) < 0.01


def test_arcs_that_cannot_meet_are_refused_rather_than_approximated():
    # 1000 mm arcs struck from marks 4000 mm apart never cross. Returning a
    # plausible-looking number here is how a target ends up in the wrong place.
    with pytest.raises(tp.TargetPlacementError, match="never meet"):
        tp.solve_two_arc(near=100, far=4100, arc=1000)


def test_reference_marks_given_in_the_wrong_order_are_refused():
    with pytest.raises(tp.TargetPlacementError, match="further from the vehicle"):
        tp.solve_two_arc(near=1937, far=267, arc=1000)


def test_non_numeric_and_out_of_range_dimensions_are_refused():
    with pytest.raises(tp.TargetPlacementError):
        tp.solve_two_arc(near="about a metre", far=1937, arc=1000)
    with pytest.raises(tp.TargetPlacementError):
        tp.solve_two_arc(near=267, far=1937, arc=-5)


# -------------------------------------------------------------------- polar


def test_polar_resolves_a_distance_and_angle_into_tape_measurements():
    # Subaru Ascent blind spot monitor, p.1: reflector 1500 mm from the sensor
    # at 50 degrees.
    result = tp.solve_polar(distance=1500, angle_degrees=50, angle_from="centreline")

    along = result["along_from_sensor"]["mm"]
    offset = result["offset_from_sensor"]["mm"]
    assert abs(along - 1500 * math.cos(math.radians(50))) < 0.1
    assert abs(offset - 1500 * math.sin(math.radians(50))) < 0.1
    # The direct line back to the sensor is the procedure's own check.
    assert abs(math.hypot(along, offset) - 1500) < 0.1


def test_the_two_angle_conventions_are_ninety_degrees_apart_and_must_be_named():
    centreline = tp.solve_polar(distance=1500, angle_degrees=50, angle_from="centreline")
    perpendicular = tp.solve_polar(distance=1500, angle_degrees=50, angle_from="perpendicular")

    # Swapping the convention swaps the two measurements. Guessing it puts the
    # target roughly a metre from where the procedure wants it.
    assert abs(
        centreline["along_from_sensor"]["mm"] - perpendicular["offset_from_sensor"]["mm"]
    ) < 0.1
    assert centreline["along_from_sensor"]["mm"] != perpendicular["along_from_sensor"]["mm"]

    with pytest.raises(tp.TargetPlacementError, match="do not assume"):
        tp.solve_polar(distance=1500, angle_degrees=50, angle_from="whichever")


def test_polar_always_carries_the_convention_warning():
    result = tp.solve_polar(distance=1500, angle_degrees=50, angle_from="centreline")
    assert any("convention" in note for note in result["notes"])


# --------------------------------------------------------------- tool entry


def test_the_tool_refuses_to_answer_without_a_cited_procedure():
    # The failure this whole module exists for: a confident placement answer
    # with nothing behind it.
    with pytest.raises(tp.TargetPlacementError, match="source_document is required"):
        tp.solve({"method": "two_arc", "near_point_mm": 267, "far_point_mm": 1937, "arc_mm": 1000})


def test_the_tool_returns_layout_steps_and_echoes_its_source():
    result = tp.solve({
        "method": "two_arc",
        "near_point_mm": 267,
        "far_point_mm": 1937,
        "arc_mm": 1000,
        "source_document": "2018-2020CamryLKAS.pdf",
        "source_page": 11,
    })

    assert result["source"] == {"document": "2018-2020CamryLKAS.pdf", "page": 11}
    assert result["along_centreline"]["mm"] == 1102.0
    assert abs(result["offset_each_side"]["mm"] - 550.0) < 0.5
    assert result["computed_from_caller_supplied_dimensions"] is True
    assert result["verify_before_use"]
    assert len(result["layout"]) >= 4


def test_an_unknown_method_is_refused():
    with pytest.raises(tp.TargetPlacementError, match="method must be"):
        tp.solve({"method": "eyeball", "source_document": "x.pdf"})


def test_millimetres_inches_and_feet_agree():
    result = tp.solve({
        "method": "two_arc",
        "near_point_mm": 267,
        "far_point_mm": 1937,
        "arc_mm": 1000,
        "source_document": "2018-2020CamryLKAS.pdf",
        "source_page": 11,
    })
    along = result["along_centreline"]
    assert abs(along["inches"] - along["mm"] / 25.4) < 0.01
    assert abs(along["feet"] - along["mm"] / 304.8) < 0.01
