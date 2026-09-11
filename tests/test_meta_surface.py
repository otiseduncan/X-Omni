"""Contract tests for the permanent meta-tool surface.

query_ciq expands structurally to the concrete read handlers; stage_action
reads the exact RO fresh and either stages a contract or executes through the
same gateway path a direct write would take (binding, approvals, receipts);
capability_search unlocks discoverable tools for one turn; delegate_research
orders sources from structured fields and stops at the first verified
finding. No user prose is interpreted anywhere on these paths.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.main import configured_profile_catalog
from core.orchestrator.loop import Orchestrator, TurnMetrics, artifacts_for_result
from core.services import research_delegate
from core.state.db import Store
from core.tools import meta
from core.tools.registry import NeedsApproval, Registry, ToolError


def _catalog_loaded() -> None:
    configured_profile_catalog(
        SimpleNamespace(tools_config="config/tools.yaml", tool_profile="adas_operator")
    )


def _ro_read(version: int = 7, *, targets: bool = True) -> dict[str, Any]:
    raw_ro = {
        "id": "ro-uuid-1",
        "ro_number": "2400911779",
        "version": version,
        "status": "Repair In Progress",
        "phase": 5,
        "shop": "Warner Robins",
    }
    raw: dict[str, Any] = {
        "repair_order": raw_ro,
        "workflow": {"status": "Repair In Progress", "phase": "5", "version": version},
    }
    if targets:
        raw["calibrations"] = [
            {"id": "cal-1", "version": 3, "name": "Front radar"},
            {"id": "cal-2", "version": 1, "name": "Blind spot"},
        ]
        raw["blockers"] = [{"id": "blk-1", "version": 2, "title": "Alignment"}]
    return {
        "status": "verified",
        "repair_order": {
            "id": "ro-uuid-1",
            "RO": "2400911779",
            "version": version,
            "Status": "Repair In Progress",
            "Phase": 5,
            "Shop": "Warner Robins",
        },
        "raw": raw,
    }


# ---------------------------------------------------------------- query_ciq


def test_query_ciq_expands_every_kind_to_its_concrete_read() -> None:
    assert meta.expand_query_ciq({"kind": "ro", "repair_order_id": "11779", "shop": "Warner Robins"}) == (
        "calibration_iq_ro",
        {"repair_order_id": "11779", "shop": "Warner Robins"},
    )
    assert meta.expand_query_ciq({"kind": "board_count", "shop": "Macon", "phase": "5"}) == (
        "calibration_iq_summary",
        {"shop": "Macon", "phase": "5"},
    )
    assert meta.expand_query_ciq({"kind": "board_list", "shop": "Perry", "limit": 10}) == (
        "calibration_iq_read",
        {"shop": "Perry", "limit": 10},
    )
    # One finished-scope enum stands in for the two backend booleans.
    assert meta.expand_query_ciq({"kind": "board_count", "shop": "Macon", "finished": "exclude"}) == (
        "calibration_iq_summary", {"shop": "Macon"},
    )
    assert meta.expand_query_ciq({"kind": "board_count", "shop": "Macon", "finished": "include"}) == (
        "calibration_iq_summary", {"shop": "Macon", "include_completed": True},
    )
    assert meta.expand_query_ciq({"kind": "board_list", "finished": "only"}) == (
        "calibration_iq_read", {"terminal_only": True},
    )
    assert meta.expand_query_ciq({"kind": "phase_list", "phase": "3"}) == (
        "calibration_iq_work_prep",
        {"mode": "phase_list", "phase": "3"},
    )
    assert meta.expand_query_ciq({"kind": "ro_requirements", "repair_order_id": "ro-1"}) == (
        "calibration_iq_work_prep",
        {"mode": "ro_requirements", "repair_order_id": "ro-1"},
    )
    assert meta.expand_query_ciq({"kind": "adas_map_inventory", "phases": ["5", "6"]}) == (
        "calibration_iq_work_prep",
        {"mode": "adas_map_inventory", "phases": ["5", "6"]},
    )
    assert meta.expand_query_ciq({"kind": "status"}) == ("calibration_iq_status", {})


def test_query_ciq_rejects_unknown_kinds_and_missing_identity() -> None:
    with pytest.raises(ValueError, match="kind must be one of"):
        meta.expand_query_ciq({"kind": "everything"})
    with pytest.raises(ValueError, match="requires repair_order_id"):
        meta.expand_query_ciq({"kind": "ro"})
    with pytest.raises(ValueError, match="requires phase"):
        meta.expand_query_ciq({"kind": "phase_list"})
    # Fields that do not belong to the kind are dropped, never forwarded.
    assert meta.expand_query_ciq({"kind": "status", "shop": "Macon"}) == (
        "calibration_iq_status", {},
    )


@pytest.mark.asyncio
async def test_registry_invoke_expands_query_ciq_through_the_gateway(tmp_path) -> None:
    _catalog_loaded()
    store = Store(tmp_path / "meta.sqlite")
    conversation_id = store.create_conversation("meta")
    message_id = store.add_message(conversation_id, "user", "phase?")
    registry = Registry("config/tools.yaml", store=store, profile="adas_operator")
    seen: list[dict[str, Any]] = []

    async def exact_ro(args: dict[str, Any]) -> dict[str, Any]:
        seen.append(args)
        return _ro_read()

    registry.register("calibration_iq_ro", exact_ro)

    result = await registry.invoke(
        "query_ciq",
        {"kind": "ro", "repair_order_id": "11779", "shop": "Warner Robins"},
        message_id=message_id,
        conversation_id=conversation_id,
        tool_call_id="call-1",
        user_id="local-dev",
        role="owner",
    )
    assert result["status"] == "verified"
    assert seen == [{"repair_order_id": "11779", "shop": "Warner Robins"}]
    store.close()


# ------------------------------------------------------------- stage_action


def test_plan_stage_action_stages_close_ro_until_the_fresh_version_matches() -> None:
    _catalog_loaded()
    kind, payload = meta.plan_stage_action(
        {"operation": "close_ro", "repair_order_id": "11779"}, _ro_read(7)
    )
    assert kind == "staged"
    assert payload["stage"] == "staged"
    assert payload["executed"] is False and payload["mutated"] is False
    assert payload["expected_version"] == 7
    assert payload["repair_order_id"] == "ro-uuid-1"
    assert payload["repair_order"]["ro_number"] == "2400911779"
    assert payload["tool"] == "calibration_iq_operator"
    assert any("expected_version" in reason for reason in payload["reasons"])
    assert payload["argument_contract"]["tool"] == "calibration_iq_operator"

    stale_kind, stale = meta.plan_stage_action(
        {"operation": "close_ro", "repair_order_id": "11779", "expected_version": 6},
        _ro_read(7),
    )
    assert stale_kind == "staged"
    assert "current version 7" in " ".join(stale["reasons"])

    kind, execute = meta.plan_stage_action(
        {"operation": "close_ro", "repair_order_id": "11779", "expected_version": 7},
        _ro_read(7),
    )
    assert kind == "execute"
    assert execute == (
        "calibration_iq_operator",
        {
            "actions": [
                {
                    "operation": "close_ro",
                    "arguments": {},
                    "repair_order_id": "ro-uuid-1",
                    "expected_version": 7,
                }
            ]
        },
    )


def test_plan_stage_action_child_operations_need_a_current_target_and_version() -> None:
    _catalog_loaded()
    kind, staged = meta.plan_stage_action(
        {"operation": "complete_calibration", "repair_order_id": "11779"}, _ro_read()
    )
    assert kind == "staged"
    assert staged["target_kind"] == "calibration"
    assert {target["target_id"] for target in staged["targets"]} == {"cal-1", "cal-2"}
    assert all(target["kind"] == "calibration" for target in staged["targets"])

    kind, wrong = meta.plan_stage_action(
        {
            "operation": "complete_calibration",
            "repair_order_id": "11779",
            "target_id": "blk-1",
            "expected_version": 2,
        },
        _ro_read(),
    )
    assert kind == "staged"
    assert "not a current child" in " ".join(wrong["reasons"])

    # A wrong version on a known target hands back the exact next call.
    kind, stale = meta.plan_stage_action(
        {
            "operation": "delete_calibration",
            "repair_order_id": "11779",
            "target_id": "cal-2",
            "expected_version": 7,
        },
        _ro_read(),
    )
    assert kind == "staged"
    assert stale["next_call"] == {
        "operation": "delete_calibration",
        "repair_order_id": "ro-uuid-1",
        "target_id": "cal-2",
        "expected_version": 1,
    }
    assert "approval card" in stale["approval"]
    kind, staged_ro = meta.plan_stage_action(
        {"operation": "close_ro", "repair_order_id": "11779"}, _ro_read(7)
    )
    assert staged_ro["next_call"] == {
        "operation": "close_ro",
        "repair_order_id": "ro-uuid-1",
        "expected_version": 7,
    }

    kind, execute = meta.plan_stage_action(
        {
            "operation": "complete_calibration",
            "repair_order_id": "11779",
            "target_id": "cal-1",
            "expected_version": 3,
        },
        _ro_read(),
    )
    assert kind == "execute"
    tool, args = execute
    assert tool == "calibration_iq_operator"
    assert args["actions"][0]["target_id"] == "cal-1"
    assert args["actions"][0]["expected_version"] == 3
    assert args["actions"][0]["repair_order_id"] == "ro-uuid-1"

    kind, destructive = meta.plan_stage_action(
        {
            "operation": "delete_calibration",
            "repair_order_id": "11779",
            "target_id": "cal-2",
            "expected_version": 1,
        },
        _ro_read(),
    )
    assert kind == "execute"
    assert destructive[0] == "calibration_iq_destructive"


def test_plan_stage_action_refuses_a_child_target_on_a_whole_ro_operation() -> None:
    """Seen live: 'remove the blind spot calibration' became
    mark_no_calibration_required with target_id=cal-bsm-1 and the RO version.
    A whole-RO operation must not silently execute with a child attached; it
    stages and names the one-child operations for that target's kind."""

    _catalog_loaded()
    kind, staged = meta.plan_stage_action(
        {
            "operation": "mark_no_calibration_required",
            "repair_order_id": "11779",
            "target_id": "cal-2",
            "expected_version": 7,
        },
        _ro_read(7),
    )
    assert kind == "staged"
    assert staged["target_kind"] == "calibration"
    assert "delete_calibration" in staged["one_child_alternatives"]
    assert "complete_calibration" in staged["one_child_alternatives"]
    assert "takes no target_id" in " ".join(staged["reasons"])

    kind, execute = meta.plan_stage_action(
        {"operation": "mark_no_calibration_required", "repair_order_id": "11779", "expected_version": 7},
        _ro_read(7),
    )
    assert kind == "execute"


