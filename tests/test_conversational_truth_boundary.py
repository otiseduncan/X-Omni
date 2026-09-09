"""Acceptance tests for the model-first conversational boundary.

The repaired contract: the model decides what Otis means and what to say;
deterministic systems decide what is allowed to happen and what facts have
been proven. Concretely --

* a valid model response is never replaced by a deterministic service-prose
  report (the old protected terminal summaries are audit renderers only);
* read turns flow straight through with no truth-review call;
* mutation turns are validated by a bounded model truth review with at most
  one language-only regeneration, then a tiny fail-closed line;
* language repair can never re-execute a mutation;
* the full structured evidence remains available to the model and on the
  turn's artifacts even though it is not automatically narrated.

Fake clients script the review path structurally: the orchestrator passes
``tools=None`` for every truth-review call, so those calls are distinguished
by that -- not by inspecting any prose.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.orchestrator import loop as loop_mod
from core.orchestrator import truth_review as truth_review_mod
from core.orchestrator.loop import (
    Orchestrator,
    calibration_iq_mutation_truth_review_required,
    calibration_iq_work_prep_terminal_summary,
)


class _Store:
    def __init__(self) -> None:
        self.saved: tuple[tuple[Any, ...], dict[str, Any]] | None = None

    def get_messages(self, _conversation_id: int) -> list[dict[str, Any]]:
        return []

    def add_message(self, *args: Any, **kwargs: Any) -> int:
        self.saved = (args, kwargs)
        return 77

    def touch_conversation(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Router:
    active_name = "omni"

    @staticmethod
    def active_config():
        return SimpleNamespace(supports_vision=True, supports_audio=True)


class _Registry:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = list(results)
        self.invocations: list[tuple[str, dict[str, Any]]] = []

    def model_tools(self, _role: str = "owner") -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {"name": name, "parameters": {"type": "object"}},
            }
            for name in (
                "calibration_iq_summary",
                "calibration_iq_work_prep",
                "calibration_iq_operator",
            )
        ]

    async def invoke(self, name: str, args: dict[str, Any], **_kwargs: Any) -> dict:
        self.invocations.append((name, args))
        return self.results.pop(0)


class _Client:
    """Scripted turn driver.

    ``tool_rounds`` feed rounds that end in tool calls; the next tools-present
    call yields ``candidate``. Calls with ``tools=None`` (truth review,
    regeneration, synthesis) pop from ``review_responses``.
    """

    def __init__(self, tool_rounds=None, candidate="", review_responses=()):
        self.tool_rounds = [list(events) for events in (tool_rounds or [])]
        self.candidate = candidate
        self.review_responses = list(review_responses)
        self.no_tool_calls = 0

    async def stream(self, _messages, tools=None):
        if tools is None:
            self.no_tool_calls += 1
            yield {"type": "content", "text": self.review_responses.pop(0)}
            return
        if self.tool_rounds:
            for event in self.tool_rounds.pop(0):
                yield event
            return
        yield {"type": "content", "text": self.candidate}


VALID_REVIEW = json.dumps(
    {
        "valid": True,
        "unsupported_claims": [],
        "contradictions": [],
        "correction_guidance": "",
    }
)
INVALID_REVIEW = json.dumps(
    {
        "valid": False,
        "unsupported_claims": [],
        "contradictions": ["The evidence does not verify that claim."],
        "correction_guidance": "State only what the receipts prove.",
    }
)


def _tool_call(name: str, args: dict[str, Any], call_id: str = "call-1") -> dict:
    return {
        "type": "tool_call",
        "id": call_id,
        "name": name,
        "arguments": json.dumps(args),
    }


def _read_result() -> dict[str, Any]:
    return {
        "status": "verified",
        "mode": "week_readiness",
        "success": True,
        "verified": True,
        "readiness_complete": False,
        "queue_count": 51,
        "ready_count": 36,
        "exception_count": 15,
        "si_missing_count": 15,
        "adas_map_verified_count": 36,
        "reconciliation_failed_count": 0,
        "phase_scope": ["5", "6", "7", "8"],
        "repair_orders": [
            {"ro_number": "11840", "vehicle": "Vehicle A", "ready": False},
            {"ro_number": "11841", "vehicle": "Vehicle B", "ready": True},
        ],
    }


def _close_success(ro: str = "11774") -> dict[str, Any]:
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
                "operation": "close_ro",
                "repair_order_id": f"ro-{ro}",
                "status": "completed",
                "success": True,
                "resource_type": "repair_order",
                "resource_id": f"ro-{ro}",
                "verification": {"verified": True},
            }
        ],
        "final_snapshots": {
            f"ro-{ro}": {
                "status": "verified",
                "snapshot": {
                    "repair_order": {"id": f"ro-{ro}", "ro_number": ro, "version": 9},
                    "workflow": {"status": "COMPLETE", "version": 9},
                },
            }
        },
    }


def _close_failure(ro: str = "11774") -> dict[str, Any]:
    return {
        "status": "failed",
        "executed": True,
        "success": False,
        "verified": False,
        "partial": False,
        "requested_count": 1,
        "processed_count": 1,
        "error": {"code": "conflict", "message": "Version changed.", "retryable": False},
        "receipts": [
            {
                "operation": "close_ro",
                "repair_order_id": f"ro-{ro}",
                "status": "failed",
                "success": False,
                "verification": {"verified": False},
                "error": {"code": "conflict", "message": "Version changed."},
            }
        ],
    }


def _orchestrator(client, registry, store):
    return Orchestrator(
        _Router(),
        client,
        registry,
        store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )


async def _turn(client, registry, store, text="How many ROs need SI?"):
    return [
        event
        async for event in _orchestrator(client, registry, store).run_turn(1, text)
    ]


def _tokens(events) -> str:
    return "".join(e["text"] for e in events if e.get("type") == "token")


# ---------------------------------------------------------------------------
# read turns: model-owned, concise, unreviewed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concise_read_answer_survives_and_skips_review(monkeypatch) -> None:
    """'How many need SI?' -> '15.' -- not a multi-section audit report."""
    registry = _Registry([_read_result()])
    store = _Store()
    client = _Client(
        tool_rounds=[[_tool_call("calibration_iq_work_prep", {"mode": "week_readiness"})]],
        candidate="15.",
    )
    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])

    events = await _turn(client, registry, store)
    text = _tokens(events)

    assert text == "15."
    assert client.no_tool_calls == 0  # no truth review on a read turn
    assert store.saved is not None and store.saved[0][2] == "15."


@pytest.mark.asyncio
async def test_deterministic_core_cannot_replace_a_valid_model_read_answer(
    monkeypatch,
) -> None:
    """Regression: the old protected path replaced this with service prose."""
    result = _read_result()
    registry = _Registry([result])
    store = _Store()
    client = _Client(
        tool_rounds=[[_tool_call("calibration_iq_work_prep", {"mode": "week_readiness"})]],
        candidate="15 of the 51 active ROs still need SI.",
    )
    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])

    text = _tokens(await _turn(client, registry, store))

    assert text == "15 of the 51 active ROs still need SI."
    old_summary = calibration_iq_work_prep_terminal_summary([result])
    assert text != old_summary
    assert "CIQ mutation receipts" not in text


@pytest.mark.asyncio
async def test_model_receives_full_structured_evidence_for_follow_ups(
    monkeypatch,
) -> None:
    """Concision must come from the model, not from starving it of evidence.

    The projected tool result the model sees still carries the rows, counts,
    and exception detail needed to answer 'Which ones?' or 'Why those?' later.
    """
    registry = _Registry([_read_result()])
    store = _Store()
    seen_tool_messages: list[str] = []

    class RecordingClient(_Client):
        async def stream(self, messages, tools=None):
            for message in messages:
                if message.get("role") == "tool":
                    seen_tool_messages.append(str(message.get("content")))
            async for event in super().stream(messages, tools=tools):
                yield event

    client = RecordingClient(
        tool_rounds=[[_tool_call("calibration_iq_work_prep", {"mode": "week_readiness"})]],
        candidate="15.",
    )
    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])

    text = _tokens(await _turn(client, registry, store))

    assert text == "15."
    evidence = "\n".join(seen_tool_messages)
    assert "11840" in evidence
    assert '"exception_count": 15' in evidence
    assert '"queue_count": 51' in evidence


# ---------------------------------------------------------------------------
# mutation turns: truth-reviewed, never replayed
# ---------------------------------------------------------------------------


def _close_call() -> dict:
    return _tool_call(
        "calibration_iq_operator",
        {"actions": [{"operation": "close_ro", "repair_order_id": "ro-11774"}]},
        "close-11774",
    )


@pytest.mark.asyncio
async def test_verified_close_releases_concise_candidate_unchanged(
    monkeypatch,
) -> None:
    """'Done. RO 11774 is closed.' is valid and must not grow an audit tail."""
    registry = _Registry([_close_success()])
    store = _Store()
    candidate = "Done. RO 11774 is closed."
    client = _Client(
        tool_rounds=[[_close_call()]],
        candidate=candidate,
        review_responses=[VALID_REVIEW],
    )
    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])

    text = _tokens(await _turn(client, registry, store, "Close RO 11774."))

    assert text == candidate
    assert client.no_tool_calls == 1
    assert "receipt" not in text.casefold()
    assert "version" not in text.casefold()
    assert store.saved is not None and store.saved[0][2] == candidate


@pytest.mark.asyncio
async def test_false_close_claim_is_regenerated_without_replaying_the_mutation(
    monkeypatch,
) -> None:
    registry = _Registry([_close_failure()])
    store = _Store()
    truthful = "I couldn't close RO 11774; Calibration IQ still shows it open."
    client = _Client(
        tool_rounds=[[_close_call()]],
        candidate="Done. RO 11774 is closed.",
        review_responses=[INVALID_REVIEW, truthful, VALID_REVIEW],
    )
    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])

    text = _tokens(await _turn(client, registry, store, "Close RO 11774."))

    assert text == truthful
    assert "Done." not in text
    # The one execution already happened; language repair added zero more.
    assert len(registry.invocations) == 1
    assert client.no_tool_calls == 3


@pytest.mark.asyncio
async def test_double_invalid_regeneration_falls_closed_without_audit_dump(
    monkeypatch,
) -> None:
    registry = _Registry([_close_success()])
    store = _Store()
    client = _Client(
        tool_rounds=[[_close_call()]],
        candidate="Done. RO 11774 is closed and archived and emailed.",
        review_responses=[
            INVALID_REVIEW,
            "It is closed, archived, emailed, and framed.",
            INVALID_REVIEW,
        ],
    )
    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])

    text = _tokens(await _turn(client, registry, store, "Close RO 11774."))

    # Tiny deterministic fallback chosen from structural verification state --
    # never a rebuilt audit report, never a third model attempt.
    assert text == truth_review_mod.fail_closed_text("verified")
    assert "CIQ mutation receipts" not in text
    assert len(registry.invocations) == 1
    assert client.no_tool_calls == 3


@pytest.mark.asyncio
async def test_unavailable_reviewer_never_releases_unsupported_success(
    monkeypatch,
) -> None:
    """Garbage reviewer output = reviewer unavailable = fail closed."""
    registry = _Registry([_close_failure()])
    store = _Store()
    client = _Client(
        tool_rounds=[[_close_call()]],
        candidate="Done. RO 11774 is closed.",
        review_responses=["I think it looks fine to me."],
    )
    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])

    text = _tokens(await _turn(client, registry, store, "Close RO 11774."))

    assert text == truth_review_mod.fail_closed_text("failed")
    assert "Done." not in text
    assert len(registry.invocations) == 1
    assert client.no_tool_calls == 1  # parse failure stops the review path


@pytest.mark.asyncio
async def test_mixed_read_and_mutation_turn_is_reviewed_once_no_duplicate_execution(
    monkeypatch,
) -> None:
    registry = _Registry([_read_result(), _close_success()])
    store = _Store()
    candidate = "Closed RO 11774; 15 others still need SI."
    client = _Client(
        tool_rounds=[
            [
                _tool_call(
                    "calibration_iq_work_prep",
                    {"mode": "week_readiness"},
                    "read-first",
                ),
                _close_call(),
            ]
        ],
        candidate=candidate,
        review_responses=[VALID_REVIEW],
    )
    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])

    text = _tokens(
        await _turn(client, registry, store, "Close 11774 and tell me what's left.")
    )

    assert text == candidate
    assert client.no_tool_calls == 1
    assert [name for name, _ in registry.invocations] == [
        "calibration_iq_work_prep",
        "calibration_iq_operator",
    ]


@pytest.mark.asyncio
async def test_receipts_and_audit_remain_available_off_the_chat_path(
    monkeypatch,
) -> None:
    """Removing automatic audit prose must not remove the audit itself."""
    success = _close_success()
    registry = _Registry([success])
    store = _Store()
    client = _Client(
        tool_rounds=[[_close_call()]],
        candidate="Done. RO 11774 is closed.",
        review_responses=[VALID_REVIEW],
    )
    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])

    events = await _turn(client, registry, store, "Close RO 11774.")

    # The receipt card still carries the full structured result.
    card = next(
        event["artifact"]
        for event in events
        if event.get("type") == "artifact"
        and (event.get("artifact") or {}).get("type") == "calibration_iq_receipt"
    )
    card_json = json.dumps(card)
    assert "close_ro" in card_json
    assert "COMPLETE" in card_json
    # And the audit renderer still reproduces receipt truth on demand.
    rendered = loop_mod.calibration_iq_operator_terminal_summary([success])
    assert "close_ro" in rendered


# ---------------------------------------------------------------------------
# trigger metadata: structural, never linguistic
# ---------------------------------------------------------------------------


def test_review_trigger_is_derived_from_structured_results_only() -> None:
    read_only = _read_result()
    assert not calibration_iq_mutation_truth_review_required([], [read_only])
    assert calibration_iq_mutation_truth_review_required([_close_success()], [])
    mutated = dict(read_only)
    mutated["ciq_receipt_count"] = 1
    mutated["ciq_verified_receipt_count"] = 1
    assert calibration_iq_mutation_truth_review_required([], [mutated])
    acquisition = {"mode": "ro_si_acquire", "executed": True}
    assert calibration_iq_mutation_truth_review_required([], [acquisition])
