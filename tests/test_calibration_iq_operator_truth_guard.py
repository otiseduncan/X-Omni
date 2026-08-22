import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.orchestrator import loop as loop_mod
from core.orchestrator.loop import (
    Orchestrator,
    calibration_iq_operator_failure_summary,
    calibration_iq_operator_terminal_summary,
)
from core.tools.registry import NeedsApproval


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
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = list(results)
        self.invocations: list[tuple[str, dict[str, Any]]] = []

    def model_tools(self, _role: str = "owner") -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "calibration_iq_operator",
                    "parameters": {"type": "object"},
                },
            }
        ]

    async def invoke(self, name: str, args: dict[str, Any], **_kwargs: Any) -> dict:
        self.invocations.append((name, args))
        return self.results.pop(0)


class _Router:
    active_name = "omni"


def _failed_result(
    *,
    code: str = "idempotency_conflict",
    message: str = "The idempotency key belongs to a different action.",
) -> dict[str, Any]:
    return {
        "status": "failed",
        "executed": False,
        "success": False,
        "verified": False,
        "partial": False,
        "requested_count": 1,
        "processed_count": 1,
        "error": {"code": code, "message": message, "retryable": False},
        "receipts": [
            {
                "operation": "add_note",
                "repair_order_id": "ro-1",
                "status": "failed",
                "success": False,
                "verification": {"verified": False},
                "error": {"code": code, "message": message},
            }
        ],
    }


def _successful_result() -> dict[str, Any]:
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
                "operation": "add_note",
                "repair_order_id": "ro-1",
                "status": "completed",
                "success": True,
                "resource_type": "note",
                "resource_id": "note-1",
                "verification": {"verified": True},
            }
        ],
        "final_snapshots": {
            "ro-1": {
                "status": "verified",
                "snapshot": {
                    "repair_order": {
                        "id": "ro-1",
                        "ro_number": "XOP-20260821211550-c28d41ae",
                        "version": 6,
                    },
                    "workflow": {"status": "REPAIR_IN_PROGRESS", "version": 6},
                },
            }
        },
    }


def _orchestrator(client: Any, registry: Any, store: _Store) -> Orchestrator:
    return Orchestrator(
        _Router(),
        client,
        registry,
        store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )


@pytest.mark.asyncio
async def test_failed_operator_replaces_model_invention_with_exact_receipt_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = _failed_result()
    registry = _Registry([failure])
    store = _Store()

    class Client:
        calls = 0

        async def stream(self, messages: list[dict], tools: Any = None):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "content", "text": "I will update the RO now."}
                yield {
                    "type": "tool_call",
                    "id": "operator-failure",
                    "name": "calibration_iq_operator",
                    "arguments": json.dumps(
                        {
                            "actions": [
                                {
                                    "operation": "add_note",
                                    "repair_order_id": "ro-1",
                                    "arguments": {"body": "ready"},
                                }
                            ]
                        }
                    ),
                }
                return
            assert any(message.get("role") == "tool" for message in messages)
            yield {
                "type": "content",
                "text": "That repair order was not found, so nothing could be done.",
            }

    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])
    events = [
        event
        async for event in _orchestrator(Client(), registry, store).run_turn(
            1, "Add a note to this repair order."
        )
    ]

    expected = calibration_iq_operator_failure_summary(failure)
    token_text = "".join(
        event["text"] for event in events if event.get("type") == "token"
    )
    assert token_text == expected
    assert "idempotency_conflict" in token_text
    assert "Verified actions: 0 of 1; processed actions: 1 of 1." in token_text
    assert "not found" not in token_text
    assert "I will update" not in token_text
    assert store.saved is not None
    assert store.saved[0][2] == expected


