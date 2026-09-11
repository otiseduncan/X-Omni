"""Turn-loop rules for repeated reads and calls past the per-round limit."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.orchestrator.loop import (
    DEFERRED_CALL_RESULT,
    MAX_TOOL_CALLS_PER_ROUND,
    REPEAT_ONLY_SYNTHESIS_MESSAGE,
    Orchestrator,
)


class _Store:
    def get_messages(self, _conversation_id: int) -> list[dict[str, Any]]:
        return []

    def add_message(self, *args: Any, **kwargs: Any) -> int:
        return 1

    def touch_conversation(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Registry:
    def __init__(self) -> None:
        self.invocations: list[tuple[str, dict[str, Any]]] = []
        self.version = 7

    def model_tools(self, _role: str = "owner", **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
            for name in ("read_status", "close_it")
        ]

    @staticmethod
    def tier(name: str) -> str:
        return "read_only" if name == "read_status" else "operator_authorized"

    async def invoke(self, name: str, args: dict[str, Any], **_kwargs: Any) -> dict:
        self.invocations.append((name, dict(args)))
        if name == "close_it":
            self.version += 1
            return {"status": "success", "executed": True}
        return {"status": "running", "version": self.version}


class _Router:
    active_name = "omni"

    @staticmethod
    def active_config():
        return SimpleNamespace(supports_vision=True, supports_audio=True)


class _ScriptedClient:
    def __init__(self, rounds: list[list[dict[str, Any]]]) -> None:
        self.rounds = rounds
        self.seen: list[dict[str, Any]] = []

    async def stream(self, messages, tools=None, **_kwargs):
        self.seen.append({"messages": list(messages), "tools": list(tools or [])})
        for event in self.rounds.pop(0) if self.rounds else [{"type": "content", "text": "done"}]:
            yield event


def _call(call_id: str, name: str) -> dict[str, Any]:
    return {"type": "tool_call", "id": call_id, "name": name, "arguments": "{}"}


def _orchestrator(client, registry) -> Orchestrator:
    return Orchestrator(
        _Router(), client, registry, _Store(),
        SimpleNamespace(context_tokens=32_768, max_response_tokens=1_024),
    )


async def _run(orchestrator: Orchestrator) -> list[dict[str, Any]]:
    return [event async for event in orchestrator.run_turn(1, "continue")]


@pytest.mark.asyncio
async def test_a_round_of_only_repeated_reads_goes_straight_to_the_final_answer() -> None:
    registry = _Registry()
    client = _ScriptedClient(
        [
            [_call("r1", "read_status")],
            [_call("r2", "read_status")],  # identical repeat: served from cache
            [{"type": "content", "text": "Still running."}],
        ]
    )

    events = await _run(_orchestrator(client, registry))

    assert registry.invocations == [("read_status", {})]
    # The third model call is the synthesis boundary: no tools, the repeat note.
    assert client.seen[2]["tools"] == []
    assert client.seen[2]["messages"][-1]["content"] == REPEAT_ONLY_SYNTHESIS_MESSAGE
    repeat_result = json.loads(
        next(
            message["content"]
            for message in client.seen[2]["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == "r2"
        )
    )
    assert repeat_result["deduplicated"] is True
    assert "repeating it will not change the answer" in repeat_result["note"]
    assert "".join(e["text"] for e in events if e.get("type") == "token") == "Still running."


@pytest.mark.asyncio
async def test_a_mutation_makes_the_next_identical_read_run_fresh() -> None:
    registry = _Registry()
    client = _ScriptedClient(
        [
            [_call("r1", "read_status")],
            [_call("m1", "close_it")],
            [_call("r2", "read_status")],  # after a mutation: must not be cached
            [{"type": "content", "text": "Closed at version 8."}],
        ]
    )

    await _run(_orchestrator(client, registry))

    assert registry.invocations == [
        ("read_status", {}),
        ("close_it", {}),
        ("read_status", {}),
    ]
    fresh = json.loads(
        next(
            message["content"]
            for message in client.seen[3]["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == "r2"
        )
    )
    assert fresh == {"status": "running", "version": 8}
    # The fresh read was new evidence, so the fourth call still had tools.
    assert client.seen[3]["tools"]


@pytest.mark.asyncio
async def test_calls_past_the_round_limit_return_to_the_model_as_not_run() -> None:
    registry = _Registry()
    requested = [
        {"type": "tool_call", "id": f"c{i}", "name": "read_status", "arguments": json.dumps({"n": i})}
        for i in range(MAX_TOOL_CALLS_PER_ROUND + 3)
    ]
    client = _ScriptedClient([requested, [{"type": "content", "text": "ok"}]])

    events = await _run(_orchestrator(client, registry))

    assert len(registry.invocations) == MAX_TOOL_CALLS_PER_ROUND
    second = client.seen[1]["messages"]
    assistant = next(message for message in second if message.get("tool_calls"))
    assert len(assistant["tool_calls"]) == MAX_TOOL_CALLS_PER_ROUND + 3
    deferred = [
        json.loads(message["content"])
        for message in second
        if message.get("role") == "tool"
        and message["tool_call_id"] in {f"c{i}" for i in range(MAX_TOOL_CALLS_PER_ROUND, MAX_TOOL_CALLS_PER_ROUND + 3)}
    ]
    assert deferred == [DEFERRED_CALL_RESULT] * 3
    done = next(event for event in events if event.get("type") == "done")
    assert done["metrics"]["deferred_calls"] == 3