def test_plan_stage_action_validates_operation_arguments_against_the_contract() -> None:
    _catalog_loaded()
    kind, staged = meta.plan_stage_action(
        {
            "operation": "change_status",
            "repair_order_id": "11779",
            "expected_version": 7,
            "arguments": {},
        },
        _ro_read(7),
    )
    assert kind == "staged"
    assert any(reason.startswith("arguments") for reason in staged["reasons"])
    assert staged["argument_contract"]["arguments"]["type"] == "object"

    with pytest.raises(ValueError, match="not a stageable"):
        meta.plan_stage_action({"operation": "create_ro"}, _ro_read())
    with pytest.raises(ValueError, match="verified fresh exact-RO read"):
        meta.plan_stage_action({"operation": "close_ro"}, {"status": "offline"})


@pytest.mark.asyncio
async def test_stage_action_reads_fresh_then_stages_then_executes_with_binding(tmp_path) -> None:
    _catalog_loaded()
    store = Store(tmp_path / "stage.sqlite")
    conversation_id = store.create_conversation("stage")
    message_id = store.add_message(conversation_id, "user", "close it out")
    registry = Registry("config/tools.yaml", store=store, profile="adas_operator")
    reads: list[dict[str, Any]] = []
    writes: list[dict[str, Any]] = []

    async def exact_ro(args: dict[str, Any]) -> dict[str, Any]:
        reads.append(args)
        return _ro_read(7)

    async def operator(args: dict[str, Any]) -> dict[str, Any]:
        writes.append(args)
        return {
            "status": "success",
            "success": True,
            "verified": True,
            "executed": True,
            "partial": False,
            "requested_count": 1,
            "processed_count": 1,
            "receipts": [
                {
                    "status": "completed",
                    "success": True,
                    "operation": "close_ro",
                    "repair_order_id": "ro-uuid-1",
                    "verification": {"verified": True},
                }
            ],
            "final_snapshots": {"ro-uuid-1": {"status": "verified", "snapshot": _ro_read(8)["raw"]}},
        }

    registry.register("calibration_iq_ro", exact_ro)
    registry.register("calibration_iq_operator", operator)
    context = dict(
        message_id=message_id,
        conversation_id=conversation_id,
        tool_call_id="call-stage",
        user_id="local-dev",
        role="owner",
    )

    staged = await registry.invoke(
        "stage_action", {"operation": "close_ro", "repair_order_id": "11779", "shop": "Warner Robins"}, **context
    )
    assert staged["stage"] == "staged"
    assert staged["expected_version"] == 7
    assert staged["repair_order_read"]["status"] == "verified"
    assert reads == [{"repair_order_id": "11779", "shop": "Warner Robins"}]
    assert writes == []

    executed = await registry.invoke(
        "stage_action",
        {"operation": "close_ro", "repair_order_id": "11779", "shop": "Warner Robins", "expected_version": 7},
        **context,
    )
    assert executed["stage"] == "executed"
    assert executed["executed"] is True and executed["mutated"] is True
    assert executed["executed_via"] == "calibration_iq_operator"
    assert executed["execution"]["verified"] is True
    assert len(reads) == 2
    action = writes[0]["actions"][0]
    assert action["operation"] == "close_ro"
    assert action["repair_order_id"] == "ro-uuid-1"
    assert action["expected_version"] == 7
    # The gateway injected its own invocation identity; the model never chose it.
    assert writes[0]["__xomni_invocation"]["tool_call_id"] == "call-stage"

    # Cards: the staged result renders the fresh RO; the executed result the receipt.
    assert [card_type for card_type, _ in artifacts_for_result("stage_action", staged)] == ["calibration_iq_ro"]
    assert [card_type for card_type, _ in artifacts_for_result("stage_action", executed)] == ["calibration_iq_receipt"]
    logged = [row for row in store.list_tool_calls(conversation_id)] if hasattr(store, "list_tool_calls") else []
    assert logged == [] or any(row.get("tool_name") == "stage_action" for row in logged)
    store.close()


