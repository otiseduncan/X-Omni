from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.orchestrator.loop import Orchestrator
from core.services import calibration_iq_work_prep as prep


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


class _NoModelClient:
    calls = 0

    async def stream(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("known work-prep intent must not enter the model loop")
        yield  # pragma: no cover


class _Registry:
    def __init__(self) -> None:
        self.invocations: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def model_tools(self, _role="owner"):
        return []

    @staticmethod
    def tier(_name):
        return "operator_authorized"

    async def invoke(self, name, args, **kwargs):
        self.invocations.append((name, args, kwargs))
        return {
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


class _Router:
    active_name = "omni"


@pytest.mark.parametrize(
    ("utterance", "expected_mode", "expected_phase"),
    [
        ("let's prepare for the weak", "week_readiness", None),
        (
            "do all cars in phase 5 have an ADAS Map report in ADAS SI?",
            "phase_coverage",
            "5",
        ),
    ],
)
@pytest.mark.asyncio
async def test_known_work_prep_turn_is_terminal_full_audit_not_membership_only(
    utterance: str, expected_mode: str, expected_phase: str | None
):
    prep.install()
    store = _Store(utterance)
    registry = _Registry()
    client = _NoModelClient()
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

    assert client.calls == 0
    assert [name for name, _args, _kwargs in registry.invocations] == [prep.TOOL_NAME]
    args = registry.invocations[0][1]
    assert args["mode"] == expected_mode
    assert args.get("phase") == expected_phase
    assert args.get("coverage_focus") == (
        "adas_map" if expected_mode == "phase_coverage" else None
    )
    assert args[prep._CONTEXT_KEY]["message_id"] == 345
    text = "".join(event["text"] for event in events if event.get("type") == "token")
    assert text.startswith("No — 1 of 2")
    assert "ADAS Map: 1 verified; 0 genuinely missing; 1 unverified" in text
    assert "RO 200" in text
    assert "RO 100" not in text
    assert store.saved is not None and store.saved[0][2] == text
