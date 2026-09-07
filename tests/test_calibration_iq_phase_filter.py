"""Phase filtering must hold on the shared Calibration IQ query path.

The reported failure was "show me a list of only Phase 5" answering with 56
Warner Robins ROs spanning phases 1, 4, 5, 6, 7 and unphased vehicles, narrated
as "56 active vehicle(s) in phase ." -- the blank phase being the giveaway that
no filter had been applied.

That was fixed in calibration_iq_work_prep._phase_list, which is only one of
the two ways a phase-scoped question reaches Calibration IQ. The model can just
as reasonably choose calibration_iq_read/calibration_iq_summary, which share
query_repair_orders and sent whatever phase value they were handed straight
upstream with no verification of what came back.

Confirmed live against the real service: phase="5" returns 36 rows all in phase
5, phase="Phase 5" is rejected as invalid_filter, and an absent phase returns
all 126 rows across every phase. So the two gaps worth closing on the shared
path are normalization ("Phase 5" is a value Otis and the model both use) and
proof that the rows actually came back scoped.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from core.services import calibration_iq as ciq


@dataclass
class FakeSettings:
    calibration_iq_base_url: str
    calibration_iq_project_path: Path


@pytest.fixture
def settings(tmp_path: Path) -> FakeSettings:
    project = tmp_path / "calibration iq"
    project.mkdir()
    (project / ".env").write_text(
        "TOOL_SERVICE_TOKEN=test-service-token\n", encoding="utf-8"
    )
    return FakeSettings(
        "http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq",
        project,
    )


def _install_transport(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        ciq.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    async def resolved(_settings):
        return _settings.calibration_iq_base_url

    monkeypatch.setattr(ciq, "resolve_base", resolved)


def _row(ro: str, phase: object) -> dict:
    return {
        "id": f"id-{ro}",
        "ro_number": ro,
        "phase": phase,
        "status": "In Progress",
        "shop": "Warner Robins",
    }


def _collection(rows: list[dict]) -> httpx.Response:
    # "count" is the authoritative upstream total; _collect pages until the
    # unique set reaches it, so a wrong key here reads as an incomplete answer.
    return httpx.Response(200, json={"items": rows, "count": len(rows)})


# --------------------------------------------------------------------------
# normalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (5, "5"),
        ("5", "5"),
        ("5.0", "5"),
        (" 5 ", "5"),
        ("Phase 5", "5"),
        ("phase 5", "5"),
        ("PHASE5", "5"),
        ("", None),
        (None, None),
    ],
)
def test_phase_values_normalize_to_one_wire_token(raw, expected) -> None:
    assert ciq.normalize_phase(raw) == expected


def test_a_named_phase_is_passed_through_rather_than_dropped() -> None:
    """An unrecognized label must reach the service, not silently vanish.

    Dropping it would turn a filtered request back into a full-board read --
    the exact bug this module exists to prevent.
    """
    assert ciq.normalize_phase("Teardown") == "Teardown"


@pytest.mark.asyncio
async def test_spoken_phase_wording_reaches_the_service_as_a_bare_number(
    monkeypatch, settings
) -> None:
    """The live service rejects "Phase 5" with invalid_filter."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("phase"))
        return _collection([_row("2400711774", 5)])

    _install_transport(monkeypatch, handler)
    result = await ciq.read_repair_orders(settings, {"phase": "Phase 5"})

    assert result["status"] == "verified"
    assert seen == ["5"]
    assert result["filters"]["phase"] == "5"


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_scoped_read_returns_only_rows_in_the_requested_phase(
    monkeypatch, settings
) -> None:
    _install_transport(
        monkeypatch,
        lambda _r: _collection([_row("2400711774", 5), _row("2400711775", "5.0")]),
    )
    result = await ciq.read_repair_orders(settings, {"phase": "5"})

    assert result["status"] == "verified"
    assert result["count"] == 2


@pytest.mark.asyncio
async def test_an_ignored_phase_filter_fails_closed_instead_of_listing_the_board(
    monkeypatch, settings
) -> None:
    """The original bug, reproduced at the shared query path."""
    mixed = [
        _row("2400711774", 5),
        _row("2400711775", 1),
        _row("2400711776", 4),
        _row("2400711777", 7),
        _row("2400711778", None),
    ]
    _install_transport(monkeypatch, lambda _r: _collection(mixed))
    result = await ciq.read_repair_orders(settings, {"phase": "5"})

    assert result["status"] == "filter_mismatch"
    assert result["count"] is None
    assert result["rows"] == []
    assert result["requested_phase"] == "5"
    assert result["mismatch_count"] == 4
    # A blank phase in the narrated answer was how this surfaced to Otis.
    assert "Phase 5" in result["message"]


@pytest.mark.asyncio
async def test_an_unphased_row_cannot_pass_as_a_phase_match(
    monkeypatch, settings
) -> None:
    _install_transport(
        monkeypatch,
        lambda _r: _collection([_row("2400711774", 5), _row("2400711775", None)]),
    )
    result = await ciq.read_repair_orders(settings, {"phase": "5"})

    assert result["status"] == "filter_mismatch"
    assert result["mismatch_count"] == 1


@pytest.mark.asyncio
async def test_a_scoped_count_is_verified_the_same_way_as_a_list(
    monkeypatch, settings
) -> None:
    """summarize_repair_orders reports a number with no rows to inspect.

    An unfiltered count is harder to catch by eye than an unfiltered list, so
    it must not be exempt from the same proof.
    """
    _install_transport(
        monkeypatch,
        lambda _r: _collection([_row("2400711774", 5), _row("2400711775", 1)]),
    )
    result = await ciq.summarize_repair_orders(settings, {"phase": "5"})

    assert result["status"] == "filter_mismatch"
    assert result["count"] is None


@pytest.mark.asyncio
async def test_an_unfiltered_read_is_left_alone(monkeypatch, settings) -> None:
    """No phase requested means no phase claim to verify."""
    mixed = [_row("2400711774", 5), _row("2400711775", 1), _row("2400711776", None)]
    _install_transport(monkeypatch, lambda _r: _collection(mixed))
    result = await ciq.read_repair_orders(settings, {})

    assert result["status"] == "verified"
    assert result["count"] == 3
    assert "phase" not in result["filters"]
