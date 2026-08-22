import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.orchestrator import loop as loop_mod
from core.orchestrator.loop import (
    MAX_TOOL_ROUNDS,
    Orchestrator,
    calibration_iq_research_request,
)


LIVE_RO_NUMBER = "XOP-20260821211550-c28d41ae"
LIVE_RESEARCH_REQUEST = (
    f"For Calibration IQ RO {LIVE_RO_NUMBER}, re-run the complete OEM research "
    "for the existing blind spot detection calibration using ADAS SI, verify the "
    "persisted Subaru evidence and page citations, do not duplicate existing "
    "documents, and tell me the verified result."
)


class _Store:
    def __init__(self, history: list[dict[str, Any]] | None = None) -> None:
        self.history = list(history or [])
        self.saved: tuple[tuple[Any, ...], dict[str, Any]] | None = None
        self.title: str | None = None

    def get_messages(self, _conversation_id: int) -> list[dict[str, Any]]:
        return self.history

    def add_message(self, *args: Any, **kwargs: Any) -> int:
        self.saved = (args, kwargs)
        return 346

    def touch_conversation(self, _conversation_id: int, *, title: str) -> None:
        self.title = title


def _verified_research_result() -> dict[str, Any]:
    return {
        "status": "success",
        "executed": True,
        "success": True,
        "verified": True,
        "partial": False,
        "requested_count": 1,
        "processed_count": 1,
        "receipts": [
            {
                "operation": "ensure_case_workspace",
                "status": "completed",
                "success": True,
                "verification": {"verified": True},
            }
        ],
        "research": [
            {
                "repair_order_id": "c33ae574-7d88-48f2-aa0b-66230141b0ac",
                "required_calibrations": [
                    {"id": "cal-bsd", "label": "blind spot detection"}
                ],
                "final_required_calibrations": [
                    {"id": "cal-bsd", "label": "blind spot detection"}
                ],
                "documents_prepared": [],
                "already_present": [
                    {
                        "document_id": "doc-subaru-bsd",
                        "source": "2023 Subaru Forester BSD Calibration.pdf",
                        "source_uri": (
                            "adas-si:///2023%20Subaru%20Forester%20BSD%20Calibration.pdf"
                        ),
                    }
                ],
                "research_complete_requested": True,
                "research_complete_verified": True,
            }
        ],
        "final_snapshots": {
            "c33ae574-7d88-48f2-aa0b-66230141b0ac": {
                "status": "verified",
                "snapshot": {"research": {"state": "research_complete"}},
            }
        },
    }


class _Registry:
    def __init__(self, operator_result: dict[str, Any]) -> None:
        self.operator_result = operator_result
        self.invocations: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def model_tools(self, _role: str = "owner") -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {"name": name, "parameters": {"type": "object"}},
            }
            for name in (
                "calibration_iq_operator",
                "calibration_iq_ro",
                "adas_si_search",
                "adas_si_open",
            )
        ]

    @staticmethod
    def tier(name: str | None) -> str:
        return (
            "operator_authorized" if name == "calibration_iq_operator" else "read_only"
        )

    async def invoke(
        self, name: str, args: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.invocations.append((name, args, kwargs))
        if name == "calibration_iq_operator":
            return self.operator_result
        return {"status": "verified", "success": True, "verified": True}


class _LiveSequenceTrapClient:
    """The exact six-read sequence observed live if deterministic routing regresses."""

    sequence = (
        ("calibration_iq_ro", {"repair_order_id": LIVE_RO_NUMBER}),
        (
            "adas_si_search",
            {"query": "2023 Subaru Forester blind spot detection calibration"},
        ),
        (
            "adas_si_open",
            {"relative_path": "Subaru/Forester/BSD.pdf", "page": 1},
        ),
        (
            "adas_si_open",
            {"relative_path": "Subaru/Forester/BSD.pdf", "page": 2},
        ),
        (
            "adas_si_open",
            {"relative_path": "Subaru/Forester/BSD.pdf", "page": 3},
        ),
        (
            "adas_si_open",
            {"relative_path": "Subaru/Forester/BSD.pdf", "page": 4},
        ),
    )

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, _messages: list[dict], tools: Any = None):
        name, arguments = self.sequence[self.calls]
        self.calls += 1
        yield {
            "type": "tool_call",
            "id": f"live-read-{self.calls}",
            "name": name,
            "arguments": json.dumps(arguments),
        }


class _Router:
    active_name = "omni"


@pytest.mark.parametrize(
    ("text", "identifier", "complete"),
    [
        (LIVE_RESEARCH_REQUEST, LIVE_RO_NUMBER, True),
        (
            "Please research this Calibration IQ RO 2400911667 and attach the OEM "
            "evidence from ADAS SI.",
            "2400911667",
            False,
        ),
        (
            "For CIQ RO c33ae574-7d88-48f2-aa0b-66230141b0ac, verify the persisted "
            "OEM evidence and page citations.",
            "c33ae574-7d88-48f2-aa0b-66230141b0ac",
            False,
        ),
        (
            "For Calibration IQ repair order 2400911667, research and save the OEM "
            "documents, but do not mark the research complete.",
            "2400911667",
            False,
        ),
    ],
)
def test_explicit_ro_research_persistence_intent_builds_one_composite_action(
    text: str,
    identifier: str,
    complete: bool,
) -> None:
    assert calibration_iq_research_request(text) == {
        "actions": [
            {
                "operation": "research_ro",
                "repair_order_id": identifier,
                "arguments": {"complete_research": complete},
            }
        ]
    }


