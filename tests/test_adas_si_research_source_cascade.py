from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.services import adas_si_research as research
from core.services import research_semantic_review


OBJECTIVE = {
    "objective_id": "bsm-1",
    "ro_number": "2400711902",
    "repair_order_id": "ro-id-1",
    "calibration_id": "cal-bsm",
    "calibration_title": "Blind Spot Monitor",
    "system": "Blind Spot Monitor",
    "topic": "Blind Spot Monitor calibration / aiming / initialization procedure",
    "vehicle": {
        "year": 2025,
        "make": "Kia",
        "model": "K4",
        "trim": "EX",
    },
    "vin": "KNAF24A28S5000001",
}


class FakeAdas:
    def __init__(self, root: Path, *, title: str, text: str):
        self.root = root
        self.relative = "2025/Kia/K4/" + title + ".pdf"
        self.path = root / self.relative
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"%PDF-1.4\nlocal-test")
        self.text = text
        self.search_calls: list[dict[str, Any]] = []

    def model_search(self, args: dict[str, Any]) -> dict[str, Any]:
        self.search_calls.append(args)
        return {
            "status": "success",
            "results": [
                {
                    "title": self.path.stem,
                    "relative_path": self.relative,
                    "page": 1,
                    "url": f"/api/adas-si/document?path={self.relative}",
                    "excerpt": self.text,
                }
            ],
            "matched_documents": [],
        }

    def resolve_relative(self, relative: str) -> Path:
        assert relative == self.relative
        return self.path

    def _pages(self, path: Path):
        assert path == self.path
        return [(1, self.text)]

    def render_page(self, path: Path, page: int, width: int):
        assert path == self.path and page == 1 and width == 1100
        return b"fake-png"


class FakeNavigator:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "status": "verified",
            "verified": True,
            "complete": True,
            "captured": True,
            "evidence_title": "Blind Spot Radar Calibration",
            "source_url": "https://my.alldata.com/bsm",
            "semantic_review": {
                "classification": "ACTUAL_PROCEDURE",
                "procedure_type": "BLIND_SPOT_RADAR",
                "vehicle_match": "MATCHES",
                "decision": "ACCEPT",
                "confidence": 0.95,
                "evidence_summary": "BSM calibration steps.",
                "dependencies": [],
            },
            "documents": [],
            "dependencies": [],
            "incomplete_reasons": [],
            "task_ids": ["nav-1"],
        }


def service(tmp_path: Path, adas: FakeAdas, navigator: FakeNavigator):
    return research.AdasSiResearchService(
        SimpleNamespace(),
        object(),
        client=object(),
        router=None,
        adas=adas,
        navigator_search=navigator,
        attach=lambda *_args, **_kwargs: None,
    )


@pytest.mark.asyncio
async def test_actual_local_procedure_satisfies_objective_without_opening_alldata(
    tmp_path, monkeypatch
):
    adas = FakeAdas(
        tmp_path,
        title="Blind Spot Radar Calibration",
        text="Perform blind spot radar aiming. Follow these calibration steps.",
    )
    navigator = FakeNavigator()

    async def accepted(**_kwargs):
        return {
            "classification": "ACTUAL_PROCEDURE",
            "procedure_type": "BLIND_SPOT_RADAR",
            "vehicle_match": "MATCHES",
            "evidence": {},
            "dependencies": [],
            "decision": "ACCEPT",
            "confidence": 0.96,
            "evidence_summary": "This is the BSM calibration procedure.",
            "malformed": False,
        }

    monkeypatch.setattr(research_semantic_review, "review_candidate", accepted)
    result = await service(tmp_path, adas, navigator)._research(dict(OBJECTIVE))

    assert result["verified"] is True
    assert result["complete"] is True
    assert result["source"] == "adas_si"
    assert navigator.calls == []
    assert result["documents"][0]["artifact"]["relative_path"] == adas.relative
    assert result["documents"][0]["artifact"]["storage_policy"] == "year/make/model"
    assert adas.search_calls[0]["vehicle"] == {
        "year": 2025,
        "make": "Kia",
        "model": "K4",
        "trim": "EX",
    }


@pytest.mark.asyncio
async def test_related_bumper_requirement_does_not_suppress_vin_bound_bsm_lookup(
    tmp_path, monkeypatch
):
    adas = FakeAdas(
        tmp_path,
        title="Blind Spot Monitor Bumper Removal Requirements",
        text="After rear bumper removal, inspect the blind spot monitor system.",
    )
    navigator = FakeNavigator()

    async def related_only(**_kwargs):
        return {
            "classification": "GENERAL_DESCRIPTION",
            "procedure_type": "NOT_A_PROCEDURE",
            "vehicle_match": "MATCHES",
            "evidence": {},
            "dependencies": [],
            "decision": "CONTINUE_SEARCH",
            "confidence": 0.94,
            "evidence_summary": "Requirement information, not calibration steps.",
            "malformed": False,
        }

    monkeypatch.setattr(research_semantic_review, "review_candidate", related_only)
    result = await service(tmp_path, adas, navigator)._research(dict(OBJECTIVE))

    assert result["verified"] is True
    assert len(navigator.calls) == 1
    assert navigator.calls[0]["provider"] == "alldata"
    assert navigator.calls[0]["target"]["vin"] == OBJECTIVE["vin"]
    assert navigator.calls[0]["target"]["year"] == 2025
    assert navigator.calls[0]["target"]["make"] == "Kia"
    assert navigator.calls[0]["target"]["model"] == "K4"


@pytest.mark.asyncio
async def test_local_procedure_with_unresolved_dependency_still_escalates(
    tmp_path, monkeypatch
):
    adas = FakeAdas(
        tmp_path,
        title="Blind Spot Radar Calibration",
        text="Perform BSM aiming, then perform the referenced initialization.",
    )
    navigator = FakeNavigator()

    async def incomplete(**_kwargs):
        return {
            "classification": "ACTUAL_PROCEDURE",
            "procedure_type": "BLIND_SPOT_RADAR",
            "vehicle_match": "MATCHES",
            "evidence": {},
            "dependencies": [
                {
                    "title": "Blind Spot Initialization",
                    "reason": "Required after aiming",
                    "quote": "then perform the referenced initialization",
                }
            ],
            "decision": "ACCEPT_WITH_DEPENDENCIES",
            "confidence": 0.91,
            "evidence_summary": "Primary exists but the referenced initialization is still required.",
            "malformed": False,
        }

    monkeypatch.setattr(research_semantic_review, "review_candidate", incomplete)
    await service(tmp_path, adas, navigator)._research(dict(OBJECTIVE))

    assert len(navigator.calls) == 1
    assert navigator.calls[0]["target"]["vin"] == OBJECTIVE["vin"]
