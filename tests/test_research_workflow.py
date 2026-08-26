from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.services import adas_si as adas_mod
from core.services import research_workflow
from core.tools.registry import TOOL_SCHEMAS


def test_explicit_toyota_search_never_returns_hyundai(tmp_path: Path, monkeypatch):
    root = tmp_path / "ADAS SI"
    root.mkdir()
    toyota = root / "2023 Toyota Highlander Blind Spot Calibration.pdf"
    hyundai = root / "2023 Hyundai Palisade Blind Spot Calibration.pdf"
    toyota.write_bytes(b"pdf")
    hyundai.write_bytes(b"pdf")

    inventory = adas_mod.SourceInventory(root)
    monkeypatch.setattr(
        inventory,
        "documents",
        lambda: [
            {**adas_mod.describe_document(root, toyota), "_path": toyota},
            {**adas_mod.describe_document(root, hyundai), "_path": hyundai},
        ],
    )
    results = inventory.matching_documents("Toyota blind spot monitor recycled module", limit=8)
    assert results
    assert {item["descriptor"]["make"] for item in results} == {"Toyota"}


def test_broad_blind_spot_search_can_still_span_manufacturers(tmp_path: Path, monkeypatch):
    root = tmp_path / "ADAS SI"
    root.mkdir()
    toyota = root / "2023 Toyota Highlander Blind Spot Calibration.pdf"
    hyundai = root / "2023 Hyundai Palisade Blind Spot Calibration.pdf"
    toyota.write_bytes(b"pdf")
    hyundai.write_bytes(b"pdf")
    inventory = adas_mod.SourceInventory(root)
    monkeypatch.setattr(
        inventory,
        "documents",
        lambda: [
            {**adas_mod.describe_document(root, toyota), "_path": toyota},
            {**adas_mod.describe_document(root, hyundai), "_path": hyundai},
        ],
    )
    results = inventory.matching_documents("blind spot calibration", limit=8)
    assert {item["descriptor"]["make"] for item in results} == {"Toyota", "Hyundai"}


def test_fixed_source_sequence_is_not_exposed_as_a_model_tool_action():
    research_workflow.install()
    actions = TOOL_SCHEMAS["collision_research"]["parameters"]["properties"]["action"]["enum"]
    assert "full_research" not in actions


def test_fixed_summary_never_claims_unverified_alldata_search():
    result = {
        "source_ledger": [
            {"source": "ADAS SI", "verified": True, "result_count": 2},
            {"source": "ALLDATA", "verified": False, "searched": False, "reason": "No search field"},
            {"source": "Public OEM web", "verified": True, "searched": True, "result_count": 3},
        ]
    }
    summary = research_workflow.fixed_summary(result)
    assert "ALLDATA: not verified as searched" in summary
    assert "Public OEM web: searched" in summary


@pytest.mark.asyncio
async def test_full_workflow_emits_three_lane_source_ledger(monkeypatch):
    class FakeAdas:
        def search(self, args):
            assert "Toyota" in args["query"]
            return {
                "status": "success",
                "results": [
                    {
                        "title": "2023 Toyota Highlander BSM",
                        "page": 2,
                        "excerpt": "Toyota blind spot monitor calibration information",
                        "vehicle": {"make": "Toyota", "model": "Highlander"},
                    }
                ],
            }

    async def fake_alldata(_browser, query):
        return {
            "attempted": True,
            "searched": True,
            "verified": True,
            "query_submitted": True,
            "query": query,
            "url": "https://my.alldata.com/search",
            "title": "ALLDATA search",
            "relevance_score": 2,
            "page_text": "Toyota recycled blind spot module",
        }

    async def fake_public(query, make, **_kwargs):
        assert make == "Toyota"
        return {
            "searched": True,
            "verified": True,
            "sources": [{"title": "Toyota Collision Pros", "url": "https://collisionpros.toyota.com/x"}],
            "read_results": [],
            "result_count": 1,
        }

    monkeypatch.setattr(research_workflow, "search_alldata", fake_alldata)
    monkeypatch.setattr(research_workflow, "search_public_oem", fake_public)
    result = await research_workflow.full_research(
        {"query": "Research whether Toyota permits a recycled blind spot monitor module.", "preserve": False},
        adas=FakeAdas(),
        browser=SimpleNamespace(),
    )
    assert result["status"] == "success"
    assert result["external_search_verified"] is True
    assert [row["source"] for row in result["source_ledger"]] == [
        "ADAS SI", "ALLDATA", "Public OEM web"
    ]
    assert all(row["verified"] is True for row in result["source_ledger"])
    assert result["adas_si"]["hits"][0]["vehicle"]["make"] == "Toyota"


@pytest.mark.asyncio
async def test_ledger_surfaces_verification_reason_when_a_result_was_found_but_not_trusted(monkeypatch):
    """A run that got as far as a result (past vehicle selection, past
    submitting a query) but failed evaluate_alldata_claim()'s checks sets
    verification_reason, not reason -- the ledger (and therefore the chat UI)
    must not silently drop that explanation."""
    class FakeAdas:
        def search(self, args):  # noqa: ARG002
            return {"status": "no_result", "results": []}

    async def fake_alldata(_browser, _query):
        return {
            "attempted": True,
            "searched": True,
            "verified": False,
            "verification_reason": "The result page no longer carries the requested vehicle's identity.",
            "query_submitted": True,
            "vehicle": {"label": "2019 Ford F-150"},
        }

    async def fake_public(_query, _make, **_kwargs):
        return {"searched": False, "verified": False, "sources": [], "read_results": [], "result_count": 0}

    monkeypatch.setattr(research_workflow, "search_alldata", fake_alldata)
    monkeypatch.setattr(research_workflow, "search_public_oem", fake_public)
    result = await research_workflow.full_research(
        {"query": "2019 Ford F-150 360 camera calibration procedure", "preserve": False},
        adas=FakeAdas(),
        browser=SimpleNamespace(),
    )

    alldata_row = next(row for row in result["source_ledger"] if row["source"] == "ALLDATA")
    assert alldata_row["verified"] is False
    assert alldata_row["reason"] == "The result page no longer carries the requested vehicle's identity."
    assert "not verified as searched" in research_workflow.fixed_summary(result)
    assert "identity" in research_workflow.fixed_summary(result).casefold()