@pytest.mark.asyncio
async def test_stage_action_destructive_raises_approval_for_the_concrete_tool(tmp_path) -> None:
    _catalog_loaded()
    store = Store(tmp_path / "stage-destructive.sqlite")
    conversation_id = store.create_conversation("destructive")
    message_id = store.add_message(conversation_id, "user", "remove that blind spot calibration")
    registry = Registry("config/tools.yaml", store=store, profile="adas_operator")
    registry.register("calibration_iq_ro", lambda _args: _ro_read())
    registry.register("calibration_iq_destructive", lambda _args: {"executed": True})

    with pytest.raises(NeedsApproval) as pending:
        await registry.invoke(
            "stage_action",
            {
                "operation": "delete_calibration",
                "repair_order_id": "2400911779",
                "target_id": "cal-2",
                "expected_version": 1,
            },
            message_id=message_id,
            conversation_id=conversation_id,
            tool_call_id="call-remove",
            user_id="local-dev",
            role="owner",
        )
    assert pending.value.tool_name == "calibration_iq_destructive"
    action = pending.value.tool_args["actions"][0]
    assert action["operation"] == "delete_calibration"
    assert action["target_id"] == "cal-2"
    assert "__xomni_write_binding" in pending.value.tool_args
    store.close()


@pytest.mark.asyncio
async def test_stage_action_read_failure_stages_nothing_and_acquire_uses_full_ro_number(tmp_path) -> None:
    _catalog_loaded()
    store = Store(tmp_path / "stage-read.sqlite")
    conversation_id = store.create_conversation("read")
    message_id = store.add_message(conversation_id, "user", "close 99999")
    registry = Registry("config/tools.yaml", store=store, profile="adas_operator")
    registry.register("calibration_iq_ro", lambda _args: {"status": "offline"})
    writes: list[dict[str, Any]] = []
    registry.register("calibration_iq_operator", lambda args: writes.append(args) or {})
    acquisitions: list[dict[str, Any]] = []
    registry.register(
        "scrapex_adas_map",
        lambda args: acquisitions.append(args) or {"executed": True, "verified": True, "status": "verified"},
    )
    context = dict(
        message_id=message_id,
        conversation_id=conversation_id,
        tool_call_id="call-x",
        user_id="local-dev",
        role="owner",
    )

    failed = await registry.invoke("stage_action", {"operation": "close_ro", "repair_order_id": "99999", "shop": "Macon"}, **context)
    assert failed["stage"] == "read_failed"
    assert failed["executed"] is False and failed["mutated"] is False
    assert writes == []

    with pytest.raises(ToolError, match="needs repair_order_id"):
        await registry.invoke("stage_action", {"operation": "close_ro"}, **context)

    registry.register("calibration_iq_ro", lambda _args: _ro_read())
    acquired = await registry.invoke(
        "stage_action", {"operation": "acquire_adas_map", "repair_order_id": "11779", "shop": "Warner Robins"}, **context
    )
    assert acquired["stage"] == "executed"
    assert acquired["executed_via"] == "scrapex_adas_map"
    assert acquisitions[0]["action"] == "acquire_exact"
    assert acquisitions[0]["ro_number"] == "2400911779"
    assert acquisitions[0]["__xomni_invocation"]["conversation_id"] == conversation_id
    assert [card_type for card_type, _ in artifacts_for_result("stage_action", acquired)] == ["scrapex"]
    store.close()


