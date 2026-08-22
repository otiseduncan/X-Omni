from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.services import adas_identity_guard
from core.services import adas_si as adas_mod
from core.services import research_workflow


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


def test_post_collision_request_routes_to_composite_workflow():
    prompt = (
        "Research whether Toyota permits the use of a recycled blind spot monitor module. "
        "Check ADAS SI first, then ALLDATA, then official Toyota collision sources if necessary. "
        "Preserve any authoritative documentation we're missing in ADAS SI."
    )
    assert research_workflow.full_research_request(prompt) is True
    assert research_workflow.preserve_requested(prompt) is True
    assert research_workflow.focused_query(prompt).startswith("whether Toyota permits")


def test_plain_calibration_iq_ro_research_is_not_hijacked():
    assert research_workflow.full_research_request("Research this RO and attach the OEM evidence") is False


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

    async def fake_public(query, make):
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
