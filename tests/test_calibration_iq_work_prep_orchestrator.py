from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.orchestrator.loop import Orchestrator
from core.services import calibration_iq_work_prep as prep
from core.state.db import Store
from core.tools.registry import Registry


class _Store:
    def __init__(self, text: str) -> None:
        self.history = [{"id": 345, "role": "user", "content": text}]
        self.saved = None

    def get_messages(self, _conversation_id: int):
        return self.history

    def add_message(self, *args, **kwargs):
        self.saved = (args, kwargs)
        return 346

    def touch_conversation(self, *_args, **_kwargs):
        return None


class _ModelClient:
    def __init__(
        self,
        args: dict[str, Any],
        final_text: str = "The work-prep audit found one exception.",
    ) -> None:
        self.args = args
        self.final_text = final_text
        self.calls = 0

    async def stream(self, _messages, tools=None):
        assert tools
        self.calls += 1
        if self.calls == 1:
            yield {
                "type": "tool_call",
                "id": "model-work-prep",
                "name": prep.TOOL_NAME,
                "arguments": json.dumps(self.args),
            }
            return
        yield {"type": "content", "text": self.final_text}


class _Registry:
    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.invocations: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.result = result
        self.last_result: dict[str, Any] | None = None

    def model_tools(self, _role="owner"):
        return [{
            "type": "function",
            "function": {
                "name": prep.TOOL_NAME,
                "description": "Run structured Calibration IQ work preparation.",
                "parameters": {"type": "object"},
            },
        }]

    @staticmethod
    def tier(_name):
        return "operator_authorized"

    async def invoke(self, name, args, **kwargs):
        self.invocations.append((name, args, kwargs))
        self.last_result = self.result or {
            "status": "partial_success",
            "mode": args["mode"],
            "coverage_focus": args.get("coverage_focus"),
            "success": True,
            "verified": True,
            "readiness_complete": False,
            "exception_count": 1,
            "queue_count": 2,
            "ready_count": 1,
            "adas_map_verified_count": 1,
            "adas_map_missing_count": 0,
            "adas_map_unverified_count": 1,
            "adas_map_unavailable_count": 1,
            "si_covered_count": 1,
            "si_missing_count": 0,
            "si_unverified_count": 1,
            "reconciliation_failed_count": 0,
            "ciq_requirements_added_or_reactivated": 0,
            "alldata_queued_count": 0,
            "phase_scope": [args.get("phase")]
            if args.get("phase")
            else ["5", "6", "7", "8"],
            "repair_orders": [
                {"ro_number": "100", "vehicle": "Ready Vehicle", "ready": True},
                {
                    "ro_number": "200",
                    "vehicle": "Attention Vehicle",
                    "ready": False,
                    "status": "adas_map_unverified",
                    "adas_map": {"status": "unverified"},
                },
            ],
        }
        return self.last_result


class _Router:
    active_name = "omni"

    @staticmethod
    def active_config():
        return SimpleNamespace(supports_vision=True, supports_audio=True)


@pytest.mark.parametrize(
    ("utterance", "expected_mode", "expected_phase", "execute_missing"),
    [
        ("let's prepare for the weak", "week_readiness", None, True),
        (
            "do all cars in phase 5 have an ADAS Map report in ADAS SI?",
            "phase_coverage",
            "5",
            False,
        ),
    ],
)
@pytest.mark.asyncio
async def test_work_prep_turn_uses_model_selected_structured_mode(
    utterance: str,
    expected_mode: str,
    expected_phase: str | None,
    execute_missing: bool,
):
    prep.install()
    store = _Store(utterance)
    registry = _Registry()
    expected_args = {"mode": expected_mode}
    if execute_missing:
        expected_args["execute_missing"] = True
    if expected_phase:
        expected_args.update({"phase": expected_phase, "coverage_focus": "adas_map"})
    client = _ModelClient(expected_args)
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
            utterance,
            approval_context={
                "session_id": "local:owner",
                "user_id": "owner",
                "role": "owner",
                "message_id": 345,
            },
        )
    ]

    assert client.calls == 2
    assert [name for name, _args, _kwargs in registry.invocations] == [prep.TOOL_NAME]
    args = registry.invocations[0][1]
    assert args["mode"] == expected_mode
    assert args.get("phase") == expected_phase
    assert args.get("execute_missing") is (True if execute_missing else None)
    assert args.get("coverage_focus") == (
        "adas_map" if expected_mode == "phase_coverage" else None
    )
    text = "".join(event["text"] for event in events if event.get("type") == "token")
    assert registry.last_result is not None
    assert text == prep.summarize(expected_mode, registry.last_result)
    assert "The work-prep audit found one exception." not in text
    assert store.saved is not None and store.saved[0][2] == text