# ------------------------------------------------------- orchestrator wiring


class _Router:
    active_name = "omni"

    @staticmethod
    def active_config():
        return SimpleNamespace(supports_vision=True, supports_audio=True)


class _ScriptedClient:
    supports_no_tool_self_check = False

    def __init__(self, rounds: list[list[dict[str, Any]]]) -> None:
        self.rounds = rounds
        self.tools_seen: list[list[str]] = []

    async def stream(self, messages, tools=None, **_kwargs):
        self.tools_seen.append([item["function"]["name"] for item in (tools or [])])
        events = self.rounds.pop(0) if self.rounds else [{"type": "content", "text": "done"}]
        for event in events:
            yield event
        yield {
            "type": "usage",
            "usage": {"prompt_tokens": 1000, "completion_tokens": 20},
            "timings": {"cache_n": 900, "prompt_n": 100, "prompt_ms": 50.0, "predicted_n": 20, "predicted_ms": 400.0},
        }


def _orchestrator(client, registry, store) -> Orchestrator:
    return Orchestrator(
        _Router(), client, registry, store,
        SimpleNamespace(context_tokens=32_768, max_response_tokens=1_024),
    )


def _context(message_id: int) -> dict[str, Any]:
    return {
        "session_id": "local:local-dev",
        "user_id": "local-dev",
        "role": "owner",
        "message_id": message_id,
    }


