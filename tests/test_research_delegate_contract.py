"""delegate_research on the shared evidence contract.

Retrieval is not an answer: every retrieved candidate is judged by the shared
evaluator, the result carries SATISFIED / PARTIAL / UNSATISFIED, and ALLDATA
is not a source. A SATISFIED answer anchored in the authoritative ADAS SI
library is promoted to verified durable knowledge by Core's trusted path and
reused later without re-running the library review; model inference never is.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.services import research_delegate
from core.services import research_evidence_contract as contract
from core.services import research_knowledge_promotion as promotion
from core.services.automotive_knowledge import (
    AutomotiveKnowledgeRepository,
    AutomotiveKnowledgeService,
)

K4 = {"year": 2025, "make": "Kia", "model": "K4"}
CHART_RELATIVE = "2025/Kia/K4/Front Radar Bumper Requirement.pdf"
CHART_TEXT = (
    "Hyundai / Kia / Genesis Front Radar Bumper Requirement\n"
    "Model | Year | Bumper during calibration\n"
    "K4 | 2025 | OFF"
)
BUMPER_RELATIVE = "2025/Kia/K4/Front Bumper Removal.pdf"
BUMPER_TEXT = "1. Remove the front bumper cover.\n2. Disconnect the fog lamp connectors."


def _hit(relative: str, text: str, *, vehicle: dict[str, Any] | None = None, page: int = 1) -> dict[str, Any]:
    return {
        "title": Path(relative).stem,
        "relative_path": relative,
        "page": page,
        "excerpt": text,
        "url": f"/api/adas-si/document?path={relative}",
        "vehicle": vehicle or K4,
    }


class ScriptedEvaluator:
    """The shared evaluator's verdicts, scripted per source title."""

    def __init__(self, verdicts: dict[str, dict[str, Any]]):
        self.verdicts = verdicts
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        title = kwargs["candidate"]["title"]
        verdict = self.verdicts.get(title, {"outcome": "UNSATISFIED", "reasons": ["not the answer"]})
        return {"deliverable": kwargs["deliverable"], "review": None, **verdict}


def _handler(adas_search=None, knowledge_search=None, public_search=None, evaluator=None, **kwargs):
    calls: list[str] = []

    def adas(query):
        calls.append("adas_si")
        return adas_search(query) if adas_search else {"status": "no_result", "results": []}

    def knowledge(query):
        calls.append("automotive_knowledge")
        return knowledge_search(query) if knowledge_search else {"status": "no_result", "records": []}

    def web(*args, **kw):
        calls.append("web")
        return public_search(*args, **kw) if public_search else {"searched": True, "sources": [], "read_results": []}

    handler = research_delegate.make_delegate_research(
        SimpleNamespace(),
        adas_search=adas,
        knowledge_search=knowledge,
        public_search=web,
        evaluator=evaluator or ScriptedEvaluator({}),
        client_provider=object,
        **kwargs,
    )
    return handler, calls


def _run(handler, args):
    return asyncio.run(handler(args))


def test_the_default_source_order_has_no_alldata_and_consults_durable_knowledge_first() -> None:
    assert research_delegate.DEFAULT_SOURCE_ORDER == ("automotive_knowledge", "adas_si", "web")
    assert research_delegate.source_order({"sources": ["alldata"]}) == list(research_delegate.DEFAULT_SOURCE_ORDER)
    assert research_delegate.source_order({"sources": ["alldata", "web"]}) == ["automotive_knowledge", "web"]
    assert research_delegate.source_order({"sources": ["adas_si"]}) == ["automotive_knowledge", "adas_si"]


def test_retrieval_is_not_satisfaction_a_related_bumper_document_answers_nothing() -> None:
    evaluator = ScriptedEvaluator(
        {"Front Bumper Removal": {"outcome": "UNSATISFIED", "reasons": ["the source is related but does not answer the objective"]}}
    )
    handler, calls = _handler(
        adas_search=lambda _q: {"status": "success", "results": [_hit(BUMPER_RELATIVE, BUMPER_TEXT)]},
        evaluator=evaluator,
    )
    result = _run(handler, {"objective": "BSM calibration procedure", "vehicle": K4, "system": "blind spot monitoring", "deliverable": "procedure"})

    # The library returned a document ("success") and it still answers nothing.
    assert calls == ["automotive_knowledge", "adas_si", "web"]
    assert result["outcome"] == "UNSATISFIED"
    assert result["status"] == "unsatisfied" and result["verified"] is False
    finding = result["findings"][0]
    assert finding["accepted"] is False
    assert finding["evaluation"]["outcome"] == "UNSATISFIED"
    ledger = {row["source"]: row for row in result["source_ledger"]}
    assert ledger["adas_si"]["retrieved"] == 1 and ledger["adas_si"]["outcome"] == "UNSATISFIED"
    assert evaluator.calls[0]["deliverable"] == "procedure"
    text = research_delegate.result_text_for_model(result, max_chars=20_000)
    assert '"outcome": "UNSATISFIED"' in text and "not accepted" in text


