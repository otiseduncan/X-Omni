from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.orchestrator.loop import (
    Orchestrator,
    calibration_iq_read_request,
    calibration_iq_result_summary,
    latest_calibration_iq_filters,
)
from core.state.db import Store
from core.tools.registry import Registry


class _Router:
    active_name = "omni"

    @staticmethod
    def active_config():
        return SimpleNamespace(supports_vision=True, supports_audio=True)


class _NoModel:
    def __init__(self):
        self.stream_calls = 0

    async def stream(self, _messages, tools=None):
        self.stream_calls += 1
        raise AssertionError("Explicit Calibration IQ reads must use the deterministic lane")
        yield  # pragma: no cover


class _CalibrationHandlers:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def _filters(args: dict) -> dict:
        return {
            key: value
            for key, value in args.items()
            if key in {"shop", "phase", "status", "insurance", "q"}
        }

    async def summary(self, args: dict) -> dict:
        self.calls.append(("calibration_iq_summary", dict(args)))
        filters = self._filters(args)
        count = 6 if filters.get("phase") == "5" else 15
        return {
            "status": "verified",
            "count": count,
            "active_count": count,
            "completed_count": 2,
            "include_completed": bool(args.get("include_completed")),
            "terminal_only": bool(args.get("terminal_only")),
            "scope": "active work only",
            "filters": filters,
            "breakdown": {
                "by_status": {"New Arrival": count},
                "by_phase": {filters.get("phase", "unspecified"): count},
                "by_shop": {filters.get("shop", "Macon"): count},
            },
            "collection_complete": True,
        }

    async def listing(self, args: dict) -> dict:
        self.calls.append(("calibration_iq_read", dict(args)))
        filters = self._filters(args)
        return {
            "status": "verified",
            "count": 6,
            "active_count": 6,
            "completed_count": 2,
            "include_completed": bool(args.get("include_completed")),
            "terminal_only": bool(args.get("terminal_only")),
            "scope": "active work only",
            "filters": filters,
            "breakdown": {
                "by_status": {"New Arrival": 6},
                "by_phase": {filters.get("phase", "unspecified"): 6},
                "by_shop": {filters.get("shop", "Macon"): 6},
            },
            "rows": [{"id": str(i), "RO": f"RO{i}"} for i in range(6)],
            "shown_count": 6,
            "truncated": False,
            "collection_complete": True,
        }


def _orchestrator(store: Store, handlers: _CalibrationHandlers, client: _NoModel):
    registry = Registry("config/tools.yaml", store=store)
    registry.register("calibration_iq_summary", handlers.summary)
    registry.register("calibration_iq_read", handlers.listing)
    return Orchestrator(
        _Router(),
        client,
        registry,
        store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )


async def _run_user_turn(
    store: Store,
    conversation_id: int,
    orchestrator: Orchestrator,
    text: str,
) -> list[dict]:
    store.add_message(conversation_id, "user", text)
    return [event async for event in orchestrator.run_turn(conversation_id, text)]


def test_latest_scope_ignores_failed_artifacts_and_other_conversations(tmp_path):
    failed_history = [{
        "artifacts": [{
            "type": "calibration_iq_summary",
            "data": {"status": "offline", "filters": {"shop": "Perry"}},
        }],
    }]
    assert latest_calibration_iq_filters(failed_history) == {}
    assert calibration_iq_read_request("Show me those.", failed_history) is None

    successful_history = [{
        "artifacts": [{
            "type": "calibration_iq_summary",
            "data": {
                "status": "verified",
                "filters": {"shop": "Macon", "phase": "5", "offset": 40},
                "include_completed": False,
            },
        }],
    }]
    assert latest_calibration_iq_filters(successful_history) == {
        "shop": "Macon",
        "phase": "5",
        "include_completed": False,
    }
    assert calibration_iq_read_request(
        "Show me those in Perry.", successful_history
    ) == (
        "calibration_iq_read",
        {"shop": "Perry", "phase": "5", "include_completed": False},
    )


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        (
            "How many completed vehicles are in Macon?",
            {
                "shop": "Macon",
                "status": "Calibration Complete",
                "include_completed": True,
                "terminal_only": True,
            },
        ),
        (
            "How many No Calibration Required vehicles are in Macon?",
            {
                "shop": "Macon",
                "status": "No Calibration Required",
                "include_completed": True,
                "terminal_only": True,
            },
        ),
        (
            "How many finished vehicles are in Macon?",
            {
                "shop": "Macon",
                "include_completed": True,
                "terminal_only": True,
            },
        ),
        (
            "How many closed repair orders are in Macon?",
            {
                "shop": "Macon",
                "include_completed": True,
                "terminal_only": True,
            },
        ),
        (
            "How many vehicles are in all work in Macon?",
            {
                "shop": "Macon",
                "include_completed": True,
                "terminal_only": False,
            },
        ),
    ],
)
def test_explicit_terminal_and_all_work_scopes(utterance, expected):
    assert calibration_iq_read_request(utterance, []) == (
        "calibration_iq_summary",
        expected,
    )


