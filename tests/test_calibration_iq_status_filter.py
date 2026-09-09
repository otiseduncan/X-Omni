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

import httpx
import pytest

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


# ---------------------------------------------------------------------------
# the safety net: a filtered zero must not read as the whole truth
# ---------------------------------------------------------------------------
#
# Schema guidance made the bad call rare, not impossible -- the model samples,
# and it still volunteered status=CALIBRATION_IN_PROGRESS in 1 of 6 live runs.
# So the service proves what happened instead of trusting the caller: drop one
# filter at a time and report which one emptied the result.
#
# Measured live with the first round forced to the bad call: with the
# structured diagnostic alone the model answered "0 cars in phase 5" 4 times
# out of 4. With the instruction added it qualified the zero 4 out of 4 and
# volunteered the true 35 in half of them. Both halves are load-bearing.


class _FakeSettings:
    calibration_iq_base_url = "http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq"

    def __init__(self, tmp_path):
        project = tmp_path / "calibration iq"
        project.mkdir(exist_ok=True)
        (project / ".env").write_text("TOOL_SERVICE_TOKEN=t\n", encoding="utf-8")
        self.calibration_iq_project_path = project


def _install(monkeypatch, handler):
    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        ciq.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw)
    )

    async def resolved(_s):
        return _s.calibration_iq_base_url

    monkeypatch.setattr(ciq, "resolve_base", resolved)


def _row(ro, phase="5", status="REPAIR_IN_PROGRESS"):
    return {"id": ro, "ro_number": ro, "phase": phase, "status": status}


def _board(request):
    """Fake CIQ: phase 5 has rows, but none in CALIBRATION_IN_PROGRESS."""
    params = request.url.params
    rows = [_row(f"110{i}") for i in range(3)]
    if params.get("status") == "CALIBRATION_IN_PROGRESS":
        rows = []
    if params.get("phase") not in (None, "5"):
        rows = []
    return httpx.Response(200, json={"items": rows, "count": len(rows)})


@pytest.mark.asyncio
async def test_a_filter_that_empties_the_result_is_named(monkeypatch, tmp_path):
    _install(monkeypatch, _board)
    result = await ciq.summarize_repair_orders(
        _FakeSettings(tmp_path), {"phase": "5", "status": "CALIBRATION_IN_PROGRESS"}
    )

    assert result["count"] == 0
    diagnostic = result["empty_because_of_filters"]
    # Dropping status finds work; dropping phase does not. Only the real
    # culprit is named.
    assert diagnostic["count_without_each_filter"] == {"status": 3}
    assert "status" in result["assistant_instruction"]


@pytest.mark.asyncio
async def test_the_zero_carries_an_obligation_not_just_a_fact(monkeypatch, tmp_path):
    """Evidence alone was ignored 4/4 live; the instruction is required."""
    _install(monkeypatch, _board)
    result = await ciq.summarize_repair_orders(
        _FakeSettings(tmp_path), {"phase": "5", "status": "CALIBRATION_IN_PROGRESS"}
    )

    instruction = result["assistant_instruction"].casefold()
    assert "do not report this 0 as the answer on its own" in instruction
    # Both branches: a filter Otis never asked for, and one he did.
    assert "call this tool again without it" in instruction
    assert "if he did name it" in instruction


@pytest.mark.asyncio
async def test_a_populated_result_carries_no_diagnostic(monkeypatch, tmp_path):
    _install(monkeypatch, _board)
    result = await ciq.summarize_repair_orders(_FakeSettings(tmp_path), {"phase": "5"})

    assert result["count"] == 3
    assert "empty_because_of_filters" not in result
    assert "assistant_instruction" not in result


@pytest.mark.asyncio
async def test_a_genuinely_empty_board_is_not_blamed_on_a_filter(
    monkeypatch, tmp_path
):
    """No filter is at fault when nothing matches with or without it."""
    _install(monkeypatch, lambda _r: httpx.Response(200, json={"items": [], "count": 0}))
    result = await ciq.summarize_repair_orders(
        _FakeSettings(tmp_path), {"phase": "5", "status": "CALIBRATION_IN_PROGRESS"}
    )

    assert result["count"] == 0
    assert "empty_because_of_filters" not in result
    assert "assistant_instruction" not in result


@pytest.mark.asyncio
async def test_an_unfiltered_zero_runs_no_probes(monkeypatch, tmp_path):
    requests: list[str] = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"items": [], "count": 0})

    _install(monkeypatch, handler)
    result = await ciq.summarize_repair_orders(_FakeSettings(tmp_path), {})

    assert result["count"] == 0
    assert len(requests) == 1  # nothing to leave out, so no extra collection
