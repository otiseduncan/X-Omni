"""Target placement geometry: the string-and-arc construction, solved.

Every OEM static-calibration procedure in this library locates the target the
same way. Two reference points are plumbed to the floor, an arc is struck from
each with a length the manual specifies, and the target goes where the arcs
cross. Doing that on a shop floor means tape, string, a plumb bob and a helper.
The arithmetic is a circle-circle intersection, so this solves it instead and
returns the two measurements a tech can lay out with a tape and a square.

Nothing here is general ADAS knowledge, and nothing here invents a dimension.
Every input is a number the caller read out of a specific procedure, and the
caller is expected to cite that document and page. Toyota's own worked example
is the regression test below:

    2018-2020 Camry front camera (sequential recognition), p.11
      point C  267 mm ahead of the front centre point (B)
      point D 1937 mm ahead of B
      arcs of 1000 mm struck from C and from D
      "Confirm that the distance between points K and G ... is 550 mm"

    solve_two_arc(near=267, far=1937, arc=1000)
      -> along 1102.0 mm, offset 550.2 mm

The 0.2 mm is Toyota rounding 550.25 to 550. The same construction, with the
same 1670 mm C-to-D span and 1000 mm arcs, appears in the Highlander, Tacoma,
Tundra, Lexus IS and Lexus ES procedures in this library; only the distance
from the emblem changes per vehicle.

WHY THIS IS NOT A CONVENIENCE: on 2026-09-12 X answered a target placement
question from its own memory and read a clear-zone diagram legend as placement
data -- 16.4 ft instead of 9.84 ft, and a 50 mm target height that was actually
the metal-object limit for the exclusion zone. A tech following it would have
mis-set the target by five feet. Placement answers come from the procedure or
they do not get given.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

MM_PER_INCH = 25.4
MM_PER_FOOT = 304.8


class TargetPlacementError(ValueError):
    """A geometry the procedure cannot produce, or inputs that disagree."""


def _mm(value: Any, name: str, *, minimum: float = 0.0, maximum: float = 20000.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise TargetPlacementError(f"{name} must be a number of millimetres.") from None
    if not math.isfinite(number):
        raise TargetPlacementError(f"{name} must be a finite number of millimetres.")
    if not (minimum <= number <= maximum):
        raise TargetPlacementError(
            f"{name} must be between {minimum:g} and {maximum:g} mm; got {number:g}."
        )
    return number


def _imperial(millimetres: float) -> dict[str, float]:
    return {
        "mm": round(millimetres, 1),
        "inches": round(millimetres / MM_PER_INCH, 2),
        "feet": round(millimetres / MM_PER_FOOT, 2),
    }


@dataclass
class Placement:
    """Where the target goes, in measurements a tape and a square can lay out."""

    along_mm: float
    offset_mm: float
    arc_mm: float
    near_mm: float
    far_mm: float
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "along_centreline": _imperial(self.along_mm),
            "offset_each_side": _imperial(self.offset_mm),
            "confirm_arc": _imperial(self.arc_mm),
            "inputs": {
                "near_point_mm": round(self.near_mm, 1),
                "far_point_mm": round(self.far_mm, 1),
                "arc_mm": round(self.arc_mm, 1),
            },
            "layout": [
                "Plumb the front centre point of the vehicle to the floor (point B).",
                "Run the centreline forward from B.",
                f"Measure {self.along_mm:.0f} mm ({self.along_mm / MM_PER_INCH:.2f} in) "
                "forward along that line and mark point K.",
                f"Square off K and measure {self.offset_mm:.0f} mm "
                f"({self.offset_mm / MM_PER_INCH:.2f} in) to each side for the two "
                "outer placement points.",
                f"Check: each outer point should be {self.arc_mm:.0f} mm from both "
                "reference points on the centreline.",
            ],
            "notes": list(self.notes),
        }


def solve_two_arc(
    *,
    near: Any,
    far: Any,
    arc: Any,
    tolerance_mm: float = 1.0,
) -> Placement:
    """Solve the arc construction shared by these procedures.

    ``near`` and ``far`` are the two reference marks the manual puts on the
    centreline, measured from the vehicle's front centre point. ``arc`` is the
    string length struck from each. The target line crosses the centreline
    midway between them; the outer points sit square to it.
    """

    near_mm = _mm(near, "near point")
    far_mm = _mm(far, "far point")
    arc_mm = _mm(arc, "arc length", minimum=1.0)

    if far_mm <= near_mm:
        raise TargetPlacementError(
            "The far reference point must be further from the vehicle than the "
            f"near one; got near={near_mm:g} mm and far={far_mm:g} mm. Check "
            "which mark the procedure calls which."
        )

    half_span = (far_mm - near_mm) / 2.0
    if arc_mm <= half_span:
        raise TargetPlacementError(
            f"Arcs of {arc_mm:g} mm struck from marks {far_mm - near_mm:g} mm apart "
            "never meet, so this cannot be the geometry the procedure describes. "
            "Re-read the arc length and the two reference distances."
        )

    along_mm = (near_mm + far_mm) / 2.0
    offset_mm = math.sqrt(arc_mm * arc_mm - half_span * half_span)

    notes: list[str] = []
    if offset_mm < tolerance_mm:
        notes.append(
            "The arcs barely meet, so the two outer points collapse onto the "
            "centreline. Re-check the inputs against the procedure."
        )
    return Placement(
        along_mm=along_mm,
        offset_mm=offset_mm,
        arc_mm=arc_mm,
        near_mm=near_mm,
        far_mm=far_mm,
        notes=notes,
    )


def solve_polar(
    *,
    distance: Any,
    angle_degrees: Any,
    angle_from: str = "centreline",
) -> dict[str, Any]:
    """Resolve a sensor-relative distance and angle into tape measurements.

    Some procedures give the reflector position as a distance from the sensor
    and an angle instead of two arcs -- Subaru's blind spot monitor, for
    instance, specifies 1500 mm at 50 degrees. The two conventions in use
    differ by ninety degrees, so ``angle_from`` must say which one the
    procedure means; this never guesses, because guessing the convention puts
    the target on the wrong side of the arc.
    """

    distance_mm = _mm(distance, "reflector distance", minimum=1.0)
    try:
        angle = float(angle_degrees)
    except (TypeError, ValueError):
        raise TargetPlacementError("angle must be a number of degrees.") from None
    if not math.isfinite(angle) or not (0.0 < angle < 90.0):
        raise TargetPlacementError(
            f"angle must be between 0 and 90 degrees; got {angle_degrees!r}."
        )

    convention = str(angle_from or "").strip().lower()
    if convention not in {"centreline", "centerline", "perpendicular"}:
        raise TargetPlacementError(
            "angle_from must be 'centreline' (angle opens from the vehicle's "
            "long axis) or 'perpendicular' (angle opens from square to it). "
            "The procedure's illustration decides which; do not assume."
        )

    radians = math.radians(angle)
    if convention == "perpendicular":
        along_mm = distance_mm * math.sin(radians)
        offset_mm = distance_mm * math.cos(radians)
    else:
        along_mm = distance_mm * math.cos(radians)
        offset_mm = distance_mm * math.sin(radians)

    return {
        "along_from_sensor": _imperial(along_mm),
        "offset_from_sensor": _imperial(offset_mm),
        "confirm_direct": _imperial(distance_mm),
        "inputs": {
            "distance_mm": round(distance_mm, 1),
            "angle_degrees": angle,
            "angle_from": convention,
        },
        "layout": [
            "Plumb the sensor centre to the floor and mark it.",
            f"Measure {along_mm:.0f} mm ({along_mm / MM_PER_INCH:.2f} in) along the "
            "vehicle from that mark.",
            f"Square off and measure {offset_mm:.0f} mm "
            f"({offset_mm / MM_PER_INCH:.2f} in) away from the vehicle.",
            f"Check: the direct line back to the sensor mark should be "
            f"{distance_mm:.0f} mm ({distance_mm / MM_PER_INCH:.2f} in).",
        ],
        "notes": [
            "Confirm the angle convention against the procedure's illustration "
            "before placing the target. The two conventions differ by 90 degrees."
        ],
    }


def solve(args: dict[str, Any]) -> dict[str, Any]:
    """Tool entry point. Requires the caller to name where the numbers came from."""

    if not isinstance(args, dict):
        raise TargetPlacementError("Target placement arguments must be an object.")

    source = str(args.get("source_document") or "").strip()
    page = args.get("source_page")
    if not source:
        raise TargetPlacementError(
            "source_document is required: name the procedure these dimensions "
            "were read from. This tool does not supply dimensions, and a "
            "placement answer without a cited procedure is not usable."
        )

    method = str(args.get("method") or "two_arc").strip().lower()
    if method == "two_arc":
        placement = solve_two_arc(
            near=args.get("near_point_mm"),
            far=args.get("far_point_mm"),
            arc=args.get("arc_mm"),
        )
        payload = placement.as_dict()
    elif method == "polar":
        payload = solve_polar(
            distance=args.get("distance_mm"),
            angle_degrees=args.get("angle_degrees"),
            angle_from=args.get("angle_from") or "centreline",
        )
    else:
        raise TargetPlacementError(
            "method must be 'two_arc' (two reference marks and an arc length) or "
            "'polar' (a distance and an angle from the sensor)."
        )

    payload["method"] = method
    payload["source"] = {"document": source, "page": page}
    payload["computed_from_caller_supplied_dimensions"] = True
    payload["verify_before_use"] = (
        "These are arithmetic on the dimensions supplied. Check them against the "
        "procedure's own confirmation dimension before placing a target."
    )
    return payload