@pytest.mark.asyncio
async def test_work_prep_terminal_summary_seals_counts_exceptions_and_receipt_truth():
    result = {
        "status": "partial_success",
        "mode": "week_readiness",
        "executed": True,
        "success": True,
        "verified": True,
        "readiness_complete": False,
        "exception_count": 5,
        "queue_count": 8,
        "ready_count": 3,
        "adas_map_verified_count": 8,
        "adas_map_missing_count": 0,
        "adas_map_unverified_count": 0,
        "adas_map_unavailable_count": 0,
        "si_covered_count": 3,
        "si_missing_count": 5,
        "si_unverified_count": 0,
        "reconciliation_failed_count": 1,
        "ciq_mutations_requested_count": 2,
        "ciq_mutations_processed_count": 1,
        "ciq_receipt_count": 1,
        "ciq_verified_receipt_count": 1,
        "ciq_indeterminate_reconciliation_count": 1,
        "ciq_may_have_executed_reconciliation_count": 1,
        "ciq_requirements_added_or_reactivated": 1,
        "alldata_queued_count": 5,
        "phase_scope": ["5", "6", "7", "8"],
        "repair_orders_truncated": True,
        "repair_orders": [
            {
                "ro_number": str(ro_number),
                "vehicle": f"Exception Vehicle {ro_number}",
                "ready": False,
                "adas_map": {"status": "verified"},
                "missing_si": [{"calibration": "Front camera calibration"}],
            }
            for ro_number in (401, 402, 403)
        ],
    }
    registry = _Registry(result)
    store = _Store("prepare us for the week")
    model_lie = "All eight vehicles are ready and both CIQ changes completed."
    orchestrator = Orchestrator(
        _Router(),
        _ModelClient({"mode": "week_readiness"}, model_lie),
        registry,
        store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )

    events = [
        event
        async for event in orchestrator.run_turn(
            61,
            "prepare us for the week",
            approval_context={"role": "owner", "message_id": 345},
        )
    ]
    text = "".join(event["text"] for event in events if event.get("type") == "token")

    assert model_lie not in text
    assert "No — 5 of 8" in text
    assert all(f"RO {ro_number}" in text for ro_number in (401, 402, 403))
    assert "2 additional RO exception(s)" in text
    assert "1 verified of 2 requested; 1 processed; 1 returned; 1 unverified" in text
    assert "Indeterminate reconciliation outcomes: 1" in text
    assert "may-have-executed outcomes: 1" in text
    assert store.saved is not None and store.saved[0][2] == text


@pytest.mark.asyncio
async def test_real_registry_binds_work_prep_context_and_logs_partial_receipts_failed(
    tmp_path,
):
    store = Store(tmp_path / "work-prep-context.sqlite")
    registry = Registry("config/tools.yaml", store=store)
    captured: dict[str, Any] = {}

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        captured.update(args)
        return {
            "status": "partial_success",
            "mode": "week_readiness",
            "executed": True,
            "success": True,
            "verified": True,
            "reconciliation_failed_count": 1,
            "ciq_mutations_requested_count": 1,
            "ciq_mutations_processed_count": 0,
            "ciq_receipt_count": 0,
            "ciq_verified_receipt_count": 0,
            "ciq_indeterminate_reconciliation_count": 1,
            "ciq_may_have_executed_reconciliation_count": 1,
        }

    registry.register(prep.TOOL_NAME, handler)
    conversation_id = store.create_conversation("work prep context")
    message_id = store.add_message(conversation_id, "user", "prepare the week")
    spoof = {
        "conversation_id": 999,
        "message_id": 998,
        "tool_call_id": "spoofed-call",
        "user_id": "attacker",
        "role": "owner",
    }

    await registry.invoke(
        prep.TOOL_NAME,
        {"mode": "week_readiness", prep._CONTEXT_KEY: spoof},  # noqa: SLF001
        message_id=message_id,
        conversation_id=conversation_id,
        tool_call_id="real-work-prep-call",
        user_id="local-dev",
        role="owner",
    )

    context = captured[prep._CONTEXT_KEY]  # noqa: SLF001
    assert context["conversation_id"] == conversation_id
    assert context["message_id"] == message_id
    assert context["tool_call_id"] == "real-work-prep-call"
    assert context["user_id"] == "local-dev"
    row = store.conn.execute(
        "SELECT status, approved_by, args_json FROM tool_calls WHERE tool_call_id = ?",
        ("real-work-prep-call",),
    ).fetchone()
    assert row["status"] == "failed"
    assert row["approved_by"] == "operator_authorized"
    assert prep._CONTEXT_KEY not in json.loads(row["args_json"])  # noqa: SLF001
    store.close()
