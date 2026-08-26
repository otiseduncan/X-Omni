"""Regression coverage for the same-turn duplicate read-only tool-call guard.

Live field trace: after the ALLDATA login card ran and "retrieve SI
information please" fell through to unrouted model tool choice, the model
issued six back-to-back identical adas_si_inventory calls in one turn
because none of them named a specific document to change context. The
underlying bug (wrong tool chosen for a continuation message) is fixed
separately in core/services/calibration_iq_work_prep.py; this guard is the
independent safety net so a repeated read-only request never re-runs its
handler or re-renders its card more than once per turn.

These tests call Orchestrator._execute directly (the exact code path the
round loop in Orchestrator._run drives) rather than routing free text
through run_turn. Several other installed services (research task
continuity, full research, work prep) globally wrap Orchestrator._run and
read/write real on-disk state keyed by conversation text, which makes
free-text routing in an isolated test process-order-dependent -- exercising
_execute directly keeps this test focused on the dedup mechanism itself.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.orchestrator.loop import Orchestrator
from core.state.db import Store
from core.tools.registry import Registry, calibration_iq_evidence_from_result


class _Router:
    active_name = "omni"

    @staticmethod
    def active_config():
        return SimpleNamespace(supports_vision=True, supports_audio=True)


def _orchestrator(store: Store, calls: list, *, handler_name: str = "adas_si_inventory"):
    registry = Registry("config/tools.yaml", store=store)

    async def handler(args: dict) -> dict:
        calls.append(dict(args))
        return {"status": "verified", "documents": [{"title": "2023 Acura TLX front camera"}]}

    registry.register(handler_name, handler)
    return Orchestrator(
        _Router(),
        None,
        registry,
        store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )


async def _drain(
    orchestrator, name, args, messages, artifacts, *,
    conversation_id, call_id, call_cache, approval_context=None,
    calibration_iq_evidence=None,
):
    return [
        event async for event in orchestrator._execute(  # noqa: SLF001
            name, args, messages, artifacts,
            conversation_id=conversation_id,
            approval_context=approval_context,
            call_id=call_id,
            call_cache=call_cache,
            calibration_iq_evidence=calibration_iq_evidence,
        )
    ]


def _exact_ro_evidence(conversation_id: int, message_id: int):
    return calibration_iq_evidence_from_result(
        "calibration_iq_ro",
        {
            "status": "verified",
            "repair_order": {"id": "ro-1", "RO": "2400911667", "version": 7},
            "raw": {
                "repair_order": {
                    "id": "ro-1", "ro_number": "2400911667", "version": 7,
                },
            },
        },
        conversation_id=conversation_id,
        message_id=message_id,
        source_tool_call_id="exact-ro-call",
    )


def test_identical_read_only_calls_share_one_execution_and_one_card(tmp_path):
    store = Store(tmp_path / "dedup.sqlite")
    conversation_id = store.create_conversation("ADAS SI")
    calls: list = []
    orchestrator = _orchestrator(store, calls)

    messages: list = []
    artifacts: list = []
    call_cache: dict = {}

    async def run():
        results = []
        for i in range(6):
            results.append(await _drain(
                orchestrator, "adas_si_inventory", {}, messages, artifacts,
                conversation_id=conversation_id, call_id=f"call_{i}", call_cache=call_cache,
            ))
        return results

    all_events = asyncio.run(run())

    assert len(calls) == 1

    tool_results = [event for events in all_events for event in events if event["type"] == "tool_result"]
    assert len(tool_results) == 6
    assert sum(1 for event in tool_results if event.get("deduplicated")) == 5
    assert all(event["result"] == tool_results[0]["result"] for event in tool_results)

    artifact_events = [event for events in all_events for event in events if event["type"] == "artifact"]
    assert len(artifact_events) == 1

    tool_messages = [message for message in messages if message.get("role") == "tool"]
    assert len(tool_messages) == 6
    assert {message["tool_call_id"] for message in tool_messages} == {f"call_{i}" for i in range(6)}
    store.close()


def test_calls_with_different_arguments_each_execute(tmp_path):
    store = Store(tmp_path / "dedup-distinct.sqlite")
    conversation_id = store.create_conversation("ADAS SI")
    calls: list = []
    orchestrator = _orchestrator(store, calls, handler_name="adas_si_search")

    messages: list = []
    artifacts: list = []
    call_cache: dict = {}

    async def run():
        await _drain(
            orchestrator, "adas_si_search", {"query": "2023 Acura TLX front camera"},
            messages, artifacts, conversation_id=conversation_id, call_id="call_0", call_cache=call_cache,
        )
        await _drain(
            orchestrator, "adas_si_search", {"query": "2021 Jeep Cherokee BSM"},
            messages, artifacts, conversation_id=conversation_id, call_id="call_1", call_cache=call_cache,
        )

    asyncio.run(run())
    assert len(calls) == 2
    store.close()


def test_operator_authorized_tool_is_never_deduplicated(tmp_path):
    # Only read_only tier is safe to reuse across identical calls; an
    # operator-authorized action must always run, even if requested twice
    # with identical arguments in one turn (e.g. two intentional retries).
    store = Store(tmp_path / "dedup-operator.sqlite")
    conversation_id = store.create_conversation("Calibration IQ")
    message_id = store.add_message(conversation_id, "user", "add a note twice")
    calls: list = []
    orchestrator = _orchestrator(store, calls, handler_name="calibration_iq_operator")

    messages: list = []
    artifacts: list = []
    call_cache: dict = {}
    args = {"actions": [{"operation": "add_note", "repair_order_id": "ro-1", "arguments": {"body": "hi"}}]}
    approval_context = {"message_id": message_id, "user_id": "local-dev", "role": "owner"}
    evidence = _exact_ro_evidence(conversation_id, message_id)

    async def run():
        await _drain(
            orchestrator, "calibration_iq_operator", args, messages, artifacts,
            conversation_id=conversation_id, call_id="call_0", call_cache=call_cache,
            approval_context=approval_context,
            calibration_iq_evidence=evidence,
        )
        await _drain(
            orchestrator, "calibration_iq_operator", args, messages, artifacts,
            conversation_id=conversation_id, call_id="call_1", call_cache=call_cache,
            approval_context=approval_context,
            calibration_iq_evidence=evidence,
        )

    asyncio.run(run())
    assert len(calls) == 2
    store.close()


def test_research_ro_composite_is_deduplicated_like_a_read(tmp_path):
    # research_ro is the one operator_authorized exception: retrying it for
    # the same RO after a "missing documentation" result re-runs the same
    # ADAS SI search and reports the same finding, and doing that repeatedly
    # inside one sweep is what blew a turn's context budget. It must be
    # absorbed by the same cache read_only calls use.
    store = Store(tmp_path / "dedup-research-ro.sqlite")
    conversation_id = store.create_conversation("Calibration IQ")
    message_id = store.add_message(conversation_id, "user", "research this RO")
    calls: list = []
    orchestrator = _orchestrator(store, calls, handler_name="calibration_iq_operator")

    messages: list = []
    artifacts: list = []
    call_cache: dict = {}
    args = {"actions": [{"operation": "research_ro", "repair_order_id": "ro-1", "arguments": {}}]}
    approval_context = {"message_id": message_id, "user_id": "local-dev", "role": "owner"}
    evidence = _exact_ro_evidence(conversation_id, message_id)

    async def run():
        await _drain(
            orchestrator, "calibration_iq_operator", args, messages, artifacts,
            conversation_id=conversation_id, call_id="call_0", call_cache=call_cache,
            approval_context=approval_context,
            calibration_iq_evidence=evidence,
        )
        return await _drain(
            orchestrator, "calibration_iq_operator", args, messages, artifacts,
            conversation_id=conversation_id, call_id="call_1", call_cache=call_cache,
            approval_context=approval_context,
            calibration_iq_evidence=evidence,
        )

    second_events = asyncio.run(run())
    assert len(calls) == 1
    assert any(event.get("deduplicated") for event in second_events if event["type"] == "tool_result")
    store.close()


def test_research_ro_mixed_with_another_operation_is_not_deduplicated(tmp_path):
    # Only a composite made up entirely of research_ro actions is safe to
    # absorb -- one mixed in with a real mutation must always run for real.
    store = Store(tmp_path / "dedup-research-ro-mixed.sqlite")
    conversation_id = store.create_conversation("Calibration IQ")
    message_id = store.add_message(conversation_id, "user", "research and add a note")
    calls: list = []
    orchestrator = _orchestrator(store, calls, handler_name="calibration_iq_operator")

    messages: list = []
    artifacts: list = []
    call_cache: dict = {}
    args = {
        "actions": [
            {"operation": "research_ro", "repair_order_id": "ro-1", "arguments": {}},
            {"operation": "add_note", "repair_order_id": "ro-1", "arguments": {"body": "hi"}},
        ]
    }
    approval_context = {"message_id": message_id, "user_id": "local-dev", "role": "owner"}
    evidence = _exact_ro_evidence(conversation_id, message_id)

    async def run():
        await _drain(
            orchestrator, "calibration_iq_operator", args, messages, artifacts,
            conversation_id=conversation_id, call_id="call_0", call_cache=call_cache,
            approval_context=approval_context,
            calibration_iq_evidence=evidence,
        )
        await _drain(
            orchestrator, "calibration_iq_operator", args, messages, artifacts,
            conversation_id=conversation_id, call_id="call_1", call_cache=call_cache,
            approval_context=approval_context,
            calibration_iq_evidence=evidence,
        )

    asyncio.run(run())
    assert len(calls) == 2
    store.close()
