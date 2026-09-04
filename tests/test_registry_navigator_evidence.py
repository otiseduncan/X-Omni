from __future__ import annotations

from typing import Any

import pytest

from core.tools.registry import (
    NavigatorTurnEvidence,
    ToolBlocked,
    navigator_apply_new_quarantine,
    navigator_evidence_from_result,
    validate_navigator_task_binding,
)


def _verified_navigator_result(action: str, data: Any) -> dict[str, Any]:
    return {
        "service": "ScrapeX",
        "action": action,
        "status": "verified",
        "success": True,
        "executed": True,
        "verified": True,
        "data": data,
    }


def test_navigator_evidence_is_minted_only_from_verified_create_task_results() -> None:
    created = navigator_evidence_from_result(
        "scrapex_navigator",
        {"action": "create_task", "provider": "alldata", "target": {}, "topic": "t"},
        _verified_navigator_result("create_task", {"id": "task-1"}),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="create-call",
    )
    assert created is not None
    assert created.task_ids == ("task-1",)
    assert created.source_tool_call_ids == ("create-call",)

    malformed = _verified_navigator_result("create_task", {"task_id": "no-id-field"})
    assert navigator_evidence_from_result(
        "scrapex_navigator",
        {"action": "create_task", "provider": "alldata", "target": {}, "topic": "t"},
        malformed,
        conversation_id=11,
        message_id=22,
        source_tool_call_id="bad-create",
    ) is None

    for unsafe_result in (
        {**_verified_navigator_result("create_task", {"id": "task-untrusted"}), "action": "observe"},
        {**_verified_navigator_result("create_task", {"id": "task-untrusted"}), "verified": False},
        {**_verified_navigator_result("create_task", {"id": "task-untrusted"}), "authentication_required": True},
        {
            **_verified_navigator_result("create_task", {"id": "task-untrusted"}),
            "status": "indeterminate",
            "may_have_executed": True,
        },
    ):
        assert navigator_evidence_from_result(
            "scrapex_navigator",
            {"action": "create_task", "provider": "alldata", "target": {}, "topic": "t"},
            unsafe_result,
            conversation_id=11,
            message_id=22,
            source_tool_call_id="unsafe-call",
        ) is None


def test_other_tools_never_mint_navigator_evidence() -> None:
    assert navigator_evidence_from_result(
        "scrapex_read",
        {"action": "list_batches"},
        _verified_navigator_result("list_batches", {"id": "task-should-not-count"}),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="wrong-tool",
    ) is None


def test_bound_navigator_results_can_preserve_but_never_mint_an_opaque_id() -> None:
    created = navigator_evidence_from_result(
        "scrapex_navigator",
        {"action": "create_task", "provider": "alldata", "target": {}, "topic": "t"},
        _verified_navigator_result("create_task", {"id": "task-created"}),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="create-call",
    )
    assert created is not None

    preserved = navigator_evidence_from_result(
        "scrapex_navigator",
        {"action": "observe", "task_id": "task-created"},
        _verified_navigator_result("observe", {"url": "https://x/", "elements": []}),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="observe-call",
        previous=created,
    )
    assert preserved is not None
    assert preserved.task_ids == ("task-created",)
    assert preserved.source_tool_call_ids == ("create-call", "observe-call")

    invented = navigator_evidence_from_result(
        "scrapex_navigator",
        {"action": "observe", "task_id": "task-invented"},
        _verified_navigator_result("observe", {"url": "https://x/", "elements": []}),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="invented-call",
        previous=created,
    )
    assert invented == created

    stale = navigator_evidence_from_result(
        "scrapex_navigator",
        {"action": "observe", "task_id": "task-created"},
        _verified_navigator_result("observe", {"url": "https://x/", "elements": []}),
        conversation_id=11,
        message_id=23,
        source_tool_call_id="stale-call",
        previous=created,
    )
    assert stale is None


def test_indeterminate_id_bound_action_quarantines_only_that_task_for_turn() -> None:
    evidence = navigator_evidence_from_result(
        "scrapex_navigator",
        {"action": "create_task", "provider": "alldata", "target": {}, "topic": "t"},
        _verified_navigator_result("create_task", {"id": "task-risk"}),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="create-risk",
    )
    evidence = navigator_evidence_from_result(
        "scrapex_navigator",
        {"action": "create_task", "provider": "alldata", "target": {}, "topic": "t2"},
        _verified_navigator_result("create_task", {"id": "task-safe"}),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="create-safe",
        previous=evidence,
    )
    assert evidence is not None
    assert set(evidence.task_ids) == {"task-risk", "task-safe"}

    quarantined = navigator_evidence_from_result(
        "scrapex_navigator",
        {"action": "click", "task_id": "task-risk", "ref": "e1"},
        {
            "service": "ScrapeX",
            "action": "click",
            "status": "indeterminate",
            "success": False,
            "executed": False,
            "verified": False,
            "may_have_executed": True,
            "indeterminate": True,
        },
        conversation_id=11,
        message_id=22,
        source_tool_call_id="ambiguous-click",
        previous=evidence,
    )
    assert quarantined is not None
    assert quarantined.task_ids == ("task-safe",)
    assert quarantined.quarantined_task_ids == ("task-risk",)

    with pytest.raises(ToolBlocked, match="automatic retry is forbidden"):
        validate_navigator_task_binding(
            "scrapex_navigator",
            {"action": "click", "task_id": "task-risk", "ref": "e1"},
            quarantined,
            conversation_id=11,
            message_id=22,
        )
    validate_navigator_task_binding(
        "scrapex_navigator",
        {"action": "observe", "task_id": "task-safe"},
        quarantined,
        conversation_id=11,
        message_id=22,
    )