@pytest.mark.asyncio
async def test_later_verified_operator_self_correction_seals_success_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _Registry([_failed_result(), _successful_result()])
    store = _Store()

    class Client:
        calls = 0

        async def stream(self, _messages: list[dict], tools: Any = None):
            self.calls += 1
            if self.calls <= 2:
                yield {
                    "type": "tool_call",
                    "id": f"operator-attempt-{self.calls}",
                    "name": "calibration_iq_operator",
                    "arguments": json.dumps(
                        {
                            "actions": [
                                {
                                    "operation": "add_note",
                                    "repair_order_id": "ro-1",
                                    "arguments": {"body": "ready"},
                                }
                            ]
                        }
                    ),
                }
                return
            yield {
                "type": "content",
                "text": "The note is now verified in Calibration IQ.",
            }

    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])
    events = [
        event
        async for event in _orchestrator(Client(), registry, store).run_turn(
            1, "Add the note, correcting the request if needed."
        )
    ]

    token_text = "".join(
        event["text"] for event in events if event.get("type") == "token"
    )
    assert len(registry.invocations) == 2
    assert token_text == calibration_iq_operator_terminal_summary(
        [_failed_result(), _successful_result()]
    )
    assert "verified 1 of 1 requested actions" in token_text
    assert "add_note -> type=note, id=note-1" in token_text
    assert "version=6" in token_text
    assert "Structured error" not in token_text
    assert "The note is now" not in token_text
    assert store.saved is not None
    assert store.saved[0][2] == token_text


@pytest.mark.asyncio
async def test_verified_success_replaces_false_version_change_prose_and_keeps_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    success = _successful_result()
    registry = _Registry([success])
    store = _Store()

    class Client:
        calls = 0

        async def stream(self, _messages: list[dict], tools: Any = None):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_call",
                    "id": "operator-success",
                    "name": "calibration_iq_operator",
                    "arguments": json.dumps(
                        {
                            "actions": [
                                {
                                    "operation": "add_note",
                                    "repair_order_id": "ro-1",
                                    "arguments": {"body": "ready"},
                                }
                            ]
                        }
                    ),
                }
                return
            yield {
                "type": "content",
                "text": "The RO version has been incremented to 6.",
            }

    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])
    events = [
        event
        async for event in _orchestrator(Client(), registry, store).run_turn(
            1, "Add the shared note."
        )
    ]

    expected = calibration_iq_operator_terminal_summary([success])
    token_text = "".join(
        event["text"] for event in events if event.get("type") == "token"
    )
    assert token_text == expected
    assert "incremented" not in token_text
    assert "changed" not in token_text
    assert "Current repair order:" in token_text
    assert "number=XOP-20260821211550-c28d41ae" in token_text
    assert "status=REPAIR_IN_PROGRESS" in token_text
    assert "version=6" in token_text
    assert any(
        event.get("type") == "artifact"
        and (event.get("artifact") or {}).get("type") == "calibration_iq_receipt"
        for event in events
    )
    assert store.saved is not None
    assert store.saved[0][2] == expected


@pytest.mark.asyncio
async def test_failed_approved_destructive_result_uses_same_truth_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = _failed_result(code="conflict", message="The photo version changed.")
    store = _Store()

    class Client:
        async def stream(self, _messages: list[dict], tools: Any = None):
            yield {
                "type": "content",
                "text": "The photo was already missing from that repair order.",
            }

    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])
    approved = {
        "name": "calibration_iq_destructive",
        "args": {"actions": [{"operation": "delete_photo", "target_id": "p-1"}]},
        "result": failure,
        "receipt": {
            "tool_name": "calibration_iq_destructive",
            "status": "failed",
            "executed": True,
            "success": False,
            "result": failure,
        },
        "call_id": "approved-delete-photo",
    }
    events = [
        event
        async for event in _orchestrator(Client(), _Registry([]), store).run_turn(
            1, "Delete that photo.", approved_tool=approved
        )
    ]

    token_text = "".join(
        event["text"] for event in events if event.get("type") == "token"
    )
    assert token_text == calibration_iq_operator_failure_summary(failure)
    assert "conflict" in token_text
    assert "already missing" not in token_text


@pytest.mark.asyncio
async def test_verified_approved_destructive_success_is_receipt_sealed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    success = _successful_result()
    success["receipts"][0].update(
        {
            "operation": "delete_photo",
            "resource_type": "photo",
            "resource_id": "photo-1",
            "risk": "destructive",
            "after": {"id": "photo-1", "deleted_at": "2026-08-21T12:00:00Z"},
        }
    )
    store = _Store()

    class Client:
        async def stream(self, _messages: list[dict], tools: Any = None):
            yield {
                "type": "content",
                "text": "The photo and all of its retained bytes were permanently erased.",
            }

    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])
    approved = {
        "name": "calibration_iq_destructive",
        "args": {"actions": [{"operation": "delete_photo", "target_id": "photo-1"}]},
        "result": success,
        "receipt": {
            "tool_name": "calibration_iq_destructive",
            "status": "succeeded",
            "executed": True,
            "success": True,
            "result": success,
        },
        "call_id": "approved-delete-photo",
    }
    events = [
        event
        async for event in _orchestrator(Client(), _Registry([]), store).run_turn(
            1, "Delete that photo.", approved_tool=approved
        )
    ]

    token_text = "".join(
        event["text"] for event in events if event.get("type") == "token"
    )
    assert token_text == calibration_iq_operator_terminal_summary([success])
    assert "delete_photo -> type=photo, id=photo-1" in token_text
    assert "permanently erased" not in token_text
    assert any(
        event.get("type") == "artifact"
        and (event.get("artifact") or {}).get("type") == "calibration_iq_receipt"
        for event in events
    )


