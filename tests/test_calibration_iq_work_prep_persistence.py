"""Regression coverage: calibration_iq_work_prep results must persist.

Live field trace: "let's prepare for the week" produced a real, correctly
computed readiness audit (43 of 49 ROs not SI-ready, with per-RO detail).
The very next message, "I need a list of the ones that need SI," should
have been answerable straight from that result -- instead X invented an
unrelated shop/phase board query. Root cause: calibration_iq_work_prep had
no entry in ARTIFACT_FOR_TOOL, so its result was never captured as a stored
artifact -- nothing survived past the single turn it ran in for a later
question to draw on, regardless of how well-structured the result itself
was (and it already is: calibration_iq_work_prep.py has its own careful
byte-budgeted compaction with declared truncation).

This covers the fix at both ends: the tool now maps to a card type, and the
resulting artifact is what a later turn's stored-artifact context actually
contains.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.orchestrator import loop as loop_mod
from core.orchestrator import prompt as prompt_mod
from core.orchestrator.loop import Orchestrator, artifact_type_for_tool
from core.state.db import Store
from core.tools.registry import Registry


def _readiness_result() -> dict[str, Any]:
    return {
        "status": "partial_success",
        "mode": "week_readiness",
        "success": True,
        "verified": True,
        "readiness_complete": False,
        "exception_count": 43,
        "queue_count": 49,
        "needs_si_count": 5,
        "si_missing_count": 5,
        "repair_orders_total": 49,
        "repair_orders_shown": 49,
        "repair_orders_truncated": False,
        "repair_orders": [
            {"ro_number": "2400612490", "vehicle": "2023 Ford Maverick",
             "coverage_status": "MISSING",
             "missing_si": [{"calibration": "Passenger Seat Weight Sensor"}]},
            {"ro_number": "2400612471", "vehicle": "2026 Chevrolet Equinox",
             "coverage_status": "UNVERIFIED", "missing_si": []},
        ],
    }


class _Router:
    active_name = "omni"

    @staticmethod
    def active_config():
        return SimpleNamespace(supports_vision=True, supports_audio=True)


def test_artifact_type_for_tool_maps_work_prep_results():
    assert artifact_type_for_tool(
        "calibration_iq_work_prep", _readiness_result()
    ) == "calibration_iq_work_prep"


@pytest.mark.asyncio
async def test_readiness_result_persists_as_artifact_and_reaches_next_turns_context(
    tmp_path,
):
    store = Store(tmp_path / "ciq-work-prep.sqlite")
    conversation_id = store.create_conversation("Calibration IQ")
    registry = Registry("config/tools.yaml", store=store)

    result = _readiness_result()

    async def handler(_args: dict) -> dict:
        return result

    registry.register("calibration_iq_work_prep", handler)

    class Client:
        calls = 0

        async def stream(self, _messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_call", "id": "call-1",
                    "name": "calibration_iq_work_prep",
                    "arguments": '{"mode": "week_readiness"}',
                }
                return
            yield {"type": "content", "text": "43 of 49 not SI-ready."}

    orchestrator = Orchestrator(
        _Router(), Client(), registry, store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )
    store.add_message(conversation_id, "user", "let's prepare for the week")
    events = [
        event
        async for event in orchestrator.run_turn(
            conversation_id, "let's prepare for the week"
        )
    ]

    artifact_events = [e for e in events if e["type"] == "artifact"]
    assert any(
        e["artifact"]["type"] == "calibration_iq_work_prep" for e in artifact_events
    )

    # The persisted message must actually carry the artifact -- this is what
    # a later turn's stored-artifact context is built from.
    history = store.get_messages(conversation_id)
    stored_artifacts = [
        a for m in history for a in (m.get("artifacts") or [])
        if a.get("type") == "calibration_iq_work_prep"
    ]
    assert len(stored_artifacts) == 1
    assert stored_artifacts[0]["data"]["needs_si_count"] == 5

    context = prompt_mod._stored_artifact_context(history, 8_000)  # noqa: SLF001
    assert "calibration_iq_work_prep" in context
    assert "needs_si_count" in context