@pytest.mark.asyncio
async def test_orchestrator_expands_query_ciq_renders_ro_card_and_reports_metrics(tmp_path) -> None:
    _catalog_loaded()
    store = Store(tmp_path / "loop-query.sqlite")
    conversation_id = store.create_conversation("query")
    message_id = store.add_message(conversation_id, "user", "What phase is 11779 in Warner Robins in?")
    registry = Registry("config/tools.yaml", store=store, profile="adas_operator")
    registry.register("calibration_iq_ro", lambda _args: _ro_read())
    for name in ("delegate_research", "capability_search"):
        registry.register(name, lambda _args: {})
    client = _ScriptedClient([
        [{
            "type": "tool_call", "id": "c1", "name": "query_ciq",
            "arguments": json.dumps({"kind": "ro", "repair_order_id": "11779", "shop": "Warner Robins"}),
        }],
        [{"type": "content", "text": "RO 11779 in Warner Robins is in phase 5."}],
    ])

    events = [
        event async for event in _orchestrator(client, registry, store).run_turn(
            conversation_id, "What phase is 11779 in Warner Robins in?", approval_context=_context(message_id)
        )
    ]

    start = next(event for event in events if event["type"] == "tool_start")
    assert start["name"] == "calibration_iq_ro"
    assert start["requested_as"] == "query_ciq"
    assert start["args"] == {"repair_order_id": "11779", "shop": "Warner Robins"}
    cards = [event["artifact"]["type"] for event in events if event["type"] == "artifact"]
    assert cards == ["calibration_iq_ro"]
    assert client.tools_seen[0] == list(meta.PERMANENT_TOOLS)
    done = events[-1]
    assert done["type"] == "done"
    metrics = done["metrics"]
    assert metrics["tools_selected"] == ["query_ciq->calibration_iq_ro"]
    assert metrics["model_calls"] == 2
    assert metrics["cached_tokens"] == 1800 and metrics["evaluated_tokens"] == 200
    assert metrics["tool_rounds"] == 2
    assert metrics["advertised_tools"] == list(meta.PERMANENT_TOOLS)
    assert metrics["tool_schema_tokens_estimate"] < 2_350
    assert metrics["reserved_tool_schema_tokens_estimate"] > metrics["tool_schema_tokens_estimate"]
    subject = store.get_conversation_subject(conversation_id)
    assert subject["payload"]["ro_number"] == "2400911779"
    store.close()


