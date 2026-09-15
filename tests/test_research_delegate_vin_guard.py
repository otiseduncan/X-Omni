from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services import research_delegate


async def _no_local(_args):
    return {"status": "no_result", "results": [], "records": []}


@pytest.mark.asyncio
async def test_delegate_does_not_open_alldata_without_exact_vin():
    navigator_calls = []

    async def navigator(**kwargs):
        navigator_calls.append(kwargs)
        raise AssertionError("Navigator must not start without an exact VIN")

    handler = research_delegate.make_delegate_research(
        SimpleNamespace(),
        adas_search=_no_local,
        knowledge_search=_no_local,
        navigator_search=navigator,
        public_search=None,
    )
    result = await handler(
        {
            "objective": "Front radar calibration procedure",
            "vehicle": {"year": 2023, "make": "Toyota", "model": "Tacoma"},
            "sources": ["alldata"],
        }
    )

    assert navigator_calls == []
    assert result["alldata_vehicle_identity_required"] is True
    assert result["source_order"] == ["alldata"]
    assert result["sources_checked"] == []
    assert result["source_ledger"] == [
        {
            "source": "alldata",
            "attempted": False,
            "verified": False,
            "status": "vehicle_identity_required",
            "reason": (
                "ALLDATA navigation requires the exact 17-character VIN. No ALLDATA task "
                "was created. For a Calibration IQ repair order, use stage_action "
                "research_si so the VIN comes from the authoritative RO/ADAS Map handoff."
            ),
        }
    ]


@pytest.mark.asyncio
async def test_delegate_rejects_malformed_vin_as_missing_identity():
    navigator_calls = []

    async def navigator(**kwargs):
        navigator_calls.append(kwargs)
        return {"verified": False}

    handler = research_delegate.make_delegate_research(
        SimpleNamespace(),
        adas_search=_no_local,
        knowledge_search=_no_local,
        navigator_search=navigator,
    )
    result = await handler(
        {
            "objective": "Front camera calibration procedure",
            "vehicle": {
                "year": 2023,
                "make": "Toyota",
                "model": "Tacoma",
                "vin": "NOT-A-VIN",
            },
            "sources": ["alldata"],
        }
    )
    assert navigator_calls == []
    assert result["source_ledger"][0]["status"] == "vehicle_identity_required"


@pytest.mark.asyncio
async def test_delegate_passes_valid_exact_vin_to_alldata_navigator():
    navigator_calls = []

    async def navigator(**kwargs):
        navigator_calls.append(kwargs)
        return {
            "verified": True,
            "status": "verified",
            "complete": True,
            "task_id": "task-1",
            "evidence_title": "Millimeter Wave Radar Sensor Assembly Adjustment",
            "source_url": "https://example.invalid/procedure",
            "extracted_text": "Procedure text",
        }

    handler = research_delegate.make_delegate_research(
        SimpleNamespace(),
        adas_search=_no_local,
        knowledge_search=_no_local,
        navigator_search=navigator,
    )
    result = await handler(
        {
            "objective": "Front radar calibration procedure",
            "vehicle": {
                "year": 2023,
                "make": "Toyota",
                "model": "Tacoma",
                "vin": "1N6ED1EK0PN123456",
            },
            "sources": ["alldata"],
        }
    )

    assert result["verified"] is True
    assert len(navigator_calls) == 1
    assert navigator_calls[0]["target"]["vin"] == "1N6ED1EK0PN123456"