def test_task_binding_rejects_evidence_from_a_different_turn_or_a_guessed_id() -> None:
    evidence = navigator_evidence_from_result(
        "scrapex_navigator",
        {"action": "create_task", "provider": "alldata", "target": {}, "topic": "t"},
        _verified_navigator_result("create_task", {"id": "task-1"}),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="create-call",
    )
    assert evidence is not None

    with pytest.raises(ToolBlocked):
        validate_navigator_task_binding(
            "scrapex_navigator",
            {"action": "observe", "task_id": "task-1"},
            evidence,
            conversation_id=11,
            message_id=99,
        )
    with pytest.raises(ToolBlocked):
        validate_navigator_task_binding(
            "scrapex_navigator",
            {"action": "observe", "task_id": "task-guessed"},
            evidence,
            conversation_id=11,
            message_id=22,
        )
    with pytest.raises(ToolBlocked):
        validate_navigator_task_binding(
            "scrapex_navigator",
            {"action": "observe", "task_id": "task-1"},
            None,
            conversation_id=11,
            message_id=22,
        )
    # create_task itself is id-free and never gated.
    validate_navigator_task_binding(
        "scrapex_navigator",
        {"action": "create_task", "provider": "alldata", "target": {}, "topic": "t"},
        None,
        conversation_id=11,
        message_id=22,
    )
    # A different tool entirely is never gated by Navigator evidence.
    validate_navigator_task_binding(
        "scrapex_read", {"action": "list_batches"}, None,
        conversation_id=11, message_id=22,
    )


def test_sibling_overlay_applies_only_quarantine_and_never_new_task_ids() -> None:
    round_evidence = navigator_evidence_from_result(
        "scrapex_navigator",
        {"action": "create_task", "provider": "alldata", "target": {}, "topic": "t"},
        _verified_navigator_result("create_task", {"id": "task-1"}),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="create-call",
    )
    assert round_evidence is not None

    observed_evidence = navigator_evidence_from_result(
        "scrapex_navigator",
        {"action": "click", "task_id": "task-1", "ref": "e1"},
        {
            "service": "ScrapeX",
            "action": "click",
            "status": "indeterminate",
            "success": False,
            "executed": False,
            "verified": False,
            "may_have_executed": True,
            "indeterminate": True,
        },
        conversation_id=11,
        message_id=22,
        source_tool_call_id="ambiguous-click",
        previous=round_evidence,
    )
    assert observed_evidence is not None

    overlaid = navigator_apply_new_quarantine(round_evidence, observed_evidence)
    assert overlaid.task_ids == ()
    assert overlaid.quarantined_task_ids == ("task-1",)

    # A later sibling's freshly-created id must not leak into an earlier
    # round's evidence via the overlay -- only quarantine propagates, never
    # a new task id minted after the round was authored.
    sibling_created = navigator_evidence_from_result(
        "scrapex_navigator",
        {"action": "create_task", "provider": "alldata", "target": {}, "topic": "t2"},
        _verified_navigator_result("create_task", {"id": "task-2"}),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="sibling-create",
        previous=observed_evidence,
    )
    sibling_overlay = navigator_apply_new_quarantine(round_evidence, sibling_created)
    assert "task-2" not in sibling_overlay.task_ids
    assert "task-2" not in sibling_overlay.quarantined_task_ids


def test_evidence_never_exceeds_the_exact_model_visible_result() -> None:
    evidence = navigator_evidence_from_result(
        "scrapex_navigator",
        {"action": "create_task", "provider": "alldata", "target": {}, "topic": "t"},
        _verified_navigator_result("create_task", {"id": "task-1", "extra": "ignored-but-fine"}),
        conversation_id=11,
        message_id=22,
        source_tool_call_id="create-call",
    )
    assert evidence is not None
    assert evidence.task_ids == ("task-1",)
    assert isinstance(evidence, NavigatorTurnEvidence)
