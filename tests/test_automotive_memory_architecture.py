from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.orchestrator import prompt as prompt_mod
from core.services import adas_si_research_source_cascade as cascade
from core.services import research_delegate
from core.services import research_evidence_contract as contract
from core.services import research_knowledge_promotion as promotion


VEHICLE = {"year": 2025, "make": "Kia", "model": "K4"}


def _procedure_review(**overrides: Any) -> dict[str, Any]:
    review = {
        "malformed": False,
        "decision": "ACCEPT",
        "classification": "ACTUAL_PROCEDURE",
        "procedure_type": "BLIND_SPOT_RADAR",
        "vehicle_match": "MATCHES",
        "objective_match": "EXACT_MATCH",
        "requirement_system": "Blind Spot Collision Warning rear corner radar",
        "page_system": "Blind Spot Collision Warning rear corner radar",
        "same_unit": "SAME",
        "page_structure": "STEPS_FOR_ONE_SYSTEM",
        "evidence": {
            "prerequisites": "PRESENT",
            "tools_or_equipment": "PRESENT",
            "physical_setup": "PRESENT",
            "geometry_or_measurements": "PRESENT",
            "scan_tool_steps": "PRESENT",
            "execution_steps": "PRESENT",
            "completion_criteria": "PRESENT",
        },
        "dependencies": [],
        "confidence": 0.98,
        "evidence_summary": "The page contains the complete rear corner radar calibration procedure.",
    }
    review.update(overrides)
    return review


def _procedure_evaluation(**review_overrides: Any) -> dict[str, Any]:
    return {
        "outcome": contract.SATISFIED,
        "deliverable": "procedure",
        "stage": "whole_procedure",
        "review": _procedure_review(**review_overrides),
        "source_answer": "Complete rear corner radar calibration procedure.",
        "unresolved": [],
    }


def _finding() -> dict[str, Any]:
    return {
        "source": "adas_si",
        "relative_path": r"2025\Kia\K4\2025 Kia K4 BSM Calibration.pdf",
        "title": "K4 Blind Spot Collision Warning Radar Calibration",
        "page": 1,
        "library_vehicle": VEHICLE,
    }


def test_satisfied_procedure_is_eligible_for_the_same_semantic_cache_as_answers() -> None:
    candidate = {"text": "PREPARATION\n1. Position the rear radar target.\n2. Run BCW calibration.\nPASS"}
    assert (
        promotion.promotion_refusal(
            vehicle=VEHICLE,
            finding=_finding(),
            evaluation=_procedure_evaluation(),
            candidate=candidate,
        )
        is None
    )


def test_procedure_promotion_rejects_semantically_inconsistent_acceptance() -> None:
    candidate = {"text": "Complete procedure text."}
    evaluation = _procedure_evaluation(same_unit="DIFFERENT")
    assert "same unit" in str(
        promotion.promotion_refusal(
            vehicle=VEHICLE,
            finding=_finding(),
            evaluation=evaluation,
            candidate=candidate,
        )
    ).casefold()

    evaluation = _procedure_evaluation()
    evaluation["review"]["evidence"] = dict(evaluation["review"]["evidence"])
    evaluation["review"]["evidence"]["execution_steps"] = "MISSING_OR_UNCERTAIN"
    assert "execution steps" in str(
        promotion.promotion_refusal(
            vehicle=VEHICLE,
            finding=_finding(),
            evaluation=evaluation,
            candidate=candidate,
        )
    ).casefold()