def test_partial_summary_reports_verified_and_processed_action_counts() -> None:
    result = {
        "status": "partial_success",
        "success": False,
        "verified": False,
        "partial": True,
        "requested_count": 3,
        "processed_count": 2,
        "receipts": [
            {
                "status": "completed",
                "success": True,
                "verification": {"verified": True},
            },
            {
                "status": "failed",
                "success": False,
                "verification": {"verified": False},
                "error": {
                    "code": "prerequisite_missing",
                    "message": "The required calibration link is missing.",
                },
            },
        ],
    }
    summary = calibration_iq_operator_failure_summary(result)
    assert summary.startswith(
        "Calibration IQ only partially verified the requested operation."
    )
    assert "prerequisite_missing" in summary
    assert "Verified actions: 1 of 3; processed actions: 2 of 3." in summary


@pytest.mark.parametrize("action_count", [1, 2, 5])
def test_success_summary_lists_every_verified_operation_and_handles_empty_snapshots(
    action_count: int,
) -> None:
    receipts = [
        {
            "operation": f"operation_{index}",
            "status": "completed",
            "success": True,
            "resource_type": "workspace_entry",
            "resource_id": f"resource-{index}",
            "after": {"path": f"research/path-{index}"},
            "verification": {"verified": True},
        }
        for index in range(1, action_count + 1)
    ]
    result = {
        "status": "success",
        "success": True,
        "verified": True,
        "partial": False,
        "requested_count": action_count,
        "processed_count": action_count,
        "receipts": receipts,
        "final_snapshots": {},
    }

    summary = calibration_iq_operator_terminal_summary([result])

    assert f"verified {action_count} of {action_count} requested actions" in summary
    for index in range(1, action_count + 1):
        assert (
            f"{index}) operation_{index} -> type=workspace_entry, "
            f"id=resource-{index}, path=research/path-{index}"
        ) in summary
    assert (
        "Current repair-order state: unavailable because no verified final snapshot "
        "was returned."
    ) in summary


def test_sequential_successes_aggregate_and_later_failure_makes_turn_partial() -> None:
    add_note = _successful_result()
    create_folder = _successful_result()
    create_folder["receipts"][0].update(
        {
            "operation": "create_folder",
            "resource_type": "workspace_entry",
            "resource_id": "x-natural-proof",
            "after": {"path": "x-natural-proof"},
        }
    )
    summary = calibration_iq_operator_terminal_summary([add_note, create_folder])
    assert "verified 2 of 2 requested actions" in summary
    assert "1) add_note -> type=note, id=note-1" in summary
    assert (
        "2) create_folder -> type=workspace_entry, id=x-natural-proof, "
        "path=x-natural-proof"
    ) in summary

    later_failure = _failed_result(
        code="version_conflict", message="The expected version is no longer current."
    )
    later_failure["receipts"][0]["operation"] = "update_ro"
    partial = calibration_iq_operator_terminal_summary([add_note, later_failure])
    assert "only partially verified this operator turn" in partial
    assert "Verified actions: 1 of 2; processed actions: 2 of 2." in partial
    assert "add_note -> type=note, id=note-1" in partial
    assert "Latest structured error: version_conflict" in partial