@pytest.mark.parametrize(
    "text",
    [
        (
            "What does ADAS SI say about calibrating blind spot detection on a 2023 "
            "Subaru Forester? Cite the OEM page."
        ),
        "In Calibration IQ, show me RO XOP-20260821211550-c28d41ae.",
        "Research the OEM blind spot procedure and show me the source page.",
        (
            "For Calibration IQ RO XOP-20260821211550-c28d41ae, show me the OEM "
            "procedure page without changing anything."
        ),
        (
            "Research Calibration IQ RO XOP-20260821211550-c28d41ae with ADAS SI, "
            "but do not persist or change anything."
        ),
    ],
)
def test_ordinary_adas_and_calibration_iq_reads_do_not_enter_operator_lane(
    text: str,
) -> None:
    assert calibration_iq_research_request(text) is None


@pytest.mark.asyncio
async def test_live_six_read_round_cap_is_bypassed_by_terminal_verified_operator_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert len(_LiveSequenceTrapClient.sequence) == MAX_TOOL_ROUNDS
    store = _Store([{"role": "user", "content": LIVE_RESEARCH_REQUEST}])
    registry = _Registry(_verified_research_result())
    client = _LiveSequenceTrapClient()
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
            LIVE_RESEARCH_REQUEST,
            approval_context={
                "session_id": "local:local-dev",
                "user_id": "local-dev",
                "role": "owner",
                "message_id": 345,
            },
        )
    ]

    assert client.calls == 0
    assert [item[0] for item in registry.invocations] == ["calibration_iq_operator"]
    _, arguments, context = registry.invocations[0]
    assert arguments == {
        "actions": [
            {
                "operation": "research_ro",
                "repair_order_id": LIVE_RO_NUMBER,
                "arguments": {"complete_research": True},
            }
        ]
    }
    assert context == {
        "message_id": 345,
        "conversation_id": 61,
        "tool_call_id": "routed_calibration_iq_operator_61_1",
        "user_id": "local-dev",
        "role": "owner",
    }
    assert [event["name"] for event in events if event["type"] == "tool_start"] == [
        "calibration_iq_operator"
    ]
    results = [event["result"] for event in events if event["type"] == "tool_result"]
    assert results == [_verified_research_result()]
    artifacts = [event["artifact"] for event in events if event["type"] == "artifact"]
    assert artifacts == [
        {"type": "calibration_iq_receipt", "data": _verified_research_result()}
    ]
    token_text = "".join(
        event["text"] for event in events if event.get("type") == "token"
    )
    assert token_text.startswith(
        "Calibration IQ verified the RO's OEM research as complete."
    )
    assert "instead of importing duplicate copies" in token_text
    assert "Verified receipts: 1 of 1" in token_text
    assert store.saved is not None
    assert store.saved[0][2] == token_text
    assert store.saved[0][2].strip()
    assert store.saved[1]["artifacts"] == artifacts
    assert events[-1] == {
        "type": "done",
        "message_id": 346,
        "worker": "omni",
        "artifacts": artifacts,
    }


@pytest.mark.asyncio
async def test_terminal_research_route_persists_nonempty_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = {
        "status": "failed",
        "executed": False,
        "success": False,
        "verified": False,
        "partial": False,
        "requested_count": 1,
        "processed_count": 0,
        "receipts": [],
        "error": {
            "code": "research_source_unavailable",
            "message": "ADAS SI research could not be completed. Nothing was changed.",
        },
    }
    store = _Store([{"role": "user", "content": LIVE_RESEARCH_REQUEST}])
    registry = _Registry(failure)
    client = _LiveSequenceTrapClient()
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
            LIVE_RESEARCH_REQUEST,
            approval_context={"message_id": 345, "role": "owner"},
        )
    ]

    assert client.calls == 0
    token_text = "".join(
        event["text"] for event in events if event.get("type") == "token"
    )
    assert "research_source_unavailable" in token_text
    assert "Nothing was changed" in token_text
    assert store.saved is not None
    assert store.saved[0][2] == token_text
    assert token_text.strip()


@pytest.mark.asyncio
async def test_ordinary_adas_question_keeps_model_selected_read_only_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_text = (
        "What does ADAS SI say about calibrating blind spot detection on a 2023 "
        "Subaru Forester? Cite the OEM page."
    )
    store = _Store([{"role": "user", "content": user_text}])
    registry = _Registry(_verified_research_result())

    class Client:
        calls = 0

        async def stream(self, _messages: list[dict], tools: Any = None):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_call",
                    "id": "ordinary-adas-search",
                    "name": "adas_si_search",
                    "arguments": json.dumps(
                        {
                            "query": (
                                "2023 Subaru Forester blind spot detection calibration"
                            )
                        }
                    ),
                }
                return
            yield {
                "type": "content",
                "text": "The OEM procedure and page citation are in the ADAS SI card.",
            }

    client = Client()
    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])
    orchestrator = Orchestrator(
        _Router(),
        client,
        registry,
        store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )

    events = [event async for event in orchestrator.run_turn(62, user_text)]

    assert client.calls == 2
    assert [item[0] for item in registry.invocations] == ["adas_si_search"]
    assert not any(
        item[0] == "calibration_iq_operator" for item in registry.invocations
    )
    token_text = "".join(
        event["text"] for event in events if event.get("type") == "token"
    )
    assert token_text == (
        "The OEM procedure and page citation are in the ADAS SI card."
    )
    assert store.saved is not None
    assert store.saved[0][2] == token_text