@pytest.mark.asyncio
async def test_orchestrator_unlocks_discovered_tools_for_later_rounds_only(tmp_path) -> None:
    _catalog_loaded()
    store = Store(tmp_path / "loop-unlock.sqlite")
    conversation_id = store.create_conversation("unlock")
    message_id = store.add_message(conversation_id, "user", "What's on my calendar tomorrow?")
    registry = Registry("config/tools.yaml", store=store, profile="adas_operator")
    from core.tools.builtin import system as builtin

    for item in registry.profile_catalog():
        registry.register(item["function"]["name"], lambda _args: {})
    registry.register("capability_search", builtin.make_capability_search(_Router(), registry))
    registry.register("get_calendar", lambda _args: {"ok": True, "events": []})
    client = _ScriptedClient([
        [{"type": "tool_call", "id": "c1", "name": "capability_search", "arguments": json.dumps({"query": "calendar"})}],
        [{"type": "tool_call", "id": "c2", "name": "get_calendar", "arguments": json.dumps({"days": 1})}],
        [{"type": "content", "text": "Nothing on the calendar tomorrow."}],
    ])

    events = [
        event async for event in _orchestrator(client, registry, store).run_turn(
            conversation_id, "What's on my calendar tomorrow?", approval_context=_context(message_id)
        )
    ]

    assert client.tools_seen[0] == list(meta.PERMANENT_TOOLS)
    assert "get_calendar" in client.tools_seen[1]
    assert "calibration_iq_operator" not in client.tools_seen[1]
    starts = [event["name"] for event in events if event["type"] == "tool_start"]
    assert starts == ["capability_search", "get_calendar"]
    cards = [event["artifact"]["type"] for event in events if event["type"] == "artifact"]
    assert cards == ["capabilities", "calendar"]
    metrics = events[-1]["metrics"]
    assert "get_calendar" in metrics["unlocked_tools"]
    assert metrics["tools_selected"] == ["capability_search", "get_calendar"]
    store.close()


