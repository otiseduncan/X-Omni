r"""Opt-in live acceptance: X interprets retrieved evidence and answers like a person.

Drives the production orchestrator against the configured local Qwen worker.
Research results are fixtures (see ``evidence_fixtures``): the real region-OCR
text of the Hyundai/Kia/Genesis front radar bumper chart, a staged Kia radar
procedure, and a source that contradicts a common assumption. Nothing reaches
ADAS SI, ALLDATA, ScrapeX, or Calibration IQ.

Answers are judged for meaning by a separate structured audit call, never by
wording; the structural checks only prove that raw extraction stayed in the
evidence record and out of the answer and its spoken form.

Run explicitly::

    $env:XOMNI_RUN_LIVE_MODEL_ACCEPTANCE = "1"
    .venv\Scripts\python.exe -m pytest -q tests\test_evidence_interpretation_live.py -s

or::

    .venv\Scripts\python.exe tests\test_evidence_interpretation_live.py [--scenario NAME] [--repeat N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "tests"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import evidence_fixtures as fx  # noqa: E402
from test_model_first_live_acceptance import (  # noqa: E402
    RUN_ENV,
    FixtureBackends,
    LiveHarness,
    ModelProtocolError,
    WorkerTarget,
    configured_worker_target,
    worker_is_healthy,
)

NO_RAW_EXTRACTION = (
    "The response is a conversational answer: it does not paste raw OCR text, runs of "
    "table cells (such as 'N/A N/A OFF OFF'), JSON, field names, file paths, URLs, or tool "
    "status values."
)


class EvidenceBackends(FixtureBackends):
    """Fixture sources whose ADAS SI and durable-knowledge results are chosen per scenario."""

    def __init__(self) -> None:
        super().__init__()
        self.adas_hits: Callable[[dict[str, Any]], list[dict[str, Any]]] = lambda _args: []
        self.adas_failure: str | None = None
        self.knowledge_records: Callable[[dict[str, Any]], list[dict[str, Any]]] = lambda _args: []
        self.inventory_summary: dict[str, Any] | None = None

    def knowledge_search(self, args: dict[str, Any]) -> dict[str, Any]:
        self.research_calls.append(("automotive_knowledge", deepcopy(args)))
        records = self.knowledge_records(args)
        return {"status": "success" if records else "no_result", "records": deepcopy(records)}

    def adas_si_inventory(self, args: dict[str, Any]) -> dict[str, Any]:
        self.research_calls.append(("adas_si_inventory", deepcopy(args)))
        if self.inventory_summary is None:
            return {"status": "unavailable"}
        return {"status": "success", "summary": deepcopy(self.inventory_summary), "applications": []}

    def adas_search(self, args: dict[str, Any]) -> dict[str, Any]:
        self.research_calls.append(("adas_si", deepcopy(args)))
        if self.adas_failure:
            raise RuntimeError(self.adas_failure)
        hits = self.adas_hits(args)
        if not hits:
            return {"status": "no_result", "results": [], "structured_query": deepcopy(args)}
        return {"status": "success", "results": deepcopy(hits), "structured_query": deepcopy(args)}



def _kia_tokens(args: dict[str, Any]) -> list[str]:
    words = json.dumps(args).casefold().replace('"', " ").split()
    return [word.strip(",.:{}") for word in words if len(word) > 2] or ["kia"]


@dataclass(frozen=True)
class EvidenceTurn:
    user: str
    contracts: dict[str, str]
    # Raw source text that must stay out of the answer unless Otis asked for it.
    raw_sources: tuple[str, ...] = ()
    raw_allowed: bool = False
    expect_evidence: bool = True
    # A turn that never retrieved the evidence tests nothing about reading it.
    required_call: str | None = None
    # Structural check of the fixture calls this turn made (source, arguments).
    check: Callable[[list[tuple[str, dict[str, Any]]], "EvidenceTurnResult"], None] | None = None


@dataclass(frozen=True)
class EvidenceScenario:
    name: str
    turns: tuple[EvidenceTurn, ...]
    setup: Callable[[EvidenceBackends], None] = field(default=lambda _backends: None)


def _kia_staged_setup(backends: EvidenceBackends) -> None:
    def hits(args: dict[str, Any]) -> list[dict[str, Any]]:
        make = str((args.get("vehicle") or {}).get("make") or args.get("question") or "")
        if "kia" not in make.casefold() and "kia" not in json.dumps(args).casefold():
            return []
        return [fx.staged_procedure_hit(), fx.chart_search_hit(_kia_tokens(args))]

    backends.adas_hits = hits


def _kia_chart_setup(backends: EvidenceBackends) -> None:
    backends.adas_hits = lambda args: (
        [fx.chart_search_hit(_kia_tokens(args))] if "kia" in json.dumps(args).casefold() else []
    )


def _dynamic_setup(backends: EvidenceBackends) -> None:
    backends.adas_hits = lambda args: (
        [fx.dynamic_only_hit()] if "tucson" in json.dumps(args).casefold() else []
    )


def _failure_setup(backends: EvidenceBackends) -> None:
    backends.adas_failure = "sqlite3.OperationalError: unable to open database file"


def _k4_library(*hits: Callable[[], dict[str, Any]]) -> Callable[[EvidenceBackends], None]:
    def setup(backends: EvidenceBackends) -> None:
        backends.adas_hits = lambda args: (
            [hit() for hit in hits] if "k4" in json.dumps(args).casefold() else []
        )

    return setup


def _k4_reuse_setup(backends: EvidenceBackends) -> None:
    backends.adas_hits = lambda args: (
        [fx.k4_front_radar_hit()] if "k4" in json.dumps(args).casefold() else []
    )
    backends.knowledge_records = lambda args: (
        [fx.k4_front_radar_knowledge_record()]
        if str(args.get("model") or "").casefold() == "k4" and args.get("year") == 2025
        else []
    )


def _inventory_setup(backends: EvidenceBackends) -> None:
    backends.inventory_summary = {
        "document_count": 412,
        "vehicle_application_count": 137,
        "parsed_document_count": 398,
    }
    # A vehicle search would return a handful of hits: never the inventory.
    backends.adas_hits = lambda _args: [fx.k4_bsm_hit(), fx.k4_bumper_hit(), fx.k4_front_radar_hit()]


def _research_args(calls: list[tuple[str, dict[str, Any]]], source: str) -> list[dict[str, Any]]:
    return [args for name, args in calls if name == source]


def _k4_research(calls: list[tuple[str, dict[str, Any]]], _result: Any) -> None:
    searched = _research_args(calls, "adas_si") + _research_args(calls, "automotive_knowledge")
    if not searched:
        raise ModelProtocolError(f"no research ran: {calls}")
    for args in searched:
        text = json.dumps(args).casefold()
        if "k4" not in text:
            raise ModelProtocolError(f"research lost the K4: {args}")


def _k4_bsm_research(calls: list[tuple[str, dict[str, Any]]], result: Any) -> None:
    _k4_research(calls, result)
    library = _research_args(calls, "adas_si")
    if not any(
        any(word in json.dumps(args).casefold() for word in ("blind", "bsm", "bcw", "corner", "side"))
        for args in library
    ):
        raise ModelProtocolError(f"the correction did not switch the system to BSM: {library}")


def _reused_without_library(calls: list[tuple[str, dict[str, Any]]], _result: Any) -> None:
    if not _research_args(calls, "automotive_knowledge"):
        raise ModelProtocolError(f"durable knowledge was not consulted: {calls}")
    if _research_args(calls, "adas_si"):
        raise ModelProtocolError("the library was re-researched although durable knowledge answered")


def _inventory_read(calls: list[tuple[str, dict[str, Any]]], _result: Any) -> None:
    reads = _research_args(calls, "adas_si_inventory")
    if not reads:
        raise ModelProtocolError(f"the ADAS SI inventory was not read: {calls}")
    if any(args.get("organize_root") is not False for args in reads):
        raise ModelProtocolError(f"the inventory read could file documents: {reads}")


def _no_alldata(calls: list[tuple[str, dict[str, Any]]], result: Any) -> None:
    from core.services import alldata_sunset

    if any(name == "alldata" for name, _args in calls):
        raise ModelProtocolError("an ALLDATA source was reached")
    touched = set(result.calls) & alldata_sunset.SUNSET_TOOLS
    if touched:
        raise ModelProtocolError(f"a sunset ALLDATA capability was called: {touched}")


STAGED_CONTRACTS = {
    "recognizes_both_states": (
        "The response says the front bumper (cover) is removed at one point in the front "
        "radar procedure and installed at another point."
    ),
    "states_belong_to_stages": (
        "The response ties the removed bumper to the inspection/mounting-check part of the "
        "procedure and the installed bumper to the calibration itself, or otherwise makes "
        "clear these are different stages rather than a contradiction."
    ),
    "not_installed_throughout": (
        "The response does not say or imply that the bumper simply stays installed for the "
        "entire procedure."
    ),
    "not_removed_throughout": (
        "The response does not say or imply that the bumper simply stays removed for the "
        "entire procedure, including during calibration."
    ),
    "no_raw_extraction": NO_RAW_EXTRACTION,
}

SCENARIOS: tuple[EvidenceScenario, ...] = (
    EvidenceScenario(
        name="kia_staged_bumper_requirement",
        setup=_kia_staged_setup,
        turns=(
            EvidenceTurn(
                user="what can you tell me about Kia front bumpers during the long range radar calibration",
                contracts=STAGED_CONTRACTS,
                required_call="delegate_research",
                raw_sources=(fx.bumper_chart_pages()["pages"][fx.CHART_TITLE],),
            ),
            EvidenceTurn(
                user="show me exactly what the source says about the bumper",
                contracts={
                    "shows_source_wording": (
                        "The response reproduces or directly quotes the source's own wording "
                        "about removing and reinstalling the front bumper cover, rather than "
                        "only paraphrasing it or refusing."
                    ),
                    "no_invented_text": (
                        "Nothing presented as the source's words is absent from the source."
                    ),
                },
                raw_allowed=True,
            ),
        ),
    ),
    EvidenceScenario(
        name="kia_real_chart_regression",
        setup=_kia_chart_setup,
        turns=(
            EvidenceTurn(
                user="what can you tell me about Kia front bumpers during the long range radar calibration",
                required_call="delegate_research",
                contracts={
                    "follows_the_chart": (
                        "The response reports what the retrieved chart says for Kia: the front "
                        "bumper is marked OFF (removed) for the front radar work on most listed "
                        "Kia model years, with some ON or N/A entries, so it depends on model "
                        "and year."
                    ),
                    "no_generic_override": (
                        "The response does not state, as a general rule, that the bumper must "
                        "be installed or in place during front radar calibration."
                    ),
                    "no_raw_extraction": NO_RAW_EXTRACTION,
                },
                raw_sources=(fx.bumper_chart_pages()["pages"][fx.CHART_TITLE],),
            ),
        ),
    ),
    EvidenceScenario(
        name="authoritative_evidence_overrides_generic_knowledge",
        setup=_dynamic_setup,
        turns=(
            EvidenceTurn(
                user=(
                    "What's the blind spot radar calibration procedure for a 2024 Hyundai "
                    "Tucson after the rear bumper is replaced?"
                ),
                required_call="delegate_research",
                contracts={
                    "follows_retrieved_source": (
                        "The response says the rear corner radar is calibrated by the driving "
                        "auto-calibration the source describes (drive straight above about 30 "
                        "km/h for at least 10 minutes on a road with guardrails)."
                    ),
                    "no_static_target_procedure": (
                        "The response does not describe a static calibration with a target, "
                        "reflector, or measured setup for this vehicle."
                    ),
                    "no_raw_extraction": NO_RAW_EXTRACTION,
                },
                raw_sources=(fx.DYNAMIC_ONLY_TEXT,),
            ),
        ),
    ),
    EvidenceScenario(
        name="source_failure_is_explained_naturally",
        setup=_failure_setup,
        turns=(
            EvidenceTurn(
                user="what's the front camera calibration procedure for a 2022 Kia Telluride?",
                required_call="delegate_research",
                contracts={
                    "explains_failure": (
                        "The response says plainly that it could not retrieve the procedure "
                        "(for example the library was unavailable) and does not present any "
                        "procedure steps as retrieved."
                    ),
                    "no_orchestration_dump": (
                        "The response does not include JSON, exception text, database errors, "
                        "status codes or field names such as source_ledger, not_attempted, or "
                        "authentication_required."
                    ),
                },
            ),
        ),
    ),
    # --- 2026-09-17 research contract field regressions ------------------
    EvidenceScenario(
        name="retrieval_is_not_satisfaction",
        setup=_k4_library(fx.k4_bumper_hit),
        turns=(
            EvidenceTurn(
                user="Give me the BSM calibration procedure for a 2025 Kia K4.",
                required_call="delegate_research",
                check=_k4_research,
                contracts={
                    "no_procedure_from_a_related_page": (
                        "The response does not present rear bumper removal and installation "
                        "steps as the blind spot (BSM/BCW) calibration procedure."
                    ),
                    "says_procedure_not_found": (
                        "The response makes clear the BSM calibration procedure itself was not "
                        "found or not confirmed in the sources checked."
                    ),
                    "no_invented_steps": (
                        "The response does not give calibration steps, target distances, or "
                        "reflector positions as if they came from a source."
                    ),
                },
            ),
        ),
    ),
    EvidenceScenario(
        name="kia_application_grounding",
        setup=_k4_library(fx.k4_front_radar_hit),
        turns=(
            EvidenceTurn(
                user="On a 2021 Kia K4, does the front bumper stay on for the front radar calibration?",
                required_call="delegate_research",
                contracts={
                    "no_later_model_year_as_fact": (
                        "The response does not state as an established fact for a 2021 K4 what "
                        "a 2025 K4 source says; it says the source it found covers the 2025 K4 "
                        "or that it could not confirm the answer for a 2021."
                    ),
                },
            ),
        ),
    ),
    EvidenceScenario(
        name="bsm_correction_keeps_the_vehicle",
        setup=_k4_library(fx.k4_front_radar_hit, fx.k4_bsm_hit),
        turns=(
            EvidenceTurn(
                user="What does the front radar calibration need on a 2025 Kia K4?",
                required_call="delegate_research",
                check=_k4_research,
                contracts={"stays_on_topic": "The response is about the front radar on the 2025 Kia K4."},
            ),
            EvidenceTurn(
                user="No, I meant BSM.",
                required_call="delegate_research",
                check=_k4_bsm_research,
                contracts={
                    "switched_system_kept_vehicle": (
                        "The response is about the blind spot (BSM / BCW / rear corner radar) "
                        "system on the 2025 Kia K4, not the front radar and not another vehicle."
                    ),
                    "no_raw_extraction": NO_RAW_EXTRACTION,
                },
                raw_sources=(fx.K4_BSM_TEXT,),
            ),
        ),
    ),
    EvidenceScenario(
        name="technical_follow_ups_keep_the_subject",
        setup=_k4_library(fx.k4_bsm_hit, fx.k4_bumper_hit),
        turns=(
            EvidenceTurn(
                user="Check BSM on this 2025 Kia K4.",
                required_call="delegate_research",
                check=_k4_research,
                contracts={"about_k4_bsm": "The response is about the blind spot system on the 2025 Kia K4."},
            ),
            EvidenceTurn(
                user="Does the bumper stay on?",
                contracts={
                    "answers_from_the_source": (
                        "The response says the rear bumper cover must be installed before the "
                        "BSM calibration, for the 2025 Kia K4."
                    ),
                },
                expect_evidence=False,
            ),
            EvidenceTurn(
                user="Where does it say that?",
                contracts={
                    "names_the_source": (
                        "The response points to the K4 blind spot / rear corner radar "
                        "calibration document (or quotes its bumper sentence), not a different "
                        "vehicle or system."
                    ),
                },
                raw_allowed=True,
                expect_evidence=False,
            ),
            EvidenceTurn(
                user="Show me the procedure.",
                contracts={
                    "gives_the_k4_bsm_procedure": (
                        "The response gives the 2025 Kia K4 blind spot (rear corner radar) "
                        "calibration procedure from the source: KDS calibration menu, corner "
                        "reflector 1.0 m behind the rear bumper at 45 degrees, and completion "
                        "confirmation -- not a front radar or other vehicle's procedure."
                    ),
                },
                raw_allowed=True,
                expect_evidence=False,
            ),
        ),
    ),
    EvidenceScenario(
        name="inventory_uses_the_authoritative_count",
        setup=_inventory_setup,
        turns=(
            EvidenceTurn(
                user="How many vehicles are represented in ADAS SI?",
                required_call="query_ciq",
                check=_inventory_read,
                contracts={
                    "uses_the_inventory_count": (
                        "The response says 137 vehicles are represented in ADAS SI."
                    ),
                },
                expect_evidence=False,
            ),
        ),
    ),
    EvidenceScenario(
        name="durable_knowledge_is_reused",
        setup=_k4_reuse_setup,
        turns=(
            EvidenceTurn(
                user="Does the front bumper stay on for the front radar calibration on a 2025 Kia K4?",
                required_call="delegate_research",
                check=_reused_without_library,
                contracts={
                    "answers_from_durable_knowledge": (
                        "The response says the front bumper cover must be installed during front "
                        "radar calibration on the 2025 Kia K4."
                    ),
                    "no_raw_extraction": NO_RAW_EXTRACTION,
                },
            ),
        ),
    ),
    EvidenceScenario(
        name="alldata_is_never_used",
        setup=_k4_library(fx.k4_bsm_hit),
        turns=(
            EvidenceTurn(
                user="Check ALLDATA for the 2025 Kia K4 blind spot calibration procedure.",
                check=_no_alldata,
                contracts={
                    "no_alldata_claim": (
                        "The response does not claim ALLDATA was searched, opened, or signed in "
                        "to, and does not present anything as coming from ALLDATA."
                    ),
                },
                expect_evidence=False,
            ),
        ),
    ),
    EvidenceScenario(
        name="calibration_iq_count_answers_directly",
        turns=(
            EvidenceTurn(
                user="How many ROs are missing an ADAS map?",
                contracts={
                    "direct_answer_first": (
                        "The first sentence of the response gives the number of repair orders "
                        "missing an ADAS Map."
                    ),
                    "no_raw_extraction": NO_RAW_EXTRACTION,
                },
            ),
        ),
    ),
)


@dataclass
class EvidenceTurnResult:
    calls: list[str]
    text: str
    response: dict[str, Any]
    artifacts: list[dict[str, Any]]
    metrics: dict[str, Any]
    elapsed_seconds: float


class EvidenceHarness(LiveHarness):
    def __init__(self, target: WorkerTarget, *, timeout: float = 300.0) -> None:
        from core.orchestrator.loop import Orchestrator

        super().__init__(target, timeout=timeout)
        self.backends = EvidenceBackends()
        from test_model_first_live_acceptance import build_harness_registry

        self.registry = build_harness_registry(self.store, self.backends)
        self.registry.register("adas_si_inventory", self.backends.adas_si_inventory)
        self.orchestrator = Orchestrator(
            self.orchestrator.router,
            self.client,
            self.registry,
            self.store,
            self.orchestrator.settings,
        )

    def drive(self, conversation_id: int, user: str) -> EvidenceTurnResult:
        message_id = self.store.add_message(conversation_id, "user", user)
        started = time.perf_counter()
        calls: list[str] = []
        tokens: list[str] = []
        done: dict[str, Any] = {}

        async def run() -> None:
            nonlocal done
            async for event in self.orchestrator.run_turn(
                conversation_id,
                user,
                approval_context={
                    "session_id": "local:acceptance",
                    "user_id": "local-dev",
                    "role": "owner",
                    "message_id": message_id,
                },
            ):
                kind = event.get("type")
                if kind == "tool_start":
                    calls.append(str(event.get("name")))
                elif kind == "token":
                    tokens.append(str(event.get("text") or ""))
                elif kind == "error":
                    raise ModelProtocolError(f"orchestrator error: {event.get('message')}")
                elif kind == "done":
                    done = event

        asyncio.run(run())
        return EvidenceTurnResult(
            calls=calls,
            text="".join(tokens).strip(),
            response=done.get("response") or {},
            artifacts=done.get("artifacts") or [],
            metrics=done.get("metrics") or {},
            elapsed_seconds=time.perf_counter() - started,
        )

    def audit(self, turn: EvidenceTurn, result: EvidenceTurnResult) -> None:
        """Judge each contract separately, after summarizing evidence and answer.

        A single overall verdict from the local model passed answers that
        plainly contradicted the chart; summarizing first and judging each
        contract on its own did not.
        """
        evidence = [
            artifact.get("data")
            for artifact in result.artifacts
            if artifact.get("presentation") == "evidence"
        ]
        names = sorted(turn.contracts)
        tool = {
            "type": "function",
            "function": {
                "name": "evidence_acceptance_audit",
                "description": "Test-only semantic audit of an assistant answer.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "evidence_says": {"type": "string"},
                        "response_says": {"type": "string"},
                        "contracts": {
                            "type": "array",
                            "minItems": len(names),
                            "maxItems": len(names),
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "name": {"type": "string", "enum": names},
                                    "reason": {"type": "string"},
                                    "holds": {"type": "boolean"},
                                },
                                "required": ["name", "reason", "holds"],
                            },
                        },
                    },
                    "required": ["evidence_says", "response_says", "contracts"],
                },
            },
        }
        payload = {
            "model": self.target.model,
            "temperature": 0.0,
            "max_tokens": 1_200,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a strict semantic acceptance auditor, not the assistant. First "
                        "summarize what the retrieved evidence says for the user's question and "
                        "what the assistant response claims. Then judge every contract on its "
                        "own against the response: holds is false when the response breaks it. "
                        "Judge meaning, not wording. If no evidence was retrieved, judge against "
                        "the user message and the response."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "user_message": turn.user,
                            "contracts": turn.contracts,
                            "retrieved_evidence": evidence,
                            "assistant_response": result.text,
                        },
                        ensure_ascii=False,
                        default=str,
                    )[:60_000],
                },
            ],
            "tools": [tool],
            "tool_choice": {"type": "function", "function": {"name": "evidence_acceptance_audit"}},
        }
        verdict: dict[str, Any] | None = None
        for _attempt in range(2):
            raw = self.client._post(payload)  # noqa: SLF001 - harness-internal
            tool_calls = raw["choices"][0]["message"].get("tool_calls") or []
            if len(tool_calls) == 1:
                try:
                    verdict = json.loads(tool_calls[0]["function"].get("arguments") or "{}")
                    break
                except (TypeError, ValueError):
                    continue
        if verdict is None:
            raise ModelProtocolError("audit did not emit exactly one structured result")
        judged = {item.get("name"): item for item in verdict.get("contracts") or []}
        missing = [name for name in names if name not in judged]
        broken = [name for name in names if name in judged and judged[name].get("holds") is not True]
        if missing or broken:
            raise ModelProtocolError(
                f"violated {broken} missing {missing}: "
                + json.dumps({name: judged.get(name, {}).get("reason") for name in broken}, ensure_ascii=False)
            )

    @staticmethod
    def check_structure(turn: EvidenceTurn, result: EvidenceTurnResult) -> None:
        if not result.text:
            raise ModelProtocolError("empty answer")
        if turn.required_call and turn.required_call not in result.calls:
            raise ModelProtocolError(
                f"{turn.required_call} was not called (observed {result.calls})"
            )
        response = result.response
        if response.get("assistant_text") != result.text:
            raise ModelProtocolError("done.response.assistant_text differs from the streamed answer")
        spoken = str(response.get("spoken_text") or "")
        if not spoken or "http" in spoken or "/api/" in spoken or "{" in spoken:
            raise ModelProtocolError(f"spoken_text is not clean conversational text: {spoken!r}")
        if turn.expect_evidence and result.calls:
            if not response.get("evidence"):
                raise ModelProtocolError("no evidence attached to the response")
            if (response.get("evidence_presentation") or {}).get("default_state") != "collapsed":
                raise ModelProtocolError("evidence is not collapsed by default")
        if turn.raw_allowed:
            return
        folded_answer = " ".join(result.text.split()).casefold()
        for source in turn.raw_sources:
            for row in fx.raw_row_lines(source):
                for candidate in (row, row.replace(" ", " | ")):
                    if candidate.casefold() in folded_answer:
                        raise ModelProtocolError(f"raw source row reached the answer: {candidate!r}")


def run_suite(
    target: WorkerTarget,
    *,
    names: set[str] | None = None,
    repeat: int = 1,
    timeout: float = 300.0,
) -> dict[str, Any]:
    selected = [scenario for scenario in SCENARIOS if not names or scenario.name in names]
    rows: list[dict[str, Any]] = []
    for scenario in selected:
        for attempt in range(repeat):
            harness = EvidenceHarness(target, timeout=timeout)
            scenario.setup(harness.backends)
            conversation_id = harness.store.create_conversation(scenario.name)
            turns: list[dict[str, Any]] = []
            status = "passed"
            error = None
            try:
                for turn in scenario.turns:
                    before = len(harness.backends.research_calls)
                    result = harness.drive(conversation_id, turn.user)
                    turn_calls = list(harness.backends.research_calls[before:])
                    turns.append(
                        {
                            "user": turn.user,
                            "calls": result.calls,
                            "answer": result.text,
                            "evidence": [
                                (item.get("type"), item.get("attention"))
                                for item in result.response.get("evidence") or []
                            ],
                            "evidence_review": result.metrics.get("evidence_review"),
                            "research": [name for name, _args in turn_calls],
                            "elapsed_seconds": round(result.elapsed_seconds, 1),
                        }
                    )
                    harness.check_structure(turn, result)
                    if turn.check is not None:
                        turn.check(turn_calls, result)
                    harness.audit(turn, result)
            except Exception as exc:  # noqa: BLE001 - the report keeps every failure
                status = "failed"
                error = f"{type(exc).__name__}: {exc}"
            finally:
                harness.close()
            rows.append(
                {
                    "scenario": scenario.name,
                    "attempt": attempt + 1,
                    "status": status,
                    "error": error,
                    "turns": turns,
                }
            )
    failed = sum(1 for row in rows if row["status"] == "failed")
    return {
        "worker": {"endpoint": target.endpoint, "model": target.model},
        "summary": {"runs": len(rows), "passed": len(rows) - failed, "failed": failed},
        "results": rows,
    }


def test_evidence_fixture_names_are_unique() -> None:
    names = [scenario.name for scenario in SCENARIOS]
    assert len(names) == len(set(names))


def test_live_qwen_interprets_evidence_and_answers_conversationally() -> None:
    if os.getenv(RUN_ENV, "").strip().casefold() not in {"1", "true", "yes", "on"}:
        pytest.skip(f"set {RUN_ENV}=1 to run the local Qwen evidence acceptance suite")
    target = configured_worker_target()
    if not worker_is_healthy(target):
        pytest.skip(f"configured local worker is not healthy at {target.endpoint}")
    report = run_suite(target)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    failures = [row for row in report["results"] if row["status"] == "failed"]
    assert not failures, json.dumps(failures, indent=2, ensure_ascii=False)


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", action="append", dest="scenarios")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    target = configured_worker_target()
    if not worker_is_healthy(target):
        print(json.dumps({"status": "skipped", "reason": "configured local worker is not healthy"}))
        return 2
    report = run_suite(
        target,
        names=set(args.scenarios or []) or None,
        repeat=max(1, args.repeat),
        timeout=args.timeout,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if report["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