def test_procedure_cache_record_is_source_backed_and_identified_as_procedure() -> None:
    source_text = (
        "BLIND SPOT COLLISION WARNING CALIBRATION\n"
        "Preparation: place target behind left rear corner.\n"
        "Run the calibration with the diagnostic tool and confirm PASS."
    )
    record = promotion.build_record(
        objective="Blind Spot Monitor calibration",
        vehicle=VEHICLE,
        system="Blind Spot Monitor",
        component="Rear corner radar",
        finding=_finding(),
        evaluation=_procedure_evaluation(),
        local_path=r"X:\ADAS SI\2025\Kia\K4\2025 Kia K4 BSM Calibration.pdf",
        content_sha256="a" * 64,
        candidate={"text": source_text},
    )

    assert record["lifecycle"] == "verified"
    assert record["requirement"]["requirement_type"] == "procedure"
    assert record["requirement"]["text"] == _finding()["title"]
    assert "procedure_summary" not in record["requirement"]
    assert record["procedures"][0]["procedure_identifier"] == "BLIND_SPOT_RADAR"
    assert record["repair_event"] == {
        "event_type": "adas_si_evidence",
        "description": _finding()["relative_path"] + "#page=1",
    }
    evidence = record["evidence"][0]
    assert evidence["excerpt"] in source_text
    assert evidence["source"]["metadata"]["deliverable"] == "procedure"
    assert evidence["source"]["metadata"]["research_objective"] == "Blind Spot Monitor calibration"
    assert evidence["source"]["content_sha256"] == "a" * 64


def test_cache_identity_ignores_question_wording_vin_and_trim() -> None:
    source_text = "Complete BCW rear corner radar calibration procedure with PASS criteria."
    common = {
        "system": "Blind Spot Monitor",
        "component": "Rear corner radar",
        "finding": _finding(),
        "evaluation": _procedure_evaluation(),
        "local_path": r"X:\ADAS SI\2025\Kia\K4\2025 Kia K4 BSM Calibration.pdf",
        "content_sha256": "a" * 64,
        "candidate": {"text": source_text},
    }
    first = promotion.build_record(
        objective="Does the BSM need calibration after replacement?",
        vehicle={**VEHICLE, "trim": "GT-Line", "vin": "KNAAAA11111111111"},
        **common,
    )
    second = promotion.build_record(
        objective="After replacing the rear radar, do I calibrate it?",
        vehicle={**VEHICLE, "trim": "EX", "vin": "KNBBBB22222222222"},
        **common,
    )

    # These are the fields AutomotiveKnowledgeRepository uses to fingerprint a
    # record. They must be identical for the same source-backed interpretation.
    for key in ("application", "system", "component", "repair_event", "requirement", "procedures"):
        assert first.get(key) == second.get(key)
    assert "vin_pattern" not in first["application"]
    assert "trim" not in first["application"]

    first_meta = first["evidence"][0]["source"]["metadata"]
    second_meta = second["evidence"][0]["source"]["metadata"]
    assert first_meta["research_objective"] != second_meta["research_objective"]
    assert first_meta["queried_vin"] != second_meta["queried_vin"]
    assert first_meta["queried_trim"] != second_meta["queried_trim"]


def test_research_findings_card_is_not_a_second_followup_memory() -> None:
    message = {
        "id": 91,
        "artifacts": [
            {
                "type": "research_findings",
                "data": {
                    "outcome": "SATISFIED",
                    "findings": [{"title": "old finding", "accepted": True}],
                },
            },
            {"type": "calibration_iq_summary", "data": {"count": 3}},
        ],
    }
    packed = prompt_mod._stored_artifact_json([message], 8_000)  # noqa: SLF001
    assert "research_findings" not in packed
    assert "calibration_iq_summary" in packed