def test_completed_receipt_cannot_override_failed_top_level_outcome() -> None:
    failed_outcome = _successful_result()
    failed_outcome.update(
        {
            "status": "failed",
            "success": False,
            "verified": False,
            "partial": False,
            "error": {
                "code": "final_snapshot_unverified",
                "message": "The authoritative final snapshot reread failed.",
            },
            "final_snapshots": {
                "ro-1": {
                    "status": "error",
                    "error": {"code": "temporary_service_failure"},
                }
            },
        }
    )

    summary = calibration_iq_operator_terminal_summary([failed_outcome])

    assert summary.startswith("Calibration IQ only partially verified")
    assert "Verified actions: 1 of 1" in summary
    assert not summary.startswith("Calibration IQ verified 1 of 1")
    assert "final_snapshot_unverified" in summary
    assert "number=XOP-20260821211550-c28d41ae" not in summary

    complete = _successful_result()
    complete["receipts"][0]["mutation_id"] = "mutation-replayed"
    failed_outcome["receipts"][0]["mutation_id"] = "mutation-replayed"
    replay_summary = calibration_iq_operator_terminal_summary(
        [complete, failed_outcome]
    )
    assert replay_summary.startswith("Calibration IQ only partially verified")
    assert not replay_summary.startswith("Calibration IQ verified 1 of 1")


def test_retry_suppression_is_strong_and_one_to_one() -> None:
    first_failure = _failed_result(message="First attempt failed.")
    second_failure = _failed_result(message="Second attempt failed.")
    success = _successful_result()

    one_retry = calibration_iq_operator_terminal_summary(
        [first_failure, second_failure, success]
    )
    assert "only partially verified this operator turn" in one_retry
    assert "Verified actions: 1 of 2; processed actions: 2 of 2." in one_retry

    no_ro_failure = _failed_result()
    no_ro_success = _successful_result()
    no_ro_failure["receipts"][0].pop("repair_order_id")
    no_ro_success["receipts"][0].pop("repair_order_id")
    unmatched = calibration_iq_operator_terminal_summary([no_ro_failure, no_ro_success])
    assert "Verified actions: 1 of 2; processed actions: 2 of 2." in unmatched

    no_ro_failure["receipts"][0]["mutation_id"] = "mutation-exact"
    no_ro_success["receipts"][0]["mutation_id"] = "mutation-exact"
    exact = calibration_iq_operator_terminal_summary([no_ro_failure, no_ro_success])
    assert "verified 1 of 1 requested actions" in exact
    assert "Structured error" not in exact

    mismatched_failure = _failed_result()
    mismatched_success = _successful_result()
    mismatched_failure["receipts"][0].update(
        {
            "mutation_id": "mutation-failed",
            "idempotency_key": "key-failed",
            "correlation_id": "correlation-failed",
        }
    )
    mismatched_success["receipts"][0].update(
        {
            "mutation_id": "mutation-success",
            "idempotency_key": "key-success",
            "correlation_id": "correlation-success",
        }
    )
    mismatched = calibration_iq_operator_terminal_summary(
        [mismatched_failure, mismatched_success]
    )
    assert "only partially verified this operator turn" in mismatched
    assert "Verified actions: 1 of 2; processed actions: 2 of 2." in mismatched


def test_multiple_unverified_calls_keep_aggregate_counts() -> None:
    first = _failed_result(code="note_conflict", message="The note version changed.")
    second = _failed_result(code="path_conflict", message="The folder already exists.")
    second["receipts"][0].update(
        {"operation": "create_folder", "repair_order_id": "ro-2"}
    )

    summary = calibration_iq_operator_terminal_summary([first, second])

    assert summary.startswith("Calibration IQ did not verify this operator turn.")
    assert "Verified actions: 0 of 2; processed actions: 2 of 2." in summary
    assert "path_conflict" in summary


def test_later_indeterminate_result_invalidates_older_current_snapshot() -> None:
    indeterminate = {
        "status": "failed",
        "executed": False,
        "success": False,
        "verified": False,
        "partial": False,
        "requested_count": 1,
        "processed_count": 0,
        "receipts": [],
        "error": {
            "code": "indeterminate",
            "message": "No authoritative final reread was returned.",
        },
    }

    summary = calibration_iq_operator_terminal_summary(
        [_successful_result(), indeterminate]
    )

    assert "only partially verified this operator turn" in summary
    assert "number=XOP-20260821211550-c28d41ae" not in summary
    assert "version=6" not in summary
    assert "no verified final snapshot was returned" in summary

    zero_count_invalid = {"status": "unverified_result"}
    zero_count_summary = calibration_iq_operator_terminal_summary(
        [_successful_result(), zero_count_invalid]
    )
    assert zero_count_summary.startswith(
        "Calibration IQ only partially verified this operator turn"
    )
    assert not zero_count_summary.startswith("Calibration IQ verified 1 of 1")
    assert "number=XOP-20260821211550-c28d41ae" not in zero_count_summary
    assert "version=6" not in zero_count_summary


