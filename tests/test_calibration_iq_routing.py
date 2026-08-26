from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.orchestrator.loop import Orchestrator
from core.state.db import Store
from core.tools.registry import Registry


class _Router:
    active_name = "omni"

    @staticmethod
    def active_config():
        return SimpleNamespace(supports_vision=True, supports_audio=True)


class _ScriptedModel:
    def __init__(self, rounds: list[list[dict]]):
        self.rounds = list(rounds)
        self.calls = 0
        self.messages: list[list[dict]] = []

    async def stream(self, messages, tools=None):
        assert tools
        self.calls += 1
        self.messages.append(list(messages))
        for event in self.rounds.pop(0):
            yield event


def _orchestrator(store: Store, client: _ScriptedModel, calls: list[tuple[str, dict]]):
    registry = Registry("config/tools.yaml", store=store)

    async def summary(args: dict) -> dict:
        calls.append(("calibration_iq_summary", dict(args)))
        return {
            "status": "verified",
            "count": 6,
            "filters": dict(args),
            "collection_complete": True,
        }

    async def listing(args: dict) -> dict:
        calls.append(("calibration_iq_read", dict(args)))
        return {
            "status": "verified",
            "count": 1,
            "filters": dict(args),
            "rows": [{"id": "ro-1", "RO": "2400911667"}],
            "collection_complete": True,
        }

    registry.register("calibration_iq_summary", summary)
    registry.register("calibration_iq_read", listing)
    return Orchestrator(
        _Router(),
        client,
        registry,
        store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )


async def _run(store: Store, conversation_id: int, orchestrator: Orchestrator, text: str):
    store.add_message(conversation_id, "user", text)
    return [event async for event in orchestrator.run_turn(conversation_id, text)]


@pytest.mark.asyncio
async def test_calibration_iq_read_is_selected_by_model_not_prerouted(tmp_path):
    store = Store(tmp_path / "ciq-model-first.sqlite")
    conversation_id = store.create_conversation("Calibration IQ")
    calls: list[tuple[str, dict]] = []
    client = _ScriptedModel([
        [{
            "type": "tool_call",
            "id": "call-summary",
            "name": "calibration_iq_summary",
            "arguments": '{"shop":"Macon","phase":"5"}',
        }],
        [{"type": "content", "text": "There are six matching repair orders."}],
    ])
    orchestrator = _orchestrator(store, client, calls)

    events = await _run(
        store,
        conversation_id,
        orchestrator,
        "Could you tell me how many Macon vehicles are sitting in phase five?",
    )

    assert client.calls == 2
    assert calls == [("calibration_iq_summary", {"shop": "Macon", "phase": "5"})]
    assert [event["name"] for event in events if event["type"] == "tool_start"] == [
        "calibration_iq_summary"
    ]
    assert any(event.get("text") == "There are six matching repair orders." for event in events)
    store.close()


@pytest.mark.asyncio
async def test_model_selected_truncated_list_preserves_truth_and_single_card_order(
    tmp_path,
):
    store = Store(tmp_path / "ciq-truncated-card.sqlite")
    conversation_id = store.create_conversation("Calibration IQ")
    rows = [
        {
            "id": f"ro-{index}",
            "RO": f"24009{index:05d}",
            "Vehicle": f"Vehicle {index}",
            "Status": "Research",
            "Shop": "Macon",
            "Phase": 5,
        }
        for index in range(20)
    ]
    result = {
        "status": "verified",
        "count": 59,
        "shown_count": 20,
        "truncated": True,
        "include_completed": False,
        "filters": {"shop": "Macon", "phase": "5"},
        "rows": rows,
        "collection_complete": True,
    }
    invoked: list[dict] = []
    registry = Registry("config/tools.yaml", store=store)

    async def listing(args: dict) -> dict:
        invoked.append(dict(args))
        return result

    registry.register("calibration_iq_read", listing)
    client = _ScriptedModel(
        [
            [
                {
                    "type": "tool_call",
                    "id": "call-truncated-list",
                    "name": "calibration_iq_read",
                    "arguments": '{"shop":"Macon","phase":"5"}',
                }
            ],
            [
                {
                    "type": "content",
                    "text": "Showing 20 of 59 active repair orders in Macon phase 5.",
                }
            ],
        ]
    )
    orchestrator = Orchestrator(
        _Router(),
        client,
        registry,
        store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )

    events = await _run(
        store,
        conversation_id,
        orchestrator,
        "List the active Macon phase-five work.",
    )

    assert invoked == [{"shop": "Macon", "phase": "5"}]
    assert [event["type"] for event in events] == [
        "tool_start",
        "tool_result",
        "artifact",
        "token",
        "done",
    ]
    assert events[1]["result"] == result
    expected_artifact = {"type": "calibration_iq_ros", "data": result}
    assert events[2]["artifact"] == expected_artifact
    assert events[3]["text"] == (
        "Showing 20 of 59 active repair orders in Macon phase 5."
    )
    assert events[4]["artifacts"] == [expected_artifact]

    model_tool_result = next(
        message for message in client.messages[1] if message.get("role") == "tool"
    )
    model_payload = json.loads(model_tool_result["content"])
    assert model_payload["count"] == 59
    assert model_payload["shown_count"] == 20
    assert model_payload["truncated"] is True
    assert len(model_payload["rows"]) == 20

    persisted = store.get_messages(conversation_id)[-1]
    assert persisted["role"] == "assistant"
    assert persisted["content"] == events[3]["text"]
    assert persisted["artifacts"] == [expected_artifact]
    store.close()


