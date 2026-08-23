"""Regression coverage for pre-tool-call narration leaking to the user.

Live field trace: X responded to "how many ADAS map reports do you see in
ADAS SI" with "I don't have direct access to the internal file listing of
the ADAS SI library," then in the same turn successfully called
adas_si_inventory and reported real counts from it. Root cause: the local
model can emit ordinary prose and a tool call in the same streamed round
(unlike a tightly RLHF'd hosted model), and the round loop only held back
("sealed") that prose for three named tool families -- website_preview_generate,
the Calibration IQ operator tools, and (only when deterministically
pre-routed) web_research_current. Every other tool, including every
adas_si_* tool, streamed and persisted whatever the model said before the
tool call ran, regardless of what the tool result then proved.

The fix generalizes sealing to any round that produced a tool call, for any
tool: a round's prose is provisional the moment the model itself decided it
needed to look something up in the same breath.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.orchestrator import loop as loop_mod
from core.orchestrator.loop import Orchestrator


class _Store:
    def __init__(self) -> None:
        self.saved: tuple[tuple[Any, ...], dict[str, Any]] | None = None

    def get_messages(self, _conversation_id: int) -> list[dict[str, Any]]:
        return []

    def add_message(self, *args: Any, **kwargs: Any) -> int:
        self.saved = (args, kwargs)
        return 41

    def touch_conversation(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Registry:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.invocations: list[tuple[str, dict[str, Any]]] = []

    def model_tools(self, _role: str = "owner") -> list[dict[str, Any]]:
        return []

    async def invoke(self, name: str, args: dict[str, Any], **_kwargs: Any) -> dict:
        self.invocations.append((name, dict(args)))
        return self.result


class _Router:
    active_name = "omni"


def _orchestrator(client: Any, registry: Any, store: _Store) -> Orchestrator:
    return Orchestrator(
        _Router(),
        client,
        registry,
        store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )


@pytest.mark.asyncio
async def test_pretool_capability_denial_never_reaches_user_or_persisted_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_result = {"status": "success", "summary": {"document_count": 137}}
    registry = _Registry(inventory_result)
    store = _Store()

    class Client:
        calls = 0

        async def stream(self, messages: list[dict], tools: Any = None):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "content",
                    "text": (
                        "I don't have direct access to the internal file "
                        "listing of the ADAS SI library."
                    ),
                }
                yield {
                    "type": "tool_call",
                    "id": "call_1",
                    "name": "adas_si_inventory",
                    "arguments": "{}",
                }
                return
            # By round 2 the tool result must already be in context.
            assert any(message.get("role") == "tool" for message in messages)
            yield {
                "type": "content",
                "text": "There are 137 documents in the ADAS SI inventory.",
            }

    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])
    events = [
        event
        async for event in _orchestrator(Client(), registry, store).run_turn(
            1, "How many ADAS Map reports do you see in ADAS SI?"
        )
    ]

    token_text = "".join(
        event["text"] for event in events if event.get("type") == "token"
    )
    assert "don't have direct access" not in token_text
    assert token_text == "There are 137 documents in the ADAS SI inventory."
    assert registry.invocations == [("adas_si_inventory", {})]
    assert store.saved is not None
    assert store.saved[0][2] == token_text


@pytest.mark.asyncio
async def test_final_answer_round_with_no_tool_call_is_not_sealed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generalized seal must not swallow an ordinary, tool-free reply."""
    registry = _Registry({})
    store = _Store()

    class Client:
        async def stream(self, _messages: list[dict], tools: Any = None):
            yield {"type": "content", "text": "Macon, Perry, and Warner Robins."}

    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])
    events = [
        event
        async for event in _orchestrator(Client(), registry, store).run_turn(
            1, "What shops do you track?"
        )
    ]

    token_text = "".join(
        event["text"] for event in events if event.get("type") == "token"
    )
    assert token_text == "Macon, Perry, and Warner Robins."
