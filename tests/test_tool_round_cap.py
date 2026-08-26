from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from core.orchestrator import loop as loop_mod
from core.orchestrator.loop import (
    FINAL_SYNTHESIS_MESSAGE,
    MAX_TOOL_ROUNDS,
    TOOL_ROUND_CAP_FALLBACK,
    Orchestrator,
)


class _Store:
    def __init__(self) -> None:
        self.saved: tuple[tuple[Any, ...], dict[str, Any]] | None = None

    def get_messages(self, _conversation_id: int) -> list[dict[str, Any]]:
        return []

    def add_message(self, *args: Any, **kwargs: Any) -> int:
        self.saved = (args, kwargs)
        return 71

    def touch_conversation(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Registry:
    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []

    def model_tools(
        self,
        _role: str = "owner",
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "bounded_read",
                    "description": "Return one bounded read result.",
                    "parameters": {
                        "type": "object",
                        "properties": {"step": {"type": "integer"}},
                        "required": ["step"],
                    },
                },
            }
        ]

    @staticmethod
    def tier(_name: str) -> str:
        return "read_only"

    async def invoke(self, _name: str, args: dict[str, Any], **_kwargs: Any) -> dict:
        self.invocations.append(dict(args))
        return {
            "status": "verified",
            "sequence": args["step"],
            "evidence_id": f"bounded-read-{args['step']}",
        }


class _Router:
    active_name = "omni"


class _SixToolRoundClient:
    def __init__(self, final_events: list[dict[str, Any]]) -> None:
        self.final_events = final_events
        self.calls = 0
        self.tools_by_call: list[list[dict] | None] = []
        self.messages_by_call: list[list[dict[str, Any]]] = []

    async def stream(self, messages: list[dict], tools: list[dict] | None = None):
        self.calls += 1
        self.tools_by_call.append(deepcopy(tools))
        self.messages_by_call.append(deepcopy(messages))
        if self.calls <= MAX_TOOL_ROUNDS:
            assert tools
            yield {
                "type": "tool_call",
                "id": f"bounded-call-{self.calls}",
                "name": "bounded_read",
                "arguments": json.dumps({"step": self.calls}),
            }
            return
        for event in self.final_events:
            yield event


def _orchestrator(client: Any, registry: _Registry, store: _Store) -> Orchestrator:
    return Orchestrator(
        _Router(),
        client,
        registry,
        store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )


async def _run(client: Any, registry: _Registry, store: _Store) -> list[dict]:
    return [
        event
        async for event in _orchestrator(client, registry, store).run_turn(
            1, "Use the bounded reads, then summarize their result."
        )
    ]


@pytest.mark.asyncio
async def test_sixth_tool_result_gets_one_synthesis_only_model_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_text = "All six bounded reads completed; the latest verified sequence is 6."
    client = _SixToolRoundClient([{"type": "content", "text": final_text}])
    registry = _Registry()
    store = _Store()
    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])

    events = await _run(client, registry, store)

    assert client.calls == MAX_TOOL_ROUNDS + 1
    assert all(client.tools_by_call[index] for index in range(MAX_TOOL_ROUNDS))
    assert client.tools_by_call[-1] == []
    final_messages = client.messages_by_call[-1]
    tool_results = [item for item in final_messages if item.get("role") == "tool"]
    assert len(tool_results) == MAX_TOOL_ROUNDS
    assert json.loads(tool_results[-1]["content"])["sequence"] == MAX_TOOL_ROUNDS
    assert final_messages[-1] == {
        "role": "user",
        "content": FINAL_SYNTHESIS_MESSAGE,
    }
    assert registry.invocations == [
        {"step": index} for index in range(1, MAX_TOOL_ROUNDS + 1)
    ]
    assert "".join(
        event["text"] for event in events if event.get("type") == "token"
    ) == final_text
    assert store.saved is not None
    assert store.saved[0][2] == final_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "final_events",
    [
        [],
        [
            {"type": "content", "text": "I will run one more check."},
            {
                "type": "tool_call",
                "id": "forbidden-seventh-call",
                "name": "bounded_read",
                "arguments": '{"step":7}',
            },
        ],
    ],
    ids=["empty-synthesis", "attempted-seventh-tool"],
)
async def test_synthesis_boundary_never_executes_a_seventh_tool(
    monkeypatch: pytest.MonkeyPatch,
    final_events: list[dict[str, Any]],
) -> None:
    client = _SixToolRoundClient(final_events)
    registry = _Registry()
    store = _Store()
    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])

    events = await _run(client, registry, store)

    assert client.calls == MAX_TOOL_ROUNDS + 1
    assert client.tools_by_call[-1] == []
    assert len(registry.invocations) == MAX_TOOL_ROUNDS
    assert [
        event["name"] for event in events if event.get("type") == "tool_start"
    ] == ["bounded_read"] * MAX_TOOL_ROUNDS
    token_text = "".join(
        event["text"] for event in events if event.get("type") == "token"
    )
    assert token_text == TOOL_ROUND_CAP_FALLBACK
    assert "one more check" not in token_text
    assert store.saved is not None
    assert store.saved[0][2] == TOOL_ROUND_CAP_FALLBACK
