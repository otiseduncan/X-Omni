from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.services import research_auto_acquire


def _verified_result(local_count: int = 0) -> dict:
    return {
        "query": "2024 Ford Transit forward facing calibration procedure",
        "requested_manufacturer": "Ford",
        "adas_si": {"result_count": local_count, "hits": []},
        "alldata": {
            "verified": True,
            "searched": True,
            "query_submitted": True,
            "vehicle_selection": {"selected": True},
            "vehicle": {"label": "2024 Ford Transit"},
            "topic": "forward facing calibration",
            "result_title": "Forward Facing Camera Calibration",
            "relevance_score": 12,
            "page_text": "Exact OEM procedure text " * 20,
        },
        "captures": [],
    }


def test_missing_local_exact_procedure_is_acquisition_candidate():
    assert research_auto_acquire.acquisition_candidate(_verified_result(0)) is True


def test_existing_local_exact_procedure_is_not_reacquired():
    assert research_auto_acquire.acquisition_candidate(_verified_result(1)) is False


def test_unverified_alldata_result_is_not_acquired():
    result = _verified_result(0)
    result["alldata"]["verified"] = False
    assert research_auto_acquire.acquisition_candidate(result) is False


def test_existing_capture_deduplicates_by_alldata_url(tmp_path: Path):
    folder = tmp_path / "Acquired" / "ALLDATA"
    folder.mkdir(parents=True)
    pdf = folder / "2024 Ford Transit Camera.pdf"
    sidecar = folder / "2024 Ford Transit Camera.source.json"
    pdf.write_bytes(b"pdf")
    sidecar.write_text(
        json.dumps({"url": "https://my.alldata.com/repair/#/article/123"}),
        encoding="utf-8",
    )

    found = research_auto_acquire._existing_capture(
        folder, "https://my.alldata.com/repair/#/article/123"
    )
    assert found is not None
    assert found["pdf"] == pdf
    assert found["sidecar"] == sidecar


@pytest.mark.asyncio
async def test_full_research_auto_saves_verified_missing_procedure(monkeypatch):
    result = _verified_result(0)

    async def previous(_args, *, adas, browser):
        return dict(result)

    class Browser:
        def __init__(self):
            self.calls = []

        async def _capture_to_adas(self, args):
            self.calls.append(args)
            return {
                "status": "success",
                "saved": True,
                "relative_path": "Acquired/ALLDATA/2024 Ford Transit Camera.pdf",
            }

    browser = Browser()
    monkeypatch.setattr(research_auto_acquire, "_PREVIOUS_FULL", previous)
    output = await research_auto_acquire.full_research_with_acquisition(
        {"query": result["query"]},
        adas=SimpleNamespace(),
        browser=browser,
    )

    assert output["auto_acquired_to_adas_si"] is True
    assert browser.calls
    assert browser.calls[0]["vehicle"] == "2024 Ford Transit"
    assert output["captures"][0]["source"] == "ALLDATA"
    assert output["captures"][0]["auto_acquired"] is True