def test_the_first_satisfied_source_stops_the_search() -> None:
    evaluator = ScriptedEvaluator(
        {"Front Radar Bumper Requirement": {"outcome": "SATISFIED", "anchor_quote": "K4 | 2025 | OFF", "source_answer": "Bumper off.", "stage": "calibration"}}
    )
    handler, calls = _handler(
        adas_search=lambda _q: {"status": "success", "results": [_hit(CHART_RELATIVE, CHART_TEXT)]},
        evaluator=evaluator,
    )
    result = _run(handler, {"objective": "Does the bumper stay on for front radar calibration?", "vehicle": K4})
    assert calls == ["automotive_knowledge", "adas_si"]
    assert result["outcome"] == "SATISFIED" and result["verified"] is True
    assert result["accepted_count"] == 1
    assert result["findings"][0]["evaluation"]["anchor_quote"] == "K4 | 2025 | OFF"


def test_exclusions_preferences_and_exhaustive_are_honored() -> None:
    evaluator = ScriptedEvaluator({"OEM page": {"outcome": "PARTIAL", "unresolved": ["target distance"]}})
    web_result = {"searched": True, "sources": [{"url": "https://oem.test/a", "title": "OEM page", "snippet": "s"}], "read_results": []}
    handler, calls = _handler(public_search=lambda *_a, **_k: web_result, evaluator=evaluator)
    result = _run(handler, {"objective": "radar aiming spec", "vehicle": K4, "exclude_sources": ["adas_si"], "exhaustive": True})
    assert calls == ["automotive_knowledge", "web"]
    assert result["outcome"] == "PARTIAL" and result["unresolved"] == ["target distance"]
    assert result["verified"] is False

    calls.clear()
    _run(handler, {"objective": "radar aiming spec", "vehicle": K4, "sources": ["web"]})
    # Durable knowledge is always consulted first unless Otis excludes it.
    assert calls == ["automotive_knowledge", "web"]
    calls.clear()
    _run(handler, {"objective": "radar aiming spec", "vehicle": K4, "sources": ["web"], "exclude_sources": ["automotive_knowledge"]})
    assert calls == ["web"]


def test_the_review_budget_stays_bounded_however_much_a_source_returns() -> None:
    evaluator = ScriptedEvaluator({})
    hits = [_hit(f"2025/Kia/K4/Doc {index}.pdf", f"text {index}") for index in range(8)]
    handler, _calls = _handler(adas_search=lambda _q: {"status": "success", "results": hits}, evaluator=evaluator)
    result = _run(handler, {"objective": "x y z", "vehicle": K4, "exclude_sources": ["web"]})
    assert len(evaluator.calls) == research_delegate.MAX_REVIEWS_PER_SOURCE
    unreviewed = [finding for finding in result["findings"] if finding["evaluation"]["outcome"] is None]
    assert unreviewed and all(finding["accepted"] is False for finding in unreviewed)


def test_an_evaluator_failure_is_never_an_acceptance() -> None:
    async def broken(**_kwargs):
        raise RuntimeError("model went away")

    handler, _calls = _handler(
        adas_search=lambda _q: {"status": "success", "results": [_hit(CHART_RELATIVE, CHART_TEXT)]},
        evaluator=broken,
    )
    result = _run(handler, {"objective": "bumper", "vehicle": K4, "exclude_sources": ["web"]})
    assert result["outcome"] == "UNSATISFIED"
    assert result["findings"][0]["accepted"] is False


def test_without_a_bound_model_nothing_retrieved_is_accepted() -> None:
    handler = research_delegate.make_delegate_research(
        SimpleNamespace(),
        adas_search=lambda _q: {"status": "success", "results": [_hit(CHART_RELATIVE, CHART_TEXT)]},
        knowledge_search=lambda _q: {"status": "no_result", "records": []},
        public_search=lambda *_a, **_k: {"searched": True, "sources": [], "read_results": []},
    )
    result = _run(handler, {"objective": "bumper", "vehicle": K4})
    assert result["outcome"] == "UNSATISFIED" and result["verified"] is False
    assert result["findings"][0]["evaluation"]["reasons"] == ["no model was available to review the evidence"]


