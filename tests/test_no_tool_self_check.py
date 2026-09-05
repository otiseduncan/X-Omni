from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.models.client import ModelClient
from core.orchestrator.loop import (
    NO_TOOL_SELF_CHECK_ACCEPT,
    NO_TOOL_SELF_CHECK_FALLBACK,
    NO_TOOL_SELF_CHECK_MESSAGE,
    NO_TOOL_SELF_CHECK_REQUIRED_MESSAGE,
    Orchestrator,
    model_owned_no_tool_self_check,
)
from core.state.db import Store
from core.tools.registry import Registry


def test_production_model_client_enables_bounded_no_tool_self_check() -> None:
    assert ModelClient.supports_no_tool_self_check is True


class _Router:
    active_name = "omni"

    @staticmethod
    def active_config():
        return SimpleNamespace(supports_vision=True, supports_audio=True)


def _orchestrator(client: Any, registry: Registry, store: Store) -> Orchestrator:
    return Orchestrator(
        _Router(),
        client,
        registry,
        store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )


@pytest.mark.asyncio
async def test_model_owned_self_check_accepts_only_exact_protocol_marker() -> None:
    observed: dict[str, Any] = {}

    class Client:
        async def stream(self, messages, tools=None):
            observed["messages"] = messages
            observed["tools"] = tools
            yield {"type": "content", "text": f"  {NO_TOOL_SELF_CHECK_ACCEPT}\n"}

    tools = [{"type": "function", "function": {"name": "read", "parameters": {}}}]
    result = await model_owned_no_tool_self_check(
        Client(),
        [{"role": "user", "content": "Explain torque."}],
        tools,
        "Torque is rotational force.",
    )

    assert result.accept_draft is True
    assert result.tool_calls == ()
    assert observed["tools"] is tools
    assert observed["messages"][-2] == {
        "role": "assistant", "content": "Torque is rotational force.",
    }
    assert observed["messages"][-1]["content"] == NO_TOOL_SELF_CHECK_MESSAGE


@pytest.mark.asyncio
async def test_first_round_unsupported_draft_is_replaced_by_model_selected_tool(
    tmp_path,
) -> None:
    store = Store(tmp_path / "self-check-tool.sqlite")
    conversation_id = store.create_conversation("current RO")
    user_message_id = store.add_message(
        conversation_id, "user", "Tell me the current saved RO details.",
    )
    store.set_conversation_subject(
        conversation_id,
        {
            "type": "calibration_iq_repair_order",
            "resource_id": "ro-1",
            "repair_order_id": "ro-1",
            "ro_number": "2400911667",
            "current_calibration_detail_included": False,
        },
        source_tool_name="calibration_iq_ro",
        source_tool_call_id="prior-exact-ro",
        source_message_id=user_message_id,
    )
    registry = Registry("config/tools.yaml", store=store, profile="adas_operator")
    tool_calls: list[dict[str, Any]] = []

    async def exact_ro(args: dict[str, Any]) -> dict[str, Any]:
        tool_calls.append(args)
        return {
            "status": "verified",
            "repair_order": {"id": "ro-1", "RO": "2400911667", "version": 7},
            "raw": {
                "repair_order": {
                    "id": "ro-1", "ro_number": "2400911667", "version": 7,
                },
            },
        }

    registry.register("calibration_iq_ro", exact_ro)

    class Client:
        supports_no_tool_self_check = True

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, messages, tools=None, *, tool_choice=None):
            self.calls += 1
            if self.calls == 1:
                assert tool_choice is None
                yield {
                    "type": "content",
                    "text": "Unsupported draft: the RO is already complete.",
                }
                return
            if self.calls == 2:
                assert tool_choice == "required"
                assert messages[-1]["content"] == NO_TOOL_SELF_CHECK_REQUIRED_MESSAGE
                assert any(
                    item["function"]["name"] == "calibration_iq_ro"
                    for item in tools
                )
                assert not any(
                    item["function"]["name"] in {
                        "calibration_iq_operator",
                        "calibration_iq_destructive",
                    }
                    for item in tools
                )
                yield {
                    "type": "tool_call",
                    "id": "self-check-exact-ro",
                    "name": "calibration_iq_ro",
                    "arguments": json.dumps({"repair_order_id": "ro-1"}),
                }
                return
            encoded = json.dumps(messages)
            assert tool_choice is None
            assert "Unsupported draft" not in encoded
            assert NO_TOOL_SELF_CHECK_MESSAGE not in encoded
            assert any(message.get("role") == "tool" for message in messages)
            yield {
                "type": "content",
                "text": "The verified current RO is 2400911667 at version 7.",
            }

    client = Client()
    events = [event async for event in _orchestrator(
        client, registry, store,
    ).run_turn(
        conversation_id,
        "Tell me the current saved RO details.",
        approval_context={
            "session_id": "local:local-dev",
            "user_id": "local-dev",
            "role": "owner",
            "message_id": user_message_id,
        },
    )]

    final_text = "".join(
        event["text"] for event in events if event.get("type") == "token"
    )
    assert client.calls == 3
    assert tool_calls == [{"repair_order_id": "ro-1"}]
    assert "Unsupported draft" not in final_text
    assert final_text == "The verified current RO is 2400911667 at version 7."
    assert store.get_messages(conversation_id)[-1]["content"] == final_text
    store.close()