@pytest.mark.asyncio
async def test_orchestrator_malformed_meta_call_is_a_tool_error_not_a_crash(tmp_path) -> None:
    _catalog_loaded()
    store = Store(tmp_path / "loop-malformed.sqlite")
    conversation_id = store.create_conversation("malformed")
    message_id = store.add_message(conversation_id, "user", "hi")
    registry = Registry("config/tools.yaml", store=store, profile="adas_operator")
    for name in ("delegate_research", "capability_search"):
        registry.register(name, lambda _args: {})
    client = _ScriptedClient([
        [{"type": "tool_call", "id": "c1", "name": "query_ciq", "arguments": json.dumps({"kind": "ro"})}],
        [{"type": "content", "text": "I need the RO number."}],
    ])

    events = [
        event async for event in _orchestrator(client, registry, store).run_turn(
            conversation_id, "hi", approval_context=_context(message_id)
        )
    ]
    result = next(event for event in events if event["type"] == "tool_result")
    assert result["name"] == "query_ciq"
    assert result["result"]["status"] == "error"
    assert "requires repair_order_id" in result["result"]["message"]
    assert events[-1]["type"] == "done"
    store.close()


def test_turn_metrics_absorb_and_summarize_llama_timings() -> None:
    metrics = TurnMetrics(started=0.0)
    metrics.absorb({"usage": {"prompt_tokens": 500}, "timings": {"cache_n": 450, "prompt_n": 50, "prompt_ms": 20.5, "predicted_n": 12, "predicted_ms": 300.0}})
    metrics.absorb({"usage": {}, "timings": {}})
    summary = metrics.summary(extra="x")
    assert summary["model_calls"] == 2
    assert summary["prompt_tokens"] == 500
    assert summary["cached_tokens"] == 450
    assert summary["evaluated_tokens"] == 50
    assert summary["prompt_ms"] == 20 or summary["prompt_ms"] == 21
    assert summary["generated_tokens"] == 12
    assert summary["extra"] == "x"


# ---------------------------------------------------------- delegate_research


def _adas_hit(**overrides: Any) -> dict[str, Any]:
    return {
        "status": "success",
        "evidence_id": "adas-hit-1",
        "results": [
            {
                "title": "Blind Spot Monitor Calibration",
                "relative_path": "Toyota/Camry/2023/bsm.pdf",
                "page": 4,
                "excerpt": "Place the reflector target 1.5 m behind the bumper.",
                "url": "/api/adas-si/document?path=x",
            }
        ],
        **overrides,
    }


def _run(handler, args):
    import asyncio

    return asyncio.run(handler(args))


def test_delegate_research_stops_at_first_verified_source_in_default_order() -> None:
    calls: list[str] = []

    def adas(args):
        calls.append("adas_si")
        assert args["question"].startswith("blind-spot")
        assert args["vehicle"] == {"year": 2023, "make": "Toyota", "model": "Camry"}
        return _adas_hit()

    def knowledge(args):
        calls.append("automotive_knowledge")
        return {"status": "no_result", "records": []}

    handler = research_delegate.make_delegate_research(
        SimpleNamespace(), adas_search=adas, knowledge_search=knowledge,
        navigator_search=lambda **_k: calls.append("alldata") or {"verified": False},
        public_search=lambda *_a, **_k: calls.append("web") or {"verified": False},
    )
    result = _run(handler, {
        "objective": "blind-spot calibration procedure",
        "vehicle": {"year": 2023, "make": "Toyota", "model": "Camry"},
    })
    assert calls == ["adas_si"]
    assert result["status"] == "success" and result["verified"] is True
    assert result["sources_checked"] == ["adas_si"]
    assert result["findings"][0]["source"] == "adas_si"
    assert result["findings"][0]["page"] == 4
    assert result["evidence_ids"] == ["adas-hit-1"]
    assert result["mutated_calibration_iq"] is False