def test_terminal_scope_is_inherited_and_explicit_active_clears_category():
    history = [{
        "artifacts": [{
            "type": "calibration_iq_summary",
            "data": {
                "status": "verified",
                "filters": {"shop": "Macon", "status": "Calibration Complete"},
                "include_completed": True,
                "terminal_only": True,
            },
        }],
    }]
    assert calibration_iq_read_request("Show me those.", history) == (
        "calibration_iq_read",
        {
            "shop": "Macon",
            "status": "Calibration Complete",
            "include_completed": True,
            "terminal_only": True,
        },
    )
    assert calibration_iq_read_request("Show me those active vehicles.", history) == (
        "calibration_iq_read",
        {"shop": "Macon", "include_completed": False, "terminal_only": False},
    )


def test_fixed_list_summary_preserves_visible_vs_total_without_rows():
    text = calibration_iq_result_summary(
        {
            "status": "verified",
            "count": 59,
            "shown_count": 20,
            "truncated": True,
            "include_completed": False,
            "filters": {"shop": "Macon"},
            "rows": [{"RO": "must-not-be-spoken"}],
        },
        listing=True,
    )
    assert text == "Showing 20 of 59 active repair orders in Macon."
    assert "must-not-be-spoken" not in text


@pytest.mark.asyncio
async def test_three_turn_scope_is_durable_and_each_turn_has_one_card(tmp_path):
    store = Store(tmp_path / "ciq-routing.sqlite")
    conversation_id = store.create_conversation("Calibration IQ")
    handlers = _CalibrationHandlers()
    client = _NoModel()

    first = await _run_user_turn(
        store,
        conversation_id,
        _orchestrator(store, handlers, client),
        "How many cars are active in Macon?",
    )
    second = await _run_user_turn(
        store,
        conversation_id,
        _orchestrator(store, handlers, client),
        "How many are in Macon phase 5?",
    )

    # Build a fresh orchestrator/registry for the follow-up. Its only scope
    # source is the durable same-conversation artifact in SQLite.
    third_orchestrator = _orchestrator(store, handlers, client)
    store.add_message(conversation_id, "user", "Show me those.")
    stream = third_orchestrator.run_turn(conversation_id, "Show me those.")
    first_third_event = await anext(stream)
    latest = store.get_messages(conversation_id)[-1]
    assert latest["role"] == "assistant"
    assert len(latest["artifacts"]) == 1
    third = [first_third_event, *[event async for event in stream]]

    assert handlers.calls == [
        (
            "calibration_iq_summary",
            {
                "shop": "Macon",
                "include_completed": False,
                "terminal_only": False,
            },
        ),
        ("calibration_iq_summary", {"shop": "Macon", "phase": "5"}),
        (
            "calibration_iq_read",
            {"shop": "Macon", "phase": "5", "include_completed": False},
        ),
    ]
    for events, expected_type in (
        (first, "calibration_iq_summary"),
        (second, "calibration_iq_summary"),
        (third, "calibration_iq_ros"),
    ):
        assert [event["type"] for event in events] == [
            "tool_start",
            "tool_result",
            "artifact",
            "token",
            "done",
        ]
        artifacts = [event for event in events if event["type"] == "artifact"]
        assert len(artifacts) == 1
        assert artifacts[0]["artifact"]["type"] == expected_type
        assert len(events[-1]["artifacts"]) == 1

    assert third[0]["args"] == {
        "shop": "Macon",
        "phase": "5",
        "include_completed": False,
    }
    assert third[-2]["text"] == "Showing all 6 active repair orders in Macon phase 5."
    assert "RO0" not in third[-2]["text"]
    assert client.stream_calls == 0

    persisted = store.get_messages(conversation_id)
    assistant_cards = [
        message["artifacts"]
        for message in persisted
        if message["role"] == "assistant"
    ]
    assert [len(cards) for cards in assistant_cards] == [1, 1, 1]


def test_context_is_never_inherited_across_conversations(tmp_path):
    store = Store(tmp_path / "ciq-conversations.sqlite")
    source_conversation = store.create_conversation("Source")
    other_conversation = store.create_conversation("Other")
    store.add_message(
        source_conversation,
        "assistant",
        "Six active.",
        artifacts=[{
            "type": "calibration_iq_summary",
            "data": {
                "status": "verified",
                "filters": {"shop": "Macon", "phase": "5"},
                "include_completed": False,
            },
        }],
    )
    other_history = store.get_messages(other_conversation)
    assert calibration_iq_read_request("Show me those.", other_history) is None
