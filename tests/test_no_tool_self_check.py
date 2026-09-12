from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.models.client import ModelClient
from core.orchestrator.loop import (
    ADAS_SI_POST_TOOL_SELF_CHECK_MESSAGE,
    NO_TOOL_SELF_CHECK_ACCEPT,
    NO_TOOL_SELF_CHECK_FALLBACK,
    NO_TOOL_SELF_CHECK_MESSAGE,
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
    for meta_name in ("query_ciq", "delegate_research", "stage_action", "capability_search"):
        registry.register(meta_name, lambda _args: {})

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
                # The review is model-owned and never forced, even with an
                # active subject: tool_choice stays automatic and the model
                # itself decides fresh evidence is needed.
                assert tool_choice is None
                assert messages[-1]["content"] == NO_TOOL_SELF_CHECK_MESSAGE
                advertised = {item["function"]["name"] for item in tools}
                assert "query_ciq" in advertised
                assert advertised.isdisjoint({
                    "calibration_iq_ro",
                    "calibration_iq_operator",
                    "calibration_iq_destructive",
                })
                yield {
                    "type": "tool_call",
                    "id": "self-check-exact-ro",
                    "name": "query_ciq",
                    "arguments": json.dumps({"kind": "ro", "repair_order_id": "ro-1"}),
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
async def test_search_result_cannot_be_recast_as_recent_inventory(tmp_path) -> None:
    store = Store(tmp_path / "self-check-adas-si.sqlite")
    conversation_id = store.create_conversation("recent ADAS SI")
    user_message_id = store.add_message(
        conversation_id, "user", "What documents were added this morning?"
    )
    registry = Registry("config/tools.yaml", store=store, profile="adas_operator")
    calls: list[str] = []

    def search(_args):
        calls.append("adas_si_search")
        return {
            "status": "success",
            "results": [{"title": "2017 Toyota Camry Radar"}],
            "evidence_contract": {"proves_document_arrival_time": False},
        }

    def inventory(_args):
        calls.append("adas_si_inventory")
        return {
            "status": "success",
            "recent_additions": {
                "count": 20,
                "documents": [{"title": "Honda Acura ADAS Calibration Requirements"}],
            },
            "storage_refresh": {"moved_count": 20},
        }

    registry.register("adas_si_search", search)
    registry.register("adas_si_inventory", inventory)
    for name in ("delegate_research", "capability_search"):
        registry.register(name, lambda _args: {})

    class Client:
        supports_no_tool_self_check = True
        calls = 0

        async def stream(self, messages, tools=None, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_call",
                    "id": "wrong-search",
                    "name": "adas_si_search",
                    "arguments": json.dumps({"question": "new documents"}),
                }
            elif self.calls == 2:
                yield {
                    "type": "content",
                    "text": "The 2017 Toyota Camry Radar document was added this morning.",
                }
            elif self.calls == 3:
                assert messages[-1]["content"] == ADAS_SI_POST_TOOL_SELF_CHECK_MESSAGE
                yield {
                    "type": "tool_call",
                    "id": "correct-inventory",
                    "name": "adas_si_inventory",
                    "arguments": json.dumps(
                        {
                            "added_since": "2026-09-12T00:00:00-04:00",
                            "added_before": "2026-09-12T12:00:00-04:00",
                        }
                    ),
                }
            else:
                yield {
                    "type": "content",
                    "text": "The inventory verifies 20 additions this morning.",
                }

    events = [
        event
        async for event in _orchestrator(Client(), registry, store).run_turn(
            conversation_id,
            "What documents were added this morning?",
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
    assert calls == ["adas_si_search", "adas_si_inventory"]
    assert final_text == "The inventory verifies 20 additions this morning."
    assert "Camry Radar" not in final_text
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
async def test_active_subject_never_forces_a_tool_and_a_casual_draft_survives(
    tmp_path,
) -> None:
    """The user's core case: an RO was looked up earlier, then a general
    question ("How does an ultrasonic parking sensor calculate distance?")
    is asked. The persisted subject is memory, not a gate: the review runs
    unforced, the model keeps its answer, and no tool runs."""

    store = Store(tmp_path / "self-check-advisory.sqlite")
    conversation_id = store.create_conversation("active RO then general question")
    user_message_id = store.add_message(
        conversation_id,
        "user",
        "How does an ultrasonic parking sensor calculate distance?",
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
    for meta_name in ("query_ciq", "delegate_research", "stage_action", "capability_search"):
        registry.register(meta_name, lambda _args: {})

    draft = "It times an ultrasonic pulse's echo and halves the round trip at the speed of sound."

    class Client:
        supports_no_tool_self_check = True
        calls = 0

        async def stream(self, messages, tools=None, *, tool_choice=None):
            self.calls += 1
            assert tool_choice is None
            if self.calls == 1:
                yield {"type": "content", "text": draft}
                return
            assert messages[-1]["content"] == NO_TOOL_SELF_CHECK_MESSAGE
            # The subject rides in the turn context, but nothing tells the
            # model it must call a tool because of it.
            assert not any(
                item["function"]["name"] in {
                    "calibration_iq_operator",
                    "calibration_iq_destructive",
                }
                for item in tools
            )
            yield {"type": "content", "text": NO_TOOL_SELF_CHECK_ACCEPT}

    client = Client()
    events = [
        event
        async for event in _orchestrator(client, registry, store).run_turn(
            conversation_id,
            "How does an ultrasonic parking sensor calculate distance?",
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
    assert client.calls == 2
    assert final_text == draft
    assert mutation_calls == []
    assert not any(event.get("type") == "tool_start" for event in events)
    done = next(event for event in events if event.get("type") == "done")
    assert done["metrics"]["active_subject"] == "ro-1"
    assert done["metrics"]["tools_selected"] == []
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
    # And the review must say plainly that an active subject is memory, not a
    # reason to call a tool -- the coercion that made every casual answer
    # after one RO lookup get replaced by a forced tool pick.
    assert "active conversation subject is memory" in message