def test_delegate_research_honors_exclusions_preferences_and_exhaustive() -> None:
    calls: list[str] = []
    handler = research_delegate.make_delegate_research(
        SimpleNamespace(),
        adas_search=lambda a: calls.append("adas_si") or {"status": "no_result", "results": []},
        knowledge_search=lambda a: calls.append("automotive_knowledge") or {
            "status": "success", "records": [{"id": "rec-1", "title": "Reflector", "summary": "Uses a reflector."}],
        },
        navigator_search=lambda **_k: calls.append("alldata") or {"verified": True, "task_id": "t-1", "source_url": "https://alldata.test/p", "extracted_text": "Procedure text", "provenance": {"provider": "alldata"}},
        public_search=lambda *_a, **_k: calls.append("web") or {"verified": True, "result_count": 1, "sources": [{"url": "https://oem.test/a", "title": "OEM", "snippet": "s"}], "read_results": []},
    )
    excluded = _run(handler, {
        "objective": "does the camry blind spot use a reflector",
        "vehicle": {"year": 2023, "make": "Toyota", "model": "Camry"},
        "exclude_sources": ["alldata"],
        "exhaustive": True,
    })
    assert calls == ["adas_si", "automotive_knowledge", "web"]
    assert excluded["source_order"] == ["adas_si", "automotive_knowledge", "web"]
    assert {finding["source"] for finding in excluded["findings"]} == {"automotive_knowledge", "web"}
    assert excluded["status"] == "partial_success"
    assert "rec-1" in excluded["evidence_ids"]

    calls.clear()
    preferred = _run(handler, {
        "objective": "does the camry blind spot use a reflector",
        "vehicle": {"year": 2023, "make": "Toyota", "model": "Camry"},
        "sources": ["web", "alldata"],
    })
    assert calls == ["web"]
    assert preferred["findings"][0]["source"] == "web"


def test_delegate_research_reports_alldata_auth_boundary_and_skips_without_vehicle() -> None:
    handler = research_delegate.make_delegate_research(
        SimpleNamespace(),
        adas_search=lambda a: {"status": "no_result", "results": []},
        knowledge_search=lambda a: {"status": "no_result", "records": []},
        navigator_search=lambda **_k: {"status": "authentication_required", "requires_human": True, "verified": False, "task_id": "t-9", "reason": "Sign in required."},
        public_search=lambda *_a, **_k: {"verified": False, "sources": [], "read_results": [], "result_count": 0},
    )
    blocked = _run(handler, {
        "objective": "radar aiming spec",
        "vehicle": {"year": 2021, "make": "Nissan", "model": "Rogue"},
        "exclude_sources": ["web"],
    })
    assert blocked["status"] == "blocked"
    assert blocked["authentication_required"] is True and blocked["requires_human"] is True
    assert blocked["findings"] == []
    assert "navigator-task:t-9" in blocked["evidence_ids"]

    no_vehicle = _run(handler, {"objective": "radar aiming spec"})
    ledger = {row["source"]: row for row in no_vehicle["source_ledger"]}
    assert ledger["alldata"]["attempted"] is False
    assert no_vehicle["status"] == "no_result"
    assert no_vehicle["sources_checked"] == ["adas_si", "automotive_knowledge", "web"]

    with pytest.raises(ValueError, match="objective is required"):
        _run(handler, {"objective": ""})