@pytest.mark.asyncio
async def test_approved_success_fails_closed_when_outer_receipt_does_not_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    success = _successful_result()
    store = _Store()

    class Client:
        async def stream(self, _messages: list[dict], tools: Any = None):
            yield {"type": "content", "text": "The photo was deleted successfully."}

    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])
    approved = {
        "name": "calibration_iq_destructive",
        "args": {"actions": [{"operation": "delete_photo", "target_id": "photo-1"}]},
        "result": success,
        "receipt": {
            "tool_name": "calibration_iq_destructive",
            "status": "succeeded",
            "executed": True,
            "success": True,
            "result": {"status": "tampered"},
        },
        "call_id": "approved-mismatch",
    }
    events = [
        event
        async for event in _orchestrator(Client(), _Registry([]), store).run_turn(
            1, "Delete that photo.", approved_tool=approved
        )
    ]

    token_text = "".join(
        event["text"] for event in events if event.get("type") == "token"
    )
    assert "approval_receipt_mismatch" in token_text
    assert "deleted successfully" not in token_text
    assert "Calibration IQ verified" not in token_text
    ciq_card = next(
        event["artifact"]["data"]
        for event in events
        if event.get("type") == "artifact"
        and (event.get("artifact") or {}).get("type") == "calibration_iq_receipt"
    )
    assert ciq_card["status"] == "failed"


@pytest.mark.asyncio
async def test_routine_success_is_summarized_before_destructive_approval_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    success = _successful_result()

    class ApprovalStore(_Store):
        def create_approval(self, *_args: Any, **_kwargs: Any) -> str:
            return "approval-1"

        def get_approval(self, _approval_id: str) -> dict[str, Any]:
            return {
                "id": "approval-1",
                "tool_name": "calibration_iq_destructive",
                "status": "pending",
                "summary": "Delete photo photo-1",
                "args": {"target_id": "photo-1"},
                "action_digest": "digest-1",
                "idempotency_key": "approval-key-1",
            }

    class ApprovalRegistry:
        protected_handler_ran = False

        def model_tools(self, _role: str = "owner") -> list[dict[str, Any]]:
            return [
                {
                    "type": "function",
                    "function": {"name": name, "parameters": {"type": "object"}},
                }
                for name in (
                    "calibration_iq_operator",
                    "calibration_iq_destructive",
                )
            ]

        async def invoke(self, name: str, _args: dict, **_kwargs: Any) -> dict:
            if name == "calibration_iq_operator":
                return success
            if name == "calibration_iq_destructive":
                raise NeedsApproval(name, _args, "Delete photo photo-1")
            self.protected_handler_ran = True
            raise AssertionError(name)

        @staticmethod
        def log_args(_name: str, args: dict) -> dict:
            return args

        @staticmethod
        def public_approval(record: dict, receipt: Any = None) -> dict:
            del receipt
            return {
                "tool": record["tool_name"],
                "summary": record["summary"],
                "args": record["args"],
            }

    class Client:
        async def stream(self, _messages: list[dict], tools: Any = None):
            yield {"type": "content", "text": "Everything is already complete."}
            yield {
                "type": "tool_call",
                "id": "routine-call",
                "name": "calibration_iq_operator",
                "arguments": json.dumps(
                    {
                        "actions": [
                            {
                                "operation": "add_note",
                                "repair_order_id": "ro-1",
                                "arguments": {"body": "ready"},
                            }
                        ]
                    }
                ),
            }
            yield {
                "type": "tool_call",
                "id": "destructive-call",
                "name": "calibration_iq_destructive",
                "arguments": json.dumps(
                    {"actions": [{"operation": "delete_photo", "target_id": "photo-1"}]}
                ),
            }

    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])
    store = ApprovalStore()
    registry = ApprovalRegistry()
    events = [
        event
        async for event in _orchestrator(Client(), registry, store).run_turn(
            1,
            "Add the note, then delete the photo.",
            approval_context={
                "session_id": "local:owner",
                "user_id": "owner",
                "message_id": 10,
            },
        )
    ]

    expected = calibration_iq_operator_terminal_summary([success])
    token_text = "".join(
        event["text"] for event in events if event.get("type") == "token"
    )
    assert token_text == expected
    assert "Everything is already complete" not in token_text
    assert any(event.get("type") == "approval" for event in events)
    assert registry.protected_handler_ran is False
    assert store.saved is not None
    assert store.saved[0][2] == expected
