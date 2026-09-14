from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.services import adas_si_research as research
from core.state.db import Store


def _verified_ro(*, vin: str) -> dict[str, Any]:
    return {
        "status": "verified",
        "repair_order": {
            "RO": "2400711902",
            "id": "ro-id-1902",
            "Phase": 1,
            "Shop": "Warner Robins",
        },
        "raw": {
            "id": "ro-id-1902",
            "ro_number": "2400711902",
            "vin": vin,
            "vehicle": {"year": 2025, "make": "Kia", "model": "K4"},
            "calibrations": [
                {
                    "id": "cal-radar",
                    "title": "Front Radar Sensor - SCC / AEB / FCW",
                    "determination": "REQUIRED",
                }
            ],
        },
    }


def test_target_from_read_requires_a_valid_17_character_vin() -> None:
    assert research.target_from_read(_verified_ro(vin="")) is None
    assert research.target_from_read(_verified_ro(vin="12345")) is None

    target = research.target_from_read(_verified_ro(vin="3KPFT4DE4SE215941"))
    assert target is not None
    assert target["vin"] == "3KPFT4DE4SE215941"
    assert target["vehicle"] == {"year": 2025, "make": "Kia", "model": "K4"}


@pytest.mark.asyncio
async def test_background_research_never_opens_navigator_without_ciq_vin(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite")
    conversation_id = store.create_conversation("vin guard")
    message_id = store.add_message(conversation_id, "user", "research the radar procedure")
    navigator_calls: list[dict[str, Any]] = []

    async def ro_reader(_args: dict[str, Any]) -> dict[str, Any]:
        return _verified_ro(vin="")

    async def navigator_search(**kwargs: Any) -> dict[str, Any]:
        navigator_calls.append(kwargs)
        raise AssertionError("Navigator must not run without an exact VIN")

    service = research.AdasSiResearchService(
        SimpleNamespace(),
        store,
        client=object(),
        navigator_search=navigator_search,
        ro_reader=ro_reader,
    )
    context = {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "tool_call_id": "call-vin-guard",
        "user_id": "local-dev",
        "role": "owner",
    }

    result = await service.start(
        {"repair_order_id": "2400711902", research.INVOCATION_KEY: context}
    )

    assert result["status"] == "no_targets"
    assert result["executed"] is False
    assert result["work_complete"] is False
    assert navigator_calls == []
    assert "valid 17-character VIN" in result["message"]
    assert any("valid 17-character VIN" in problem for problem in result["problems"])
    store.close()