# ------------------------------------------------ durable learning and reuse


class Library:
    """A real ADAS SI library root with one document, for hashing and reuse."""

    def __init__(self, root: Path, relative: str = CHART_RELATIVE, content: bytes = b"%PDF-1.4 chart"):
        self.root = root
        self.relative = relative
        self.path = root / relative
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(content)

    def resolve_relative(self, relative: str) -> Path:
        return self.root / relative

    def render_page(self, *_args):
        return b"png"

    def _pages(self, _path):
        return [(1, CHART_TEXT)]


def _satisfied(anchor: str = "K4 | 2025 | OFF", **review_overrides: Any) -> dict[str, Any]:
    review = {
        "question_asks": "Bumper during front radar calibration on a 2025 Kia K4?",
        "source_covers": "Kia K4 2025, front radar",
        "vehicle_applicability": "SOURCE_INCLUDES_THIS_VEHICLE",
        "system_match": "SAME_SYSTEM",
        "anchor_quote": anchor,
        "anchor_grounded": True,
        "source_answer": "The front bumper comes off for front radar calibration.",
        "stage": "calibration",
        "answers_objective": "FULLY",
        "unresolved": [],
        "confidence": 0.92,
        "malformed": False,
    }
    review.update(review_overrides)
    return {
        "outcome": "SATISFIED",
        "deliverable": "answer",
        "review": review,
        "stage": review["stage"],
        "anchor_quote": anchor,
        "source_answer": review["source_answer"],
        "source_covers": review["source_covers"],
        "unresolved": [],
        "reasons": [],
    }


def _repository(tmp_path: Path, library: Library) -> AutomotiveKnowledgeRepository:
    return AutomotiveKnowledgeRepository(tmp_path / "knowledge.sqlite", authoritative_roots=[library.root])


