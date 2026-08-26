from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.orchestrator import loop as loop_mod
from core.orchestrator.loop import Orchestrator


RO_NUMBER = "XOP-20260821211550-c28d41ae"


class _Store:
    def __init__(self) -> None:
        self.history = [{"id": 345, "role": "user", "content": "research the RO"}]
        self.saved: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def get_messages(self, _conversation_id: int):
        return list(self.history)

    def add_message(self, *args: Any, **kwargs: Any) -> int:
        self.saved.append((args, kwargs))
        return 346

    def touch_conversation(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Registry:
    def __init__(self) -> None:
        self.invocations: list[tuple[str, dict[str, Any]]] = []

    def model_tools(self, _role: str = "owner"):
        return [{
            "type": "function",
            "function": {
                "name": "calibration_iq_operator",
                "description": "Execute structured Calibration IQ actions.",
                "parameters": {"type": "object"},
            },
        }]

    @staticmethod
    def tier(_name: str) -> str:
        return "operator_authorized"

    async def invoke(self, name: str, args: dict[str, Any], **_context: Any):
        self.invocations.append((name, dict(args)))
        return {
            "status": "success",
            "executed": True,
            "success": True,
            "verified": True,
            "partial": False,
            "requested_count": 1,
            "processed_count": 1,
            "receipts": [{
                "operation": "research_ro",
                "status": "completed",
                "success": True,
                "verification": {"verified": True},
            }],
            "final_snapshots": {RO_NUMBER: {"status": "verified"}},
        }


class _Model:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, _messages, tools=None):
        assert tools
        self.calls += 1
        if self.calls == 1:
            yield {
                "type": "tool_call",
                "id": "model-research-ro",
                "name": "calibration_iq_operator",
                "arguments": (
                    '{"actions":[{"operation":"research_ro",'
                    f'"repair_order_id":"{RO_NUMBER}",'
                    '"arguments":{"complete_research":true}}]}'
                ),
            }
            return
        yield {"type": "content", "text": "The verified research is complete."}


class _Router:
    active_name = "omni"

    @staticmethod
    def active_config():
        return SimpleNamespace(supports_vision=True, supports_audio=True)


@pytest.mark.asyncio
async def test_persisted_ro_research_is_a_model_selected_structured_call(monkeypatch):
    store = _Store()
    registry = _Registry()
    client = _Model()
    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])
    orchestrator = Orchestrator(
        _Router(),
        client,
        registry,
        store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )

    events = [
        event
        async for event in orchestrator.run_turn(
            61,
            (
                f"For Calibration IQ RO {RO_NUMBER}, re-run and persist the OEM "
                "research, then mark the research complete."
            ),
            approval_context={
                "user_id": "local-dev",
                "role": "owner",
                "message_id": 345,
            },
        )
    ]

    assert client.calls == 2
    assert registry.invocations == [(
        "calibration_iq_operator",
        {
            "actions": [{
                "operation": "research_ro",
                "repair_order_id": RO_NUMBER,
                "arguments": {"complete_research": True},
            }]
        },
    )]
    assert [event["name"] for event in events if event["type"] == "tool_start"] == [
        "calibration_iq_operator"
    ]