@pytest.mark.asyncio
async def test_casual_no_tool_draft_passes_after_one_bounded_review(tmp_path) -> None:
    store = Store(tmp_path / "self-check-casual.sqlite")
    conversation_id = store.create_conversation("casual")
    user_message_id = store.add_message(conversation_id, "user", "What is torque?")
    registry = Registry("config/tools.yaml", store=store, profile="adas_operator")

    class Client:
        supports_no_tool_self_check = True

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, _messages, tools=None, *, tool_choice=None):
            self.calls += 1
            assert tool_choice is None
            if self.calls == 1:
                yield {"type": "content", "text": "Torque is rotational force."}
            else:
                yield {"type": "content", "text": NO_TOOL_SELF_CHECK_ACCEPT}

    client = Client()
    events = [event async for event in _orchestrator(
        client, registry, store,
    ).run_turn(
        conversation_id,
        "What is torque?",
        approval_context={
            "session_id": "local:local-dev",
            "user_id": "local-dev",
            "role": "owner",
            "message_id": user_message_id,
        },
    )]

    assert client.calls == 2
    assert "".join(
        event["text"] for event in events if event.get("type") == "token"
    ) == "Torque is rotational force."
    assert store.get_messages(conversation_id)[-1]["content"] == (
        "Torque is rotational force."
    )
    store.close()