@pytest.mark.asyncio
async def test_model_can_select_multiple_calibration_iq_calls_in_one_round(tmp_path):
    store = Store(tmp_path / "ciq-multi-tool.sqlite")
    conversation_id = store.create_conversation("Calibration IQ")
    calls: list[tuple[str, dict]] = []
    client = _ScriptedModel([
        [
            {
                "type": "tool_call",
                "id": "call-perry",
                "name": "calibration_iq_summary",
                "arguments": '{"shop":"Perry"}',
            },
            {
                "type": "tool_call",
                "id": "call-macon",
                "name": "calibration_iq_summary",
                "arguments": '{"shop":"Macon"}',
            },
        ],
        [{"type": "content", "text": "Here is the Perry and Macon comparison."}],
    ])
    orchestrator = _orchestrator(store, client, calls)

    await _run(
        store,
        conversation_id,
        orchestrator,
        "You're only showing Warner Robins; compare Macon with Perry instead.",
    )

    assert calls == [
        ("calibration_iq_summary", {"shop": "Perry"}),
        ("calibration_iq_summary", {"shop": "Macon"}),
    ]
    assert client.calls == 2
    store.close()


@pytest.mark.asyncio
async def test_one_model_round_has_a_hard_tool_call_limit(tmp_path):
    store = Store(tmp_path / "ciq-bounded-multi-tool.sqlite")
    conversation_id = store.create_conversation("Calibration IQ")
    calls: list[tuple[str, dict]] = []
    requested = [
        {
            "type": "tool_call",
            "id": f"call-{index}",
            "name": "calibration_iq_summary",
            "arguments": json.dumps({"shop": f"shop-{index}"}),
        }
        for index in range(12)
    ]
    client = _ScriptedModel(
        [requested, [{"type": "content", "text": "I completed the bounded comparison."}]]
    )
    orchestrator = _orchestrator(store, client, calls)

    await _run(store, conversation_id, orchestrator, "Compare the relevant shops.")

    assert len(calls) == 8
    assert [args["shop"] for _, args in calls] == [f"shop-{index}" for index in range(8)]
    assistant_call = next(
        message for message in client.messages[1] if message.get("tool_calls")
    )
    assert len(assistant_call["tool_calls"]) == 8
    store.close()


@pytest.mark.asyncio
async def test_no_result_returns_to_model_for_next_source_selection(tmp_path):
    store = Store(tmp_path / "model-escalation.sqlite")
    conversation_id = store.create_conversation("Research")
    invocations: list[tuple[str, dict]] = []

    class RegistryForEscalation:
        @staticmethod
        def model_tools(_role="owner"):
            return [
                {
                    "type": "function",
                    "function": {"name": name, "parameters": {"type": "object"}},
                }
                for name in ("adas_si_search", "collision_research")
            ]

        @staticmethod
        def tier(_name):
            return "read_only"

        async def invoke(self, name, args, **_context):
            invocations.append((name, dict(args)))
            if name == "adas_si_search":
                return {"status": "no_result", "results": []}
            return {
                "status": "success",
                "verified": True,
                "sources": [{"url": "https://oem.example/procedure"}],
            }

    client = _ScriptedModel([
        [{
            "type": "tool_call",
            "id": "local-first",
            "name": "adas_si_search",
            "arguments": '{"query":"2024 example camera calibration"}',
        }],
        [{
            "type": "tool_call",
            "id": "oem-next",
            "name": "collision_research",
            "arguments": '{"action":"public_search","query":"2024 example camera calibration"}',
        }],
        [{"type": "content", "text": "I found the verified OEM source."}],
    ])
    orchestrator = Orchestrator(
        _Router(),
        client,
        RegistryForEscalation(),
        store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )

    await _run(
        store,
        conversation_id,
        orchestrator,
        "Find the camera calibration procedure and use another source if needed.",
    )

    assert invocations == [
        ("adas_si_search", {"query": "2024 example camera calibration"}),
        (
            "collision_research",
            {"action": "public_search", "query": "2024 example camera calibration"},
        ),
    ]
    assert client.calls == 3
    first_tool_result = next(
        message for message in client.messages[1] if message.get("role") == "tool"
    )
    assert json.loads(first_tool_result["content"])["status"] == "no_result"
    store.close()