def test_a_satisfied_anchored_library_answer_becomes_verified_and_is_reused(tmp_path: Path) -> None:
    library = Library(tmp_path / "ADAS SI")
    repository = _repository(tmp_path, library)
    service = AutomotiveKnowledgeService(repository)
    learn = promotion.make_learner(repository, library)

    class FirstTime(ScriptedEvaluator):
        async def __call__(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["provider"] == "ADAS SI":
                return _satisfied()
            return {"outcome": "UNSATISFIED", "deliverable": "answer", "reasons": ["no"]}

    first_evaluator = FirstTime({})
    first, calls = _handler(
        adas_search=lambda _q: {"status": "success", "results": [_hit(CHART_RELATIVE, CHART_TEXT)]},
        knowledge_search=service.search,
        evaluator=first_evaluator,
        adas=library,
        learn=learn,
    )
    result = _run(first, {"objective": "Does the front bumper stay on for front radar calibration?", "vehicle": K4, "system": "front radar"})
    assert result["outcome"] == "SATISFIED"
    assert result["learned"]["promoted"] is True
    record = repository.get(result["learned"]["record_id"])
    assert record["lifecycle"] == "verified"
    assert record["source_integrity"]["status"] == "current"
    assert record["evidence"][0]["excerpt"] == "K4 | 2025 | OFF"
    assert record["evidence"][0]["page_start"] == 1
    assert record["created_by"] == promotion.PROMOTION_ACTOR

    # A later conversation: durable knowledge answers first, so the library
    # is never searched or reviewed again.
    class Later(ScriptedEvaluator):
        async def __call__(self, **kwargs):
            self.calls.append(kwargs)
            assert kwargs["provider"] == "durable automotive knowledge"
            assert "K4 | 2025 | OFF" in kwargs["candidate"]["text"]
            assert contract.anchor_in_text("K4 | 2025 | OFF", kwargs["candidate"]["text"])
            return {"outcome": "SATISFIED", "deliverable": "answer", "anchor_quote": "K4 | 2025 | OFF", "reasons": []}

    later_evaluator = Later({})
    later, later_calls = _handler(
        adas_search=lambda _q: pytest.fail("the library must not be re-researched"),
        knowledge_search=service.search,
        evaluator=later_evaluator,
    )
    reused = _run(later, {"objective": "Bumper on or off for the K4 front radar?", "vehicle": K4})
    assert later_calls == ["automotive_knowledge"]
    assert reused["outcome"] == "SATISFIED"
    assert reused["findings"][0]["source"] == "automotive_knowledge"
    assert len(later_evaluator.calls) == 1


def test_model_inference_is_quarantined_from_verified_knowledge(tmp_path: Path) -> None:
    library = Library(tmp_path / "ADAS SI")
    repository = _repository(tmp_path, library)
    learn = promotion.make_learner(repository, library)
    finding = {"source": "adas_si", "relative_path": CHART_RELATIVE, "page": 1, "title": "Chart", "library_vehicle": K4}

    def attempt(evaluation, *, candidate_text=CHART_TEXT, vehicle=K4, finding_overrides=None):
        return asyncio.run(
            learn(
                objective="bumper during front radar calibration",
                vehicle=vehicle,
                system="front radar",
                component=None,
                finding={**finding, **(finding_overrides or {})},
                evaluation=evaluation,
                candidate={"text": candidate_text},
            )
        )

    # An inference no retrieved text contains never becomes verified.
    invented = attempt(_satisfied(anchor="Bumper must remain installed during calibration"))
    assert invented["promoted"] is False and "anchor" in invented["reason"]
    # Neither does a PARTIAL outcome, another source, an unresolved application,
    # evidence the library files for another year, or an unpaged anchor.
    partial = {**_satisfied(), "outcome": "PARTIAL"}
    assert attempt(partial)["promoted"] is False
    assert attempt(_satisfied(), finding_overrides={"source": "web"})["promoted"] is False
    assert attempt(_satisfied(), vehicle={"make": "Kia", "model": "K4"})["promoted"] is False
    assert attempt(_satisfied(), vehicle={**K4, "year": 2021})["promoted"] is False
    assert attempt(_satisfied(), finding_overrides={"page": None})["promoted"] is False
    assert attempt(_satisfied(vehicle_applicability="SOURCE_DOES_NOT_SAY"))["promoted"] is False
    assert attempt({**_satisfied(), "review": {**_satisfied()["review"], "malformed": True}})["promoted"] is False
    assert repository.search({"manufacturer": "Kia", "model": "K4", "year": 2025})["records"] == []

    # The model-facing facade still cannot self-verify a claim.
    stored = AutomotiveKnowledgeService(repository).store(
        {
            "application": {"manufacturer": "Kia", "model": "K4", "year": 2025},
            "system": "front radar",
            "repair_event": "bumper",
            "requirement": {"requirement_type": "calibration", "text": "Bumper must stay installed."},
            "lifecycle": "verified",
            "evidence": [
                {
                    "source": {
                        "source_type": "adas_si_document",
                        "source_name": "Chart",
                        "local_path": str(library.path),
                        "content_sha256": hashlib.sha256(library.path.read_bytes()).hexdigest(),
                        "authoritative": True,
                    },
                    "page": 1,
                    "excerpt": "Bumper must stay installed.",
                    "extraction_status": "extracted",
                    "verification_status": "verified",
                }
            ],
        },
        actor="x",
    )
    assert stored["verified"] is False and stored["stored_lifecycle"] != "verified"


def test_hash_integrity_gates_promotion_and_later_reads(tmp_path: Path) -> None:
    library = Library(tmp_path / "ADAS SI")
    repository = _repository(tmp_path, library)
    record = promotion.build_record(
        objective="bumper",
        vehicle=K4,
        system="front radar",
        component=None,
        finding={"source": "adas_si", "relative_path": CHART_RELATIVE, "page": 1, "title": "Chart"},
        evaluation=_satisfied(),
        local_path=str(library.path),
        content_sha256="0" * 64,
    )
    record.pop("component")
    # The repository re-hashes the file itself; a claimed hash that does not
    # match is refused.
    with pytest.raises(Exception, match="hash"):
        repository.create_record(record, actor=promotion.PROMOTION_ACTOR)

    learned = asyncio.run(
        promotion.make_learner(repository, library)(
            objective="bumper during front radar calibration",
            vehicle=K4,
            system="front radar",
            component=None,
            finding={"source": "adas_si", "relative_path": CHART_RELATIVE, "page": 1, "title": "Chart", "library_vehicle": K4},
            evaluation=_satisfied(),
            candidate={"text": CHART_TEXT},
        )
    )
    assert learned["promoted"] is True
    # Once the source changes on disk, the claim is no longer served as verified.
    library.path.write_bytes(b"%PDF-1.4 a different chart")
    assert repository.search({"manufacturer": "Kia", "model": "K4", "year": 2025})["records"] == []
    assert repository.get(learned["record_id"])["source_integrity"]["status"] == "stale"