@pytest.mark.asyncio
async def test_active_context_required_review_cannot_sentinel_or_auto_select_mutation(
    tmp_path,
) -> None:
    store = Store(tmp_path / "self-check-required.sqlite")
    conversation_id = store.create_conversation("active RO")
    user_message_id = store.add_message(
        conversation_id,
        "user",
        "Give me the answer for this active work item.",
    )
    store.set_conversation_subject(
        conversation_id,
        {
            "type": "calibration_iq_repair_order",
            "resource_id": "ro-1",
            "repair_order_id": "ro-1",
        },
        source_tool_name="calibration_iq_ro",
        source_tool_call_id="prior-read",
        source_message_id=user_message_id,
    )
    registry = Registry("config/tools.yaml", store=store, profile="adas_operator")
    mutation_calls: list[dict[str, Any]] = []

    async def mutation(args: dict[str, Any]) -> dict[str, Any]:
        mutation_calls.append(args)
        return {"status": "verified"}

    registry.register("calibration_iq_operator", mutation)

    class Client:
        supports_no_tool_self_check = True
        calls = 0

        async def stream(self, messages, tools=None, *, tool_choice=None):
            self.calls += 1
            if self.calls == 1:
                assert tool_choice is None
                yield {"type": "content", "text": "Unsupported active-state claim."}
                return
            assert tool_choice == "required"
            assert messages[-1]["content"] == NO_TOOL_SELF_CHECK_REQUIRED_MESSAGE
            assert not any(
                item["function"]["name"] in {
                    "calibration_iq_operator",
                    "calibration_iq_destructive",
                }
                for item in tools
            )
            # Even if a model/server violates required tool choice, Core fails
            # closed. It never fabricates a read or mutation on the model's behalf.
            yield {"type": "content", "text": NO_TOOL_SELF_CHECK_ACCEPT}

    events = [
        event
        async for event in _orchestrator(Client(), registry, store).run_turn(
            conversation_id,
            "Give me the answer for this active work item.",
            approval_context={
                "session_id": "local:local-dev",
                "user_id": "local-dev",
                "role": "owner",
                "message_id": user_message_id,
            },
        )
    ]

    final_text = "".join(
        event["text"] for event in events if event.get("type") == "token"
    )
    assert final_text == NO_TOOL_SELF_CHECK_FALLBACK
    assert mutation_calls == []
    assert not any(event.get("type") == "tool_start" for event in events)
    store.close()


@pytest.mark.asyncio
async def test_malformed_no_tool_review_fails_closed_instead_of_leaking_draft(
    tmp_path,
) -> None:
    store = Store(tmp_path / "self-check-malformed.sqlite")
    conversation_id = store.create_conversation("malformed review")
    user_message_id = store.add_message(conversation_id, "user", "Current answer?")
    registry = Registry("config/tools.yaml", store=store, profile="adas_operator")

    class Client:
        supports_no_tool_self_check = True
        calls = 0

        async def stream(self, _messages, tools=None):
            self.calls += 1
            yield {
                "type": "content",
                "text": (
                    "Unsupported current-state claim."
                    if self.calls == 1
                    else "It is probably fine."
                ),
            }

    events = [event async for event in _orchestrator(
        Client(), registry, store,
    ).run_turn(
        conversation_id,
        "Current answer?",
        approval_context={
            "session_id": "local:local-dev",
            "user_id": "local-dev",
            "role": "owner",
            "message_id": user_message_id,
        },
    )]

    final_text = "".join(
        event["text"] for event in events if event.get("type") == "token"
    )
    assert final_text == NO_TOOL_SELF_CHECK_FALLBACK
    assert "Unsupported current-state claim" not in final_text
    store.close()


def test_no_tool_review_forbids_accepting_a_draft_that_reports_work_as_done() -> None:
    """A zero-tool turn cannot have performed anything.

    Seen live on RO 2400911779: with no active subject the review was not
    forced to pick a tool, and the model accepted its own draft claiming the
    ADAS Map had been acquired and attached, inventing three calibration
    requirements. ScrapeX had no batch, no PDF was written, and Calibration IQ
    still read research_required with zero documents. The review instruction
    has to state that a draft reporting completed work is unsupported when
    nothing executed, so accepting it is never the right call.
    """
    message = NO_TOOL_SELF_CHECK_MESSAGE.casefold()

    assert "nothing has executed in this turn" in message
    # The claim classes that made the fabricated answer read as authoritative.
    for claim in ("acquired", "attached", "reconciled", "complete"):
        assert claim in message
    # Findings credited to work that never ran are unsupported too, not just
    # the completion sentence itself.
    assert "unsupported" in message
    # It must send the model to the tool rather than to the accept marker.
    accept_index = message.index(NO_TOOL_SELF_CHECK_ACCEPT.casefold())
    executed_index = message.index("nothing has executed in this turn")
    assert executed_index < accept_index

    # The forced-review variant already refuses the draft outright, so it must
    # keep saying so rather than deferring to the model's own judgement.
    required = NO_TOOL_SELF_CHECK_REQUIRED_MESSAGE.casefold()
    assert "do not accept or repeat the withheld draft" in required