@pytest.mark.asyncio
async def test_ciq_satisfied_procedure_uses_shared_promotion_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeAdas:
        def model_search(self, _args: dict[str, Any]) -> dict[str, Any]:
            return {
                "status": "success",
                "results": [
                    {
                        "relative_path": r"2025\Kia\K4\K4 BSM.pdf",
                        "title": "K4 BSM",
                        "page": 1,
                    }
                ],
            }

        def resolve_relative(self, _path: str) -> Path:
            return tmp_path / "K4 BSM.pdf"

    service = SimpleNamespace(
        adas=FakeAdas(),
        client=object(),
        settings=SimpleNamespace(),
    )
    objective = {
        "objective_id": "bsm-1",
        "topic": "the OEM service procedure for Blind Spot Monitor calibration",
        "requirement_label": "Blind Spot Monitor",
        "calibration_title": "Blind Spot Monitor",
        "system": "Blind Spot Monitor",
        "calibration_id": "cal-1",
        "vehicle": VEHICLE,
        "vin": "KNABCD12345678901",
    }
    reviewed = {
        "review": _procedure_review(),
        "evaluation": _procedure_evaluation(),
        "document": {
            "title": "K4 BSM",
            "accepted": True,
            "captured": True,
            "artifact": {"relative_path": r"2025\Kia\K4\K4 BSM.pdf"},
        },
        "source_url": "adas-si:///K4%20BSM.pdf",
        "provider": "ADAS SI",
        "candidate": {"text": "complete exact procedure source text"},
        "finding": {
            "source": "adas_si",
            "relative_path": r"2025\Kia\K4\K4 BSM.pdf",
            "title": "K4 BSM",
            "page": 1,
            "library_vehicle": VEHICLE,
        },
        "vehicle": {**VEHICLE, "vin": objective["vin"]},
    }

    async def fake_review_local(_service: Any, _objective: dict[str, Any], _row: dict[str, Any]):
        return reviewed

    learned_calls: list[dict[str, Any]] = []

    async def learner(**kwargs: Any) -> dict[str, Any]:
        learned_calls.append(kwargs)
        return {"promoted": True, "record_id": "akr_test", "created": True}

    monkeypatch.setattr(cascade, "_review_local", fake_review_local)
    monkeypatch.setattr(cascade, "_semantic_cache_learner", lambda _service: learner)

    result = await cascade.local_procedure(service, objective, [])
    assert result is not None
    assert result["outcome"] == contract.SATISFIED
    assert result["learned"]["promoted"] is True
    assert len(learned_calls) == 1
    assert learned_calls[0]["evaluation"]["deliverable"] == "procedure"


@pytest.mark.asyncio
async def test_delegate_research_scopes_semantic_cache_by_vehicle_system_and_component() -> None:
    seen: list[dict[str, Any]] = []

    def knowledge_search(args: dict[str, Any]) -> dict[str, Any]:
        seen.append(dict(args))
        return {
            "status": "success",
            "records": [
                {
                    "id": "akr_bsm",
                    "lifecycle": "verified",
                    "application": {
                        "manufacturer": "Kia",
                        "year_start": 2025,
                        "year_end": 2025,
                        "model": "K4",
                    },
                    "system": {"name": "Blind Spot Monitor"},
                    "requirement": {
                        "requirement_type": "calibration",
                        "text": "Calibration is required after rear radar replacement.",
                    },
                    "evidence": [],
                }
            ],
        }

    async def evaluator(**_kwargs: Any) -> dict[str, Any]:
        return {
            "outcome": contract.SATISFIED,
            "deliverable": "answer",
            "reviewed": True,
            "review": {
                "malformed": False,
                "answers_objective": "FULLY",
                "system_match": "SAME_SYSTEM",
                "vehicle_applicability": "SOURCE_INCLUDES_THIS_VEHICLE",
            },
            "source_answer": "Calibration is required after rear radar replacement.",
            "unresolved": [],
        }

    handler = research_delegate.make_delegate_research(
        SimpleNamespace(),
        adas_search=lambda _args: {"status": "no_result", "results": []},
        knowledge_search=knowledge_search,
        evaluator=evaluator,
        client_provider=lambda: object(),
    )
    result = await handler(
        {
            "objective": "Does replacement of the rear radar require calibration?",
            "vehicle": VEHICLE,
            "system": "Blind Spot Monitor",
            "component": "Rear corner radar",
            "exclude_sources": ["web"],
        }
    )

    assert result["outcome"] == contract.SATISFIED
    assert seen
    query = seen[0]
    assert query["year"] == 2025
    assert query["manufacturer"] == "Kia"
    assert query["model"] == "K4"
    assert query["system"] == "Blind Spot Monitor"
    assert query["component"] == "Rear corner radar"
    assert query["query"] == "Blind Spot Monitor Rear corner radar"
    assert "replacement" not in query["query"].casefold()
