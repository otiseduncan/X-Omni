"""
X Omni -- the turn loop.

One user message in, a stream of events out:
    {"type": "token",     "text": ...}
    {"type": "tool_start","name": ...,"args": ...}
    {"type": "tool_result","name": ...,"result": ...}
    {"type": "artifact",  "artifact": {...}}       inline chat card
    {"type": "approval",  "approval": {...}}       pauses, waits for operator
    {"type": "done",      "message_id": ...,"worker": ...}
    {"type": "error",     "message": ...}

Tool results become artifacts where a card exists for them, which is what
makes the UI chat-native: weather and calendar render inside the message
that produced them rather than in a separate panel.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Optional

from ..state.db import WebsiteRevisionConflict
from ..tools.registry import (
    CalibrationIQTurnEvidence,
    NeedsApproval,
    ScrapeXTurnEvidence,
    ToolBlocked,
    ToolError,
    calibration_iq_evidence_from_result,
    scrapex_apply_new_quarantine,
    scrapex_evidence_from_result,
)
from . import prompt as prompt_mod

log = logging.getLogger("xomni.loop")

MAX_TOOL_ROUNDS = 6
MAX_TOOL_CALLS_PER_ROUND = 8

FINAL_SYNTHESIS_MESSAGE = (
    "Internal final-synthesis boundary; this is not a new user request. The six "
    "tool-capable rounds for this turn are complete. Using only the original "
    "request and tool results already returned, provide one concise, truthful "
    "final answer now. No more tools are available: do not request another tool "
    "or imply that an unexecuted action ran. Preserve source, approval, receipt, "
    "and indeterminate-result boundaries."
)
TOOL_ROUND_CAP_FALLBACK = (
    "The six-round tool limit was reached. No additional tool was run, and I’m "
    "not making a claim beyond the tool results already returned."
)

NO_TOOL_SELF_CHECK_ACCEPT = "NO_TOOL_NEEDED"
NO_TOOL_SELF_CHECK_MESSAGE = """Internal final-answer evidence check; this is not a new user request. Review the withheld draft against the original request, current structured context, advertised tool contracts, and returned evidence. If a safe answer requires current or live business state, execution proof, capability state, or vehicle-specific OEM technical evidence, do not answer in prose: call the best justified advertised tool or tools now. If the draft is a casual or general answer, or already states a truthful unresolved boundary and no tool is needed, output exactly NO_TOOL_NEEDED. Never run a mutation to test or demonstrate capability. Reason from meaning and evidence contracts, not keyword rules."""
NO_TOOL_SELF_CHECK_REQUIRED_MESSAGE = """Internal active-context evidence check; this is not a new user request. A trusted active working subject exists, so do not accept or repeat the withheld draft and do not output NO_TOOL_NEEDED. Select the best justified advertised tool now to refresh or establish the authoritative evidence needed for the original request. The model owns which advertised capability fits; never mutate merely to test or demonstrate capability. Reason from the structured subject, full request, evidence contracts, and tool schemas, not keyword rules."""
NO_TOOL_SELF_CHECK_FALLBACK = (
    "I can’t verify the withheld draft from the available evidence, so I’m not "
    "presenting it as established."
)


@dataclass(frozen=True)
class NoToolSelfCheckResult:
    accept_draft: bool
    tool_calls: tuple[dict[str, Any], ...]
    checker_text: str


def no_tool_self_check_reserve_tokens(max_draft_tokens: int) -> int:
    """Worst-case extra input for the one bounded review request."""

    return (
        max(0, int(max_draft_tokens))
        + max(
            prompt_mod.estimate_tokens(NO_TOOL_SELF_CHECK_MESSAGE),
            prompt_mod.estimate_tokens(NO_TOOL_SELF_CHECK_REQUIRED_MESSAGE),
        )
        + 24
    )


async def model_owned_no_tool_self_check(
    client,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    draft: str,
    *,
    require_tool: bool = False,
) -> NoToolSelfCheckResult:
    """Let the same model accept its draft or select evidence tools once."""

    review_instruction = (
        NO_TOOL_SELF_CHECK_REQUIRED_MESSAGE
        if require_tool
        else NO_TOOL_SELF_CHECK_MESSAGE
    )
    review_messages = [
        *messages,
        {"role": "assistant", "content": draft},
        {"role": "user", "content": review_instruction},
    ]
    checker_text = ""
    tool_calls: list[dict[str, Any]] = []
    stream_kwargs: dict[str, Any] = {"tools": tools}
    if require_tool:
        stream_kwargs["tool_choice"] = "required"
    async for event in client.stream(review_messages, **stream_kwargs):
        if event.get("type") == "content":
            checker_text += str(event.get("text") or "")
        elif event.get("type") == "tool_call":
            tool_calls.append(event)
    return NoToolSelfCheckResult(
        accept_draft=(
            not require_tool and bool(draft.strip()) and not tool_calls
            and checker_text.strip() == NO_TOOL_SELF_CHECK_ACCEPT
        ),
        tool_calls=tuple(tool_calls),
        checker_text=checker_text,
    )

_WEB_ACCESS_DENIAL_RE = re.compile(
    r"\b(?:don't|do\s+not|doesn't|does\s+not|can't|cannot|unable\s+to)\b"
    r"[^.!?]{0,80}\b(?:access|browse|connect)\b[^.!?]{0,40}"
    r"\b(?:internet|web|external\s+websites?|online)\b"
    r"|\bno\s+(?:real[- ]?time\s+)?(?:internet|web)\s+access\b",
    re.IGNORECASE,
)
_WEB_RESEARCH_REFUSAL_RE = re.compile(
    r"^\s*(?:(?:i(?:'m|\s+am)\s+sorry|sorry)[,;:]?\s*)?(?:but\s+)?"
    r"(?:i\s+)?(?:cannot|can't|won't|am\s+unable\s+to)\s+"
    r"(?:provide|help|assist|comply|share|give|discuss|answer|support)\b",
    re.IGNORECASE,
)


def website_result_summary(result: Any, *, update: bool) -> str:
    payload = result if isinstance(result, dict) else {}
    title = str(payload.get("title") or "website").strip()[:120]
    if payload.get("ok") is not True:
        detail = str(
            payload.get("message")
            or (
                "The website preview could not be revised."
                if update
                else "The website preview could not be generated."
            )
        ).strip()[:500]
        action = "update" if update else "generate"
        return f"I couldn't {action} the website preview. {detail}"
    if update and payload.get("changed") is False:
        return (
            f"No new revision was needed for {title}; the current chat preview already "
            "matches that edit. No files were written or deployed."
        )
    if update:
        if "cards.translucent_glass" in (payload.get("changes") or []):
            return (
                f"Updated {title} in the existing chat preview. All cards now use a "
                "translucent glass effect. No files were written or deployed."
            )
        return f"Updated {title} in the existing chat preview. No files were written or deployed."
    return (
        f"Generated {title} as a buffered website preview in chat. No files were written "
        "or deployed."
    )


def web_research_result_is_verified(result: Any) -> bool:
    """Require a completed bounded external search before guarding synthesis."""
    payload = result if isinstance(result, dict) else {}
    sources = payload.get("sources")
    return bool(
        payload.get("ok") is True
        and payload.get("external_network") is True
        and payload.get("source_bounded") is True
        and isinstance(sources, list)
        and sources
    )


def web_research_fallback_summary(result: Any) -> str:
    """Replace a false capability denial after Core already ran live research."""
    payload = result if isinstance(result, dict) else {}
    sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    if sources:
        count = len(sources)
        noun = "source result" if count == 1 else "source results"
        return (
            f"I searched the live public web and found {count} {noun}. "
            "The source-linked research card contains the returned excerpts and links."
        )
    return (
        "I searched the live public web, but the configured providers returned no source "
        "results for that query. That is a search-result limitation, not a lack of web access."
    )


# Tool name -> card type the UI knows how to render.
ARTIFACT_FOR_TOOL = {
    "get_weather": "weather",
    "get_calendar": "calendar",
    "list_tasks": "tasks",
    "add_task": "task_added",
    "update_task_status": "task_updated",
    "system_status": "system_status",
    "list_directory": "directory",
    "search_files": "file_search",
    "web_research_current": "web_research",
    "website_preview_generate": "website_preview",
    "camera_request": "camera_request",
    "exterior_camera_request": "exterior_camera_request",
    "camera_event_history": "camera_event_history",
    "camera_snapshot_analyze": "camera_snapshot",
    "camera_footage": "camera_motion_clip",
    "camera_motion_clip": "camera_motion_clip",
    "image_generation_status": "image_generation_status",
    "image_generate": "generated_image",
    "video_generation_status": "video_generation_status",
    "video_generate": "generated_video",
    "assistant_capabilities_read": "capabilities",
    "read_file": "file",
    "run_powershell": "shell_result",
    "write_file": "file_written",
    "create_calendar_event": "calendar_event_created",
    # field systems
    "adas_si_search": "adas_si_results",
    "adas_si_open": "adas_si_document",
    "adas_si_inventory": "adas_si_inventory",
    "adas_si_records": "adas_si_records",
    "adas_si_file_write": "file_written",
    "adas_si_record_write": "adas_si_record",
    "adas_si_record_modify": "adas_si_record",
    "calibration_iq_read": "calibration_iq_ros",
    "calibration_iq_summary": "calibration_iq_summary",
    "calibration_iq_ro": "calibration_iq_ro",
    "calibration_iq_status": "calibration_iq_status",
    "calibration_iq_update": "calibration_iq_receipt",
    "calibration_iq_operator": "calibration_iq_receipt",
    "calibration_iq_destructive": "calibration_iq_receipt",
    # Persist and render the work-prep audit so a later-turn follow-up can use
    # the same structured queue state instead of reconstructing it from prose.
    "calibration_iq_work_prep": "calibration_iq_work_prep",
    "collision_research": "research_provider",
    "scrapex_status": "scrapex",
    "scrapex_read": "scrapex",
    "scrapex_adas_map": "scrapex",
    "automotive_knowledge_search": "automotive_knowledge",
    "automotive_knowledge_read": "automotive_knowledge",
    "automotive_knowledge_capture": "automotive_knowledge",
    "automotive_knowledge_lifecycle": "automotive_knowledge",
}

_CALIBRATION_IQ_OPERATOR_TOOLS = frozenset(
    {
        "calibration_iq_operator",
        "calibration_iq_destructive",
    }
)

_CALIBRATION_IQ_WORK_PREP_TOOL = "calibration_iq_work_prep"


def _calibration_iq_operator_payload(result: Any) -> dict[str, Any]:
    """Return the authoritative operator payload, including approval replays."""
    if not isinstance(result, dict):
        return {}
    nested = result.get("result")
    if (
        isinstance(nested, dict)
        and result.get("verified") is not True
        and result.get("success") is not True
        and (
            result.get("replayed") is True
            or isinstance(result.get("execution_receipt"), dict)
        )
    ):
        return nested
    return result


def calibration_iq_operator_result_is_verified(result: Any) -> bool:
    """Require the operator's complete, positive receipt-level success contract."""
    payload = _calibration_iq_operator_payload(result)
    receipts = [
        receipt
        for receipt in (payload.get("receipts") or [])
        if isinstance(receipt, dict)
    ]
    requested_count = _bounded_nonnegative_int(
        payload.get("requested_count"), len(receipts)
    )
    processed_count = _bounded_nonnegative_int(
        payload.get("processed_count"), len(receipts)
    )
    return bool(
        payload.get("status") == "success"
        and payload.get("success") is True
        and payload.get("verified") is True
        and payload.get("partial") is not True
        and receipts
        and requested_count == processed_count == len(receipts)
        and all(
            receipt.get("status") == "completed"
            and receipt.get("success") is True
            and isinstance(receipt.get("verification"), dict)
            and receipt["verification"].get("verified") is True
            for receipt in receipts
        )
    )


def _bounded_nonnegative_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def calibration_iq_operator_failure_summary(result: Any) -> str:
    """Return concise prose containing only structured Calibration IQ truth."""
    payload = _calibration_iq_operator_payload(result)
    raw_receipts = payload.get("receipts")
    receipts = (
        [receipt for receipt in raw_receipts if isinstance(receipt, dict)]
        if isinstance(raw_receipts, list)
        else []
    )
    requested_count = _bounded_nonnegative_int(
        payload.get("requested_count"), len(receipts)
    )
    processed_count = _bounded_nonnegative_int(
        payload.get("processed_count"), len(receipts)
    )
    verified_count = sum(
        1
        for receipt in receipts
        if (
            receipt.get("status") == "completed"
            and receipt.get("success") is True
            and isinstance(receipt.get("verification"), dict)
            and receipt["verification"].get("verified") is True
        )
    )

    error = payload.get("error")
    error = (
        error
        if isinstance(error, dict)
        else next(
            (
                receipt.get("error")
                for receipt in receipts
                if isinstance(receipt.get("error"), dict)
            ),
            {},
        )
    )
    error_code = str(error.get("code") or error.get("category") or "").strip()
    status = str(payload.get("status") or "unverified_result").strip()
    structured_name = error_code or status or "unverified_result"
    structured_name = re.sub(r"\s+", " ", structured_name)[:120]
    detail = str(error.get("message") or payload.get("message") or "").strip()
    detail = re.sub(r"\s+", " ", detail)[:400]

    partial = bool(payload.get("partial") is True or verified_count > 0)
    lead = (
        "Calibration IQ only partially verified the requested operation."
        if partial
        else "Calibration IQ did not verify the requested operation."
    )
    exact = f" Structured error: {structured_name}"
    if detail:
        exact += f" — {detail}"
    if not exact.endswith((".", "!", "?")):
        exact += "."
    total = max(requested_count, processed_count, len(receipts), verified_count)
    counts = (
        f"Verified actions: {verified_count} of {total}; "
        f"processed actions: {processed_count} of {total}."
        if total
        else "Verified actions: 0; processed actions: 0."
    )
    return f"{lead}{exact} {counts}"


def _calibration_iq_work_prep_reconciliation_counts(
    payload: dict[str, Any],
) -> tuple[int, int, int, int]:
    """Return requested/processed/returned/verified counts without guessing.

    Weekly/phase audits expose complete aggregate receipt counts even when the
    bounded row sample omits reconciliation details.  RO-scoped work prep keeps
    the same truth inside its single nested reconciliation result.
    """
    receipt_total = _bounded_nonnegative_int(payload.get("ciq_receipt_count"), 0)
    requested_total = _bounded_nonnegative_int(
        payload.get("ciq_mutations_requested_count"), receipt_total
    )
    processed_total = _bounded_nonnegative_int(
        payload.get("ciq_mutations_processed_count"), receipt_total
    )
    verified_total = _bounded_nonnegative_int(
        payload.get("ciq_verified_receipt_count"), 0
    )
    if any(
        field in payload
        for field in (
            "ciq_receipt_count",
            "ciq_mutations_requested_count",
            "ciq_mutations_processed_count",
            "ciq_verified_receipt_count",
        )
    ):
        return (
            requested_total,
            processed_total,
            receipt_total,
            min(verified_total, requested_total),
        )

    reconciliation = payload.get("reconciliation")
    if not isinstance(reconciliation, dict):
        return 0, 0, 0, 0
    receipts = [
        receipt
        for receipt in (reconciliation.get("receipts") or [])
        if isinstance(receipt, dict)
    ]
    requested_total = _bounded_nonnegative_int(
        reconciliation.get("requested_count"), len(receipts)
    )
    processed_total = _bounded_nonnegative_int(
        reconciliation.get("processed_count"), len(receipts)
    )
    explicit_verified = reconciliation.get("verified_count")
    if isinstance(explicit_verified, int) and not isinstance(explicit_verified, bool):
        verified_total = max(0, explicit_verified)
    else:
        verified_total = sum(
            1 for receipt in receipts if _calibration_iq_receipt_is_verified(receipt)
        )
    return (
        requested_total,
        processed_total,
        len(receipts),
        min(verified_total, requested_total),
    )


def calibration_iq_work_prep_terminal_summary(results: Any) -> str:
    """Seal work-prep turns with the service's structured audit truth.

    Work prep can perform receipt-bound CIQ reconciliation while also returning
    a truthful *partial* readiness audit.  The model still sees the structured
    result for tool choice, but it cannot replace the final aggregate counts,
    named exceptions, omissions, or receipt state with optimistic prose.
    """
    from ..services import calibration_iq_work_prep as work_prep

    raw_results = results if isinstance(results, list) else [results]
    summaries: list[str] = []
    for result in raw_results:
        if not isinstance(result, dict):
            summaries.append(
                "The Calibration IQ work-prep result was not verified. "
                "CIQ mutation receipts: 0 verified; 0 processed."
            )
            continue
        mode = _calibration_iq_summary_value(result.get("mode"), limit=80)
        if not mode:
            mode = "unknown"
        result_verified = bool(
            result.get("verified") is True
            or (mode == "phase_list" and result.get("status") == "verified")
        )
        if result.get("success") is False and result.get("message"):
            summary = _calibration_iq_summary_value(
                result.get("message"), limit=500
            )
        elif result_verified:
            summary = work_prep.summarize(mode, result)
        else:
            detail = _calibration_iq_summary_value(
                result.get("message"), limit=400
            )
            summary = (
                detail
                or "The requested Calibration IQ work-prep action was not verified."
            )

        requested, processed, receipt_total, verified = (
            _calibration_iq_work_prep_reconciliation_counts(result)
        )
        reconciliation_failed = _bounded_nonnegative_int(
            result.get("reconciliation_failed_count"), 0
        )
        indeterminate = _bounded_nonnegative_int(
            result.get("ciq_indeterminate_reconciliation_count"), 0
        )
        may_have_executed = _bounded_nonnegative_int(
            result.get("ciq_may_have_executed_reconciliation_count"), 0
        )
        reconciliation = result.get("reconciliation")
        reconciliation_attempted = bool(
            requested
            or processed
            or verified
            or receipt_total
            or reconciliation_failed
            or indeterminate
            or may_have_executed
            or result.get("executed") is True
            or (
                isinstance(reconciliation, dict)
                and (
                    reconciliation.get("executed") is True
                    or reconciliation.get("may_have_executed") is True
                    or _bounded_nonnegative_int(
                        reconciliation.get("requested_count"), 0
                    )
                    > 0
                )
            )
        )
        if reconciliation_attempted:
            unverified = max(requested, processed) - verified
            receipt_line = (
                "CIQ mutation receipts: "
                f"{verified} verified of {requested} requested; "
                f"{processed} processed; {receipt_total} returned; "
                f"{max(0, unverified)} unverified."
            )
            if reconciliation_failed:
                receipt_line += (
                    f" Reconciliation exceptions: {reconciliation_failed}."
                )
            if indeterminate or may_have_executed:
                receipt_line += (
                    " Indeterminate reconciliation outcomes: "
                    f"{indeterminate}; may-have-executed outcomes: "
                    f"{may_have_executed}."
                )
            summary = f"{summary}\n{receipt_line}"
        summaries.append(summary)
    return "\n\n".join(summaries)


def _calibration_iq_protected_terminal_summary(
    operator_results: list[dict[str, Any]],
    work_prep_results: list[dict[str, Any]],
) -> str:
    sections: list[str] = []
    if operator_results:
        sections.append(calibration_iq_operator_terminal_summary(operator_results))
    if work_prep_results:
        sections.append(calibration_iq_work_prep_terminal_summary(work_prep_results))
    return "\n\n".join(section for section in sections if section)


def _calibration_iq_receipt_is_verified(receipt: Any) -> bool:
    return bool(
        isinstance(receipt, dict)
        and receipt.get("status") == "completed"
        and receipt.get("success") is True
        and isinstance(receipt.get("verification"), dict)
        and receipt["verification"].get("verified") is True
    )


def _calibration_iq_summary_value(value: Any, *, limit: int = 240) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    cleaned = re.sub(r"\s+", " ", str(value)).strip()
    return cleaned[:limit]


def _calibration_iq_receipt_correction_key(receipt: dict[str, Any]) -> tuple[str, str]:
    return (
        _calibration_iq_summary_value(receipt.get("operation"), limit=120),
        _calibration_iq_summary_value(receipt.get("repair_order_id"), limit=160),
    )


def _calibration_iq_receipts_are_same_action(
    failed: dict[str, Any], verified: dict[str, Any]
) -> bool:
    failed_operation, failed_ro = _calibration_iq_receipt_correction_key(failed)
    verified_operation, verified_ro = _calibration_iq_receipt_correction_key(verified)
    if not failed_operation or failed_operation != verified_operation:
        return False
    strong_fields = ("mutation_id", "idempotency_key", "correlation_id")
    failed_identities = {
        (field, value)
        for field in strong_fields
        if (value := _calibration_iq_summary_value(failed.get(field), limit=200))
    }
    verified_identities = {
        (field, value)
        for field in strong_fields
        if (value := _calibration_iq_summary_value(verified.get(field), limit=200))
    }
    if failed_identities or verified_identities:
        return bool(failed_identities & verified_identities)
    # Never hide a failure based on the operation name alone: one turn may
    # apply the same operation to multiple ROs or resources.
    if failed_ro or verified_ro:
        return bool(failed_ro and verified_ro and failed_ro == verified_ro)
    for field in ("target_id", "resource_id"):
        failed_resource = _calibration_iq_summary_value(failed.get(field), limit=200)
        verified_resource = _calibration_iq_summary_value(
            verified.get(field), limit=200
        )
        if (
            failed_resource
            and verified_resource
            and failed_resource == verified_resource
        ):
            return True
    return False


def _calibration_iq_receipt_identity(receipt: dict[str, Any]) -> tuple[str, str] | None:
    for field in ("mutation_id", "idempotency_key"):
        value = _calibration_iq_summary_value(receipt.get(field), limit=200)
        if value:
            return field, value
    return None


def _calibration_iq_receipt_resource(receipt: dict[str, Any]) -> str:
    operation = _calibration_iq_summary_value(receipt.get("operation"), limit=120)
    operation = operation or "operation"
    resource_type = _calibration_iq_summary_value(
        receipt.get("resource_type"), limit=120
    )
    resource_id = _calibration_iq_summary_value(receipt.get("resource_id"), limit=200)
    path = ""
    for state_name in ("after", "before"):
        state = receipt.get(state_name)
        if not isinstance(state, dict):
            continue
        for field in ("path", "storage_relative_path", "case_folder_relative_path"):
            path = _calibration_iq_summary_value(state.get(field), limit=300)
            if path:
                break
        if path:
            break

    facts: list[str] = []
    if resource_type:
        facts.append(f"type={resource_type}")
    if resource_id:
        facts.append(f"id={resource_id}")
    if path:
        facts.append(f"path={path}")
    if not facts:
        facts.append("receipt verified; resource type/id/path not returned")
    return f"{operation} -> {', '.join(facts)}"


def _calibration_iq_snapshot_state(
    result_payloads: list[dict[str, Any]],
) -> str:
    latest: dict[str, dict[str, Any]] = {}
    for payload in result_payloads:
        snapshots = payload.get("final_snapshots")
        snapshots = snapshots if isinstance(snapshots, dict) else {}
        if not snapshots:
            # Without a fresh authoritative reread, a later transport,
            # indeterminate, or invalid-response result cannot inherit an
            # older snapshot and label it as the current post-call state.
            latest.clear()
        affected_ro_ids = {
            _calibration_iq_summary_value(receipt.get("repair_order_id"), limit=160)
            for receipt in (payload.get("receipts") or [])
            if isinstance(receipt, dict) and receipt.get("repair_order_id")
        }
        # A later operator call without a verified reread must not inherit an
        # older snapshot and present it as the current post-call state.
        for ro_id in affected_ro_ids:
            if ro_id and ro_id not in snapshots:
                latest.pop(ro_id, None)
        for raw_ro_id, envelope in snapshots.items():
            ro_id = _calibration_iq_summary_value(raw_ro_id, limit=160)
            if not ro_id:
                continue
            if not (
                isinstance(envelope, dict)
                and envelope.get("status") == "verified"
                and isinstance(envelope.get("snapshot"), dict)
            ):
                latest.pop(ro_id, None)
                continue
            latest[ro_id] = envelope["snapshot"]

    if not latest:
        return (
            "Current repair-order state: unavailable because no verified final "
            "snapshot was returned."
        )

    rendered: list[str] = []
    for ro_id, snapshot in latest.items():
        repair_order = snapshot.get("repair_order")
        repair_order = repair_order if isinstance(repair_order, dict) else snapshot
        workflow = snapshot.get("workflow")
        workflow = workflow if isinstance(workflow, dict) else {}

        number = ""
        for field in ("ro_number", "roNumber", "number", "ro", "RO"):
            number = _calibration_iq_summary_value(repair_order.get(field), limit=160)
            if number:
                break
        status = _calibration_iq_summary_value(
            workflow.get("status")
            or repair_order.get("status")
            or snapshot.get("workflow_status"),
            limit=120,
        )
        version_value = repair_order.get("version")
        if version_value is None:
            version_value = workflow.get("version")
        if version_value is None:
            version_value = snapshot.get("version")
        version = _calibration_iq_summary_value(version_value, limit=80)

        identifier = f"number={number}" if number else f"id={ro_id}; number unavailable"
        rendered.append(
            f"Current repair order: {identifier}; status={status or 'unavailable'}; "
            f"version={version or 'unavailable'}."
        )
    return " ".join(rendered)


def calibration_iq_operator_terminal_summary(results: Any) -> str:
    """Seal operator turns with only receipt and final-snapshot truth.

    Multiple tool calls can be required when a later action consumes an id from
    an earlier receipt. Verified receipts are accumulated. A failed receipt is
    treated as a stale retry only when a later verified receipt identifies the
    same operation and the same RO or target resource.
    """
    raw_results = results if isinstance(results, list) else [results]
    payloads = [
        _calibration_iq_operator_payload(result)
        for result in raw_results
        if isinstance(result, dict)
    ]
    payloads = [payload for payload in payloads if payload]
    if not payloads:
        return calibration_iq_operator_failure_summary({"status": "unverified_result"})

    attempts: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    requested_total = 0
    processed_total = 0
    for result_index, payload in enumerate(payloads):
        receipts = [
            receipt
            for receipt in (payload.get("receipts") or [])
            if isinstance(receipt, dict)
        ]
        requested_count = _bounded_nonnegative_int(
            payload.get("requested_count"), len(receipts)
        )
        processed_count = _bounded_nonnegative_int(
            payload.get("processed_count"), len(receipts)
        )
        requested_total += requested_count
        processed_total += processed_count
        attempt = {
            "requested_count": requested_count,
            "processed_count": processed_count,
            "complete": calibration_iq_operator_result_is_verified(payload),
            "entries": [],
        }
        attempts.append(attempt)
        for receipt in receipts:
            entry = {
                "result_index": result_index,
                "receipt": receipt,
                "verified": _calibration_iq_receipt_is_verified(receipt),
                "payload_complete": attempt["complete"],
                "suppressed": False,
                "suppression_reason": None,
            }
            entries.append(entry)
            attempt["entries"].append(entry)

    # Do not double-count an idempotent replay that returns the same mutation.
    seen_verified: set[tuple[str, str]] = set()
    for entry in entries:
        if not entry["verified"]:
            continue
        identity = _calibration_iq_receipt_identity(entry["receipt"])
        if identity is not None and identity in seen_verified:
            entry["suppressed"] = True
            entry["suppression_reason"] = "duplicate"
            requested_total = max(0, requested_total - 1)
            processed_total = max(0, processed_total - 1)
        elif identity is not None:
            seen_verified.add(identity)

    # A completely verified retry can replace at most one earlier failed
    # attempt. Prefer the closest matching failure so one success never hides
    # several same-operation errors.
    for candidate in entries:
        if (
            not candidate["verified"]
            or not candidate["payload_complete"]
            or candidate["suppressed"]
        ):
            continue
        matching_failures = [
            entry
            for entry in entries
            if entry["result_index"] < candidate["result_index"]
            and not entry["verified"]
            and not entry["suppressed"]
            and _calibration_iq_receipts_are_same_action(
                entry["receipt"], candidate["receipt"]
            )
        ]
        if matching_failures:
            corrected = matching_failures[-1]
            corrected["suppressed"] = True
            corrected["suppression_reason"] = "corrected_retry"
            requested_total = max(0, requested_total - 1)
            processed_total = max(0, processed_total - 1)

    verified_receipts = [
        entry["receipt"]
        for entry in entries
        if entry["verified"] and not entry["suppressed"]
    ]
    verified_count = len(verified_receipts)
    requested_total = max(requested_total, verified_count)
    processed_total = max(min(processed_total, requested_total), verified_count)
    outstanding = max(0, requested_total - verified_count)
    active_attempt_indexes = {
        index
        for index, attempt in enumerate(attempts)
        if (
            max(
                0,
                int(attempt["requested_count"])
                - sum(
                    1
                    for entry in attempt["entries"]
                    if entry["suppression_reason"] == "corrected_retry"
                ),
            )
            > 0
            or (not attempt["complete"] and int(attempt["requested_count"]) == 0)
        )
    }
    incomplete_outcome = any(
        not attempts[index]["complete"] for index in active_attempt_indexes
    )

    if verified_count and not outstanding and not incomplete_outcome:
        lead = (
            f"Calibration IQ verified {verified_count} of {requested_total} "
            "requested actions."
        )
    elif verified_count:
        lead = (
            "Calibration IQ only partially verified this operator turn. "
            f"Verified actions: {verified_count} of {requested_total}; processed "
            f"actions: {processed_total} of {requested_total}."
        )
    elif len(active_attempt_indexes) <= 1:
        latest_index = max(active_attempt_indexes) if active_attempt_indexes else -1
        latest = payloads[latest_index]
        # Preserve the established structured failure wording for a completely
        # unverified turn while still preventing any model-authored diagnosis.
        return calibration_iq_operator_failure_summary(latest)
    else:
        lead = (
            "Calibration IQ did not verify this operator turn. "
            f"Verified actions: 0 of {requested_total}; processed actions: "
            f"{processed_total} of {requested_total}."
        )

    operations = " ".join(
        f"{index}) {_calibration_iq_receipt_resource(receipt)};"
        for index, receipt in enumerate(verified_receipts, start=1)
    ).rstrip(";")
    operation_summary = f" Verified operations: {operations}." if operations else ""

    error_summary = ""
    if outstanding or incomplete_outcome:
        active_failures = [
            entry["receipt"]
            for entry in entries
            if not entry["verified"] and not entry["suppressed"]
        ]
        error = next(
            (
                receipt.get("error")
                for receipt in reversed(active_failures)
                if isinstance(receipt.get("error"), dict)
            ),
            None,
        )
        if not isinstance(error, dict):
            error = next(
                (
                    payload.get("error")
                    for index, payload in reversed(list(enumerate(payloads)))
                    if index in active_attempt_indexes
                    and not attempts[index]["complete"]
                    and isinstance(payload.get("error"), dict)
                ),
                None,
            )
        if isinstance(error, dict):
            code = _calibration_iq_summary_value(
                error.get("code") or error.get("category"), limit=120
            )
            detail = _calibration_iq_summary_value(error.get("message"), limit=400)
            if code or detail:
                error_summary = (
                    f" Latest structured error: {code or 'operation_failed'}"
                )
                if detail:
                    error_summary += f" — {detail}"
                error_summary += "."

    return (
        f"{lead}{operation_summary}{error_summary} "
        f"{_calibration_iq_snapshot_state(payloads)}"
    )


def calibration_iq_research_result_summary(result: Any) -> str:
    """Summarize only receipt-backed facts from a routed research operation."""
    payload = _calibration_iq_operator_payload(result)
    if not calibration_iq_operator_result_is_verified(payload):
        return calibration_iq_operator_failure_summary(payload)

    receipts = [
        receipt
        for receipt in (payload.get("receipts") or [])
        if isinstance(receipt, dict)
    ]
    verified_receipts = sum(
        1
        for receipt in receipts
        if (
            receipt.get("status") == "completed"
            and receipt.get("success") is True
            and isinstance(receipt.get("verification"), dict)
            and receipt["verification"].get("verified") is True
        )
    )
    reports = [
        report for report in (payload.get("research") or []) if isinstance(report, dict)
    ]
    requested_complete = bool(
        reports
        and all(report.get("research_complete_requested") is True for report in reports)
    )
    completion_verified = bool(
        requested_complete
        and all(report.get("research_complete_verified") is True for report in reports)
    )

    required: set[str] = set()
    evidence: set[str] = set()
    existing: set[str] = set()
    prepared: set[str] = set()
    for report in reports:
        requirements = report.get("final_required_calibrations") or report.get(
            "required_calibrations"
        )
        for item in requirements or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("id") or item.get("label") or "").strip()
            if key:
                required.add(key)
        for field, destination in (
            ("already_present", existing),
            ("documents_prepared", prepared),
        ):
            for item in report.get(field) or []:
                if not isinstance(item, dict):
                    continue
                key = str(
                    item.get("document_id")
                    or item.get("source_uri")
                    or item.get("relative_path")
                    or item.get("source")
                    or item.get("title")
                    or ""
                ).strip()
                if key:
                    destination.add(key)
                    evidence.add(key)

    if completion_verified:
        parts = ["Calibration IQ verified the RO's OEM research as complete."]
    else:
        parts = ["Calibration IQ verified the RO-scoped OEM research operation."]
    if evidence and required:
        parts.append(
            f"{len(evidence)} managed OEM document(s) cover "
            f"{len(required)} required calibration(s)."
        )
    elif evidence:
        parts.append(f"{len(evidence)} managed OEM document(s) were verified.")
    if existing and not prepared:
        parts.append(
            f"It reused {len(existing)} existing managed document(s) instead of "
            "importing duplicate copies."
        )
    receipt_detail = (
        "the receipt card contains the persisted source and page-citation details."
        if evidence
        else "the receipt card contains the exact findings and missing-documentation details."
    )
    parts.append(
        f"Verified receipts: {verified_receipts} of {len(receipts)}; {receipt_detail}"
    )
    return " ".join(parts)


def tool_result_for_model(name: str, result: Any) -> Any:
    """Project large display artifacts into a compact, valid model result.

    The full artifact still goes to the chat stream and durable message store.
    In particular, feeding an entire generated HTML document back through a
    12K string slice can create malformed JSON and wastes context the model
    needs for its final explanation.
    """
    if name != "website_preview_generate" or not isinstance(result, dict):
        return result
    projected = {
        key: result.get(key)
        for key in (
            "ok",
            "status",
            "website_id",
            "revision",
            "parent_sha256",
            "changed",
            "changes",
            "title",
            "bytes",
            "sha256",
            "written_to_disk",
            "deployed",
            "preview",
            "message",
        )
        if key in result
    }
    if result.get("ok") is True:
        projected["assistant_instruction"] = (
            "Respond with a short summary only. The inline card already contains the full "
            "website preview and HTML; do not repeat or regenerate the code."
        )
    else:
        projected["assistant_instruction"] = (
            "State that the website preview was not updated and that the previous successful "
            "revision remains unchanged. Do not claim success."
        )
    return projected


TOOL_RESULT_MODEL_CHAR_BUDGET = 12000


def _bounded_tool_result_json(
    value: Any, *, max_chars: int = TOOL_RESULT_MODEL_CHAR_BUDGET
) -> str:
    """Serialize a tool result for the model, preferring structural
    truncation over a raw byte-level cut.

    A flat ``json.dumps(value)[:max_chars]`` slice can amputate the JSON
    mid-object -- for a large result (e.g. a library inventory over a
    hundred-plus documents) this reliably lands inside a big list field and
    silently deletes whatever comes after it, including any trailing
    safety/evidence fields a tool author put there on purpose. Instead, when
    the encoded result is too large, repeatedly shrink whichever top-level
    list-valued field is currently largest -- keeping a leading slice of it
    plus an explicit omitted-count marker -- until it fits. Scalar and dict
    fields are never touched, so nothing that survives is ever more than
    "some list got shorter"; the model always receives valid, complete JSON.
    """
    encoded = json.dumps(value, default=str)
    if len(encoded) <= max_chars or not isinstance(value, dict):
        return encoded[:max_chars]

    trimmed = dict(value)
    list_keys = [k for k, v in trimmed.items() if isinstance(v, list)]
    while list_keys:
        encoded = json.dumps(trimmed, default=str)
        if len(encoded) <= max_chars:
            break
        biggest_key = max(
            list_keys,
            key=lambda k: len(json.dumps(trimmed.get(k), default=str)),
        )
        items = trimmed.get(biggest_key)
        if not isinstance(items, list) or not items:
            list_keys.remove(biggest_key)
            continue
        keep = len(items) - max(1, len(items) // 4)
        removed = len(items) - keep
        trimmed[biggest_key] = items[:keep]
        omitted_key = f"{biggest_key}_omitted_count"
        trimmed[omitted_key] = int(trimmed.get(omitted_key) or 0) + removed
        if keep == 0:
            list_keys.remove(biggest_key)
    # A pathological case (e.g. one huge scalar string, no list fields left
    # to shrink) falls back to the previous raw-cut behavior rather than
    # emitting an even-larger-than-budget payload.
    return encoded[:max_chars]


def tool_result_json_for_model(name: str, result: Any) -> str:
    """Serialize exactly the projected result delivered to the model."""

    return _bounded_tool_result_json(tool_result_for_model(name, result))


def tool_result_visible_to_model(name: str, result: Any) -> Any:
    """Return the structurally intact value the model can actually observe.

    A pathological oversized nested result can still hit the serializer's
    legacy raw-cut fallback. Such a fragment is not authoritative evidence:
    fail closed instead of binding opaque identifiers that were present only
    in the full handler/UI payload.
    """

    try:
        return json.loads(tool_result_json_for_model(name, result))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def artifact_type_for_tool(name: str, result: Any) -> Optional[str]:
    """Choose media success cards only when result truth is self-consistent."""
    if name == "camera_footage" and isinstance(result, dict) and "analysis_status" in result:
        return "camera_footage_analysis"
    if name not in {"image_generate", "video_generate"}:
        return ARTIFACT_FOR_TOOL.get(name)
    if name == "video_generate":
        if not isinstance(result, dict):
            return "video_generation_status"
        lifecycle = result.get("lifecycle") or {}
        digest = str(result.get("sha256") or "")
        source_digest = str(result.get("source_sha256") or "")
        expected_url = f"/api/generated-videos/{digest}.mp4"
        duration_seconds = result.get("duration_seconds")
        frame_count = result.get("frame_count")
        num_bytes = result.get("bytes")
        width = result.get("width")
        height = result.get("height")
        common_verified = (
            result.get("ok") is True
            and result.get("status") == "completed"
            and result.get("executed") is True
            and result.get("success") is True
            and result.get("actual_video") is True
            and result.get("verified") is True
            and result.get("source_verified") is True
            and result.get("mime_type") == "video/mp4"
            and result.get("codec") == "h264"
            and result.get("pixel_format") == "yuv420p"
            and type(result.get("fps")) is int
            and result.get("fps") == 24
            and type(duration_seconds) is int
            and 2 <= duration_seconds <= 10
            and type(frame_count) is int
            and frame_count == duration_seconds * 24
            and type(num_bytes) is int
            and num_bytes > 0
            and type(width) is int
            and 64 <= width <= 4096
            and width % 2 == 0
            and type(height) is int
            and 64 <= height <= 4096
            and height % 2 == 0
            and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
            and re.fullmatch(r"[0-9a-f]{64}", source_digest) is not None
            and result.get("video_url") == expected_url
            and result.get("target") == expected_url
        )
        mode = result.get("mode")
        procedural_verified = (
            mode == "exact_source_animation"
            and result.get("actual_generation") is False
            and result.get("source_preserved") is True
            and result.get("source_conditioned") is False
            and result.get("provider") == "ffmpeg-exact-local"
            and result.get("render_kind") == "deterministic_exact_source_animation"
            and result.get("profile") == "hover_pulse"
            and lifecycle.get("mode") == "bounded_cpu_subprocess"
            and lifecycle.get("model_remained_available") is True
        )
        seed = result.get("seed")
        expected_wan_assets = {
            "wan2.2_ti2v_5B_fp16.safetensors": (
                9999658848,
                "456f901338bd9eadbded3828b819109a9b68e8a525ca5cf8d0049a69fcfeca1e",
            ),
            "umt5_xxl_fp8_e4m3fn_scaled.safetensors": (
                6735906897,
                "c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68",
            ),
            "wan2.2_vae.safetensors": (
                1409400960,
                "e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156",
            ),
        }
        wan_assets = result.get("model_assets")
        wan_assets_verified = (
            isinstance(wan_assets, dict)
            and set(wan_assets) == set(expected_wan_assets)
            and all(
                isinstance(wan_assets.get(filename), dict)
                and wan_assets[filename].get("verified") is True
                and wan_assets[filename].get("bytes") == expected[0]
                and wan_assets[filename].get("sha256") == expected[1]
                for filename, expected in expected_wan_assets.items()
            )
        )
        generative_verified = (
            mode == "image_to_video"
            and result.get("actual_generation") is True
            and result.get("source_preserved") is False
            and result.get("source_conditioned") is True
            and result.get("provider") == "comfyui-wan2.2-ti2v-5b-local"
            and result.get("render_kind") == "generative_image_to_video"
            and result.get("model_id") == "Wan2.2-TI2V-5B"
            and result.get("width") == 704
            and result.get("height") == 704
            and type(seed) is int
            and 0 <= seed < 2**53
            and re.fullmatch(r"[0-9a-f]{64}", str(result.get("prompt_sha256") or ""))
            is not None
            and lifecycle.get("mode") == "sequential_exclusive"
            and lifecycle.get("model_stopped") is True
            and lifecycle.get("model_restored") is True
            and type(lifecycle.get("gpu_indices")) is list
            and bool(lifecycle.get("gpu_indices"))
            and wan_assets_verified
        )
        verified = common_verified and (procedural_verified or generative_verified)
        return "generated_video" if verified else "video_generation_status"
    if not isinstance(result, dict):
        return "image_generation_status"
    lifecycle = result.get("lifecycle") or {}
    verified = (
        result.get("ok") is True
        and result.get("status") == "completed"
        and result.get("actual_generation") is True
        and result.get("verified") is True
        and result.get("image_url") == result.get("target")
        and lifecycle.get("model_restored") is True
    )
    return "generated_image" if verified else "image_generation_status"


def video_failure_summary(result: Any) -> str:
    """Return fixed, receipt-grounded prose for a failed protected video run."""
    if not isinstance(result, dict):
        return "Video generation failed. No verified playable video is being claimed."

    generation = result.get("generation")
    lifecycle = result.get("lifecycle")
    generation = generation if isinstance(generation, dict) else {}
    lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
    submit_state = generation.get("submit_state")
    may_have_generated = generation.get("may_have_generated")

    if submit_state == "not_attempted" and may_have_generated is False:
        if (
            result.get("stage") == "model_stop_readiness"
            and result.get("retryable") is True
            and lifecycle.get("model_stop_attempted") is True
            and lifecycle.get("model_stopped") is False
        ):
            return (
                "Video generation did not start because Omni's readiness check could not "
                "be completed. No video job was submitted, and Omni was not stopped."
            )
        return (
            "Video generation did not start. No video job was submitted, and no video "
            "is being claimed."
        )

    if submit_state == "indeterminate" or may_have_generated is True:
        if result.get("actual_video") is True and result.get("verified") is True:
            return (
                "A video file was generated, but final lifecycle verification failed. "
                "The receipt preserves that partial result; no playable success card is "
                "being claimed."
            )
        return (
            "Video generation may have begun, but no verified playable result is being "
            "claimed. The receipt records the cleanup and restoration outcome."
        )

    return "Video generation failed. No verified playable video is being claimed."


def image_failure_summary(result: Any) -> str:
    """Return fixed, receipt-grounded prose for a failed protected image run."""
    if not isinstance(result, dict):
        return "Image generation failed. No verified generated image is being claimed."

    if result.get("actual_generation") is True and result.get("verified") is True:
        return (
            "An image file was generated and verified, but final lifecycle or receipt "
            "verification failed. The receipt preserves that partial result; no generated-"
            "image success card is being claimed."
        )
    return "Image generation failed. No verified generated image is being claimed."


class Orchestrator:
    def __init__(self, router, client, registry, store, settings):
        self.router = router
        self.client = client
        self.registry = registry
        self.store = store
        self.settings = settings

    def _persist_website_turn(
        self,
        conversation_id: int,
        summary: str,
        artifacts: list[dict],
        result: dict,
        *,
        update: bool,
    ) -> tuple[int, str, list[dict], dict]:
        final_result = result
        final_summary = summary
        final_artifacts = artifacts
        try:
            if (
                update
                and result.get("ok") is True
                and hasattr(self.store, "add_website_revision_message")
            ):
                message_id = self.store.add_website_revision_message(
                    conversation_id,
                    summary,
                    worker_used=self.router.active_name,
                    artifacts=artifacts,
                    website_id=str(result.get("website_id") or ""),
                    expected_parent_sha256=str(result.get("parent_sha256") or ""),
                )
            else:
                message_id = self.store.add_message(
                    conversation_id,
                    "assistant",
                    summary,
                    worker_used=self.router.active_name,
                    artifacts=artifacts,
                )
        except WebsiteRevisionConflict:
            final_result = {
                "ok": False,
                "status": "revision_conflict",
                "changed": False,
                "website_id": result.get("website_id"),
                "revision": result.get("revision"),
                "parent_sha256": result.get("parent_sha256"),
                "title": result.get("title"),
                "written_to_disk": False,
                "deployed": False,
                "message": (
                    "The website preview changed in another turn before this edit could be "
                    "saved. The newer revision remains unchanged."
                ),
            }
            final_summary = website_result_summary(final_result, update=True)
            replacement = {"type": "website_preview", "data": final_result}
            final_artifacts = list(artifacts)
            for index in range(len(final_artifacts) - 1, -1, -1):
                if final_artifacts[index].get("type") == "website_preview":
                    final_artifacts[index] = replacement
                    break
            else:
                final_artifacts.append(replacement)
            message_id = self.store.add_message(
                conversation_id,
                "assistant",
                final_summary,
                worker_used=self.router.active_name,
                artifacts=final_artifacts,
            )
            if hasattr(self.store, "audit"):
                self.store.audit(
                    "website_revision_conflict",
                    {
                        "conversation_id": conversation_id,
                        "website_id": result.get("website_id"),
                        "expected_parent_sha256": result.get("parent_sha256"),
                    },
                )
        return message_id, final_summary, final_artifacts, final_result

    @staticmethod
    def _rewrite_website_events(events: list[dict], result: dict) -> list[dict]:
        rewritten: list[dict] = []
        for event in events:
            current = dict(event)
            if current.get("type") == "tool_result":
                current["result"] = tool_result_for_model(
                    "website_preview_generate", result
                )
            elif (
                current.get("type") == "artifact"
                and (current.get("artifact") or {}).get("type") == "website_preview"
            ):
                current["artifact"] = {"type": "website_preview", "data": result}
            rewritten.append(current)
        return rewritten

    async def run_turn(
        self,
        conversation_id: int,
        user_message: str,
        approved_tool: Optional[dict] = None,
        approval_context: Optional[dict] = None,
    ) -> AsyncIterator[dict]:
        """Stream one assistant turn.

        `approved_tool` contains an already executed, receipt-backed result.
        Completed media-generation actions are terminal; other protected-tool
        results may be fed to the model, but the handler is never re-invoked.
        """
        try:
            async for event in self._run(
                conversation_id, user_message, approved_tool, approval_context
            ):
                yield event
        except Exception as exc:  # noqa: BLE001
            log.exception("turn failed")
            yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}

    async def _run(
        self,
        conversation_id: int,
        user_message: str,
        approved_tool: Optional[dict],
        approval_context: Optional[dict],
    ) -> AsyncIterator[dict]:
        history = self.store.get_messages(conversation_id)
        effective_context = dict(approval_context or {})
        if not approved_tool:
            # The user message is persisted before run_turn starts. Bind every
            # model-selected call to that authoritative message/conversation
            # identity even when no approval payload was supplied. A session
            # id remains necessary only if Registry asks Core to create a
            # confirmation card.
            current_user_message = next(
                (
                    item
                    for item in reversed(history)
                    if isinstance(item, dict) and item.get("role") == "user"
                ),
                None,
            )
            if isinstance(current_user_message, dict):
                message_id = current_user_message.get("id") or current_user_message.get(
                    "message_id"
                )
                if isinstance(message_id, int) and not isinstance(message_id, bool):
                    effective_context["message_id"] = message_id

            conversation_user_id = None
            if hasattr(self.store, "conversation_user_id"):
                conversation_user_id = self.store.conversation_user_id(conversation_id)
            if conversation_user_id:
                effective_context["user_id"] = str(conversation_user_id)
                if hasattr(self.store, "get_user"):
                    persisted_user = self.store.get_user(str(conversation_user_id))
                    if isinstance(persisted_user, dict) and persisted_user.get("role"):
                        effective_context["role"] = str(persisted_user["role"])

        effective_context.setdefault("role", "owner")
        active_subject = None
        if hasattr(self.store, "get_conversation_subject"):
            try:
                active_subject = self.store.get_conversation_subject(
                    conversation_id,
                    user_id=effective_context.get("user_id"),
                )
            except Exception:  # noqa: BLE001 - context continuity must not break a turn
                log.exception(
                    "Could not load active subject for conversation %s",
                    conversation_id,
                )
        role = str(effective_context["role"])
        calibration_iq_evidence: Optional[CalibrationIQTurnEvidence] = None
        scrapex_evidence: Optional[ScrapeXTurnEvidence] = None
        no_tool_self_check_enabled = bool(
            not approved_tool
            and getattr(self.client, "supports_no_tool_self_check", False)
        )
        no_tool_self_check_requires_tool = active_subject is not None
        no_tool_self_check_reserve = (
            no_tool_self_check_reserve_tokens(self.settings.max_response_tokens)
            if no_tool_self_check_enabled
            else 0
        )
        # An approval continuation reports the already executed protected call;
        # it cannot mint a fresh staged-write unlock and chain another mutation.
        calibration_iq_staging_enabled = not bool(approved_tool)
        try:
            # Reserve the largest catalog this turn may expose. The first model
            # round receives the staged catalog, while a verified exact-RO read
            # can unlock CIQ writes for a later round without overrunning context.
            reserve_tools = self.registry.model_tools(
                role,
                gate_calibration_iq_writes=False,
                gate_scrapex_batch_ids=False,
            )
            tools = self.registry.model_tools(
                role,
                calibration_iq_evidence=calibration_iq_evidence,
                scrapex_evidence=scrapex_evidence,
            )
        except TypeError:
            # Lightweight test registries expose the original zero-argument
            # shape; the production Registry always accepts the role.
            reserve_tools = tools = self.registry.model_tools()
        messages = prompt_mod.build_messages(
            self.router,
            history,
            self.settings.context_tokens,
            self.settings.max_response_tokens,
            active_subject=active_subject,
            tools=reserve_tools,
            extra_input_reserve_tokens=no_tool_self_check_reserve,
        )
        artifacts: list[dict] = []
        full_text = ""
        last_calibration_iq_operator_result: Optional[dict[str, Any]] = None
        calibration_iq_operator_results: list[dict[str, Any]] = []
        calibration_iq_work_prep_results: list[dict[str, Any]] = []
        calibration_iq_truth_emitted = False
        # Tracks the most recent web_research_current result across rounds
        # regardless of whether the call was model-chosen or (formerly)
        # deterministically routed, so the false-capability-denial guard
        # below still works now that routing is the model's own choice.
        last_web_research_result: Optional[dict[str, Any]] = None

        # The approval resolver already executed exactly once and persisted a
        # terminal receipt. Reconstruct the protocol pair without calling the
        # handler again, then let the model report the verified result.
        if approved_tool:
            name = approved_tool["name"]
            raw_approved_args = approved_tool.get("args") or {}
            args = (
                self.registry.log_args(name, raw_approved_args)
                if hasattr(self.registry, "log_args")
                else raw_approved_args
            )
            result = approved_tool.get("result")
            receipt = approved_tool.get("receipt") or {}
            if name in _CALIBRATION_IQ_OPERATOR_TOOLS:
                operator_payload = (
                    result
                    if isinstance(result, dict)
                    else {"status": "unverified_result"}
                )
                if calibration_iq_operator_result_is_verified(
                    operator_payload
                ) and not (
                    receipt.get("tool_name") == name
                    and receipt.get("status") == "succeeded"
                    and receipt.get("executed") is True
                    and receipt.get("success") is True
                    and receipt.get("result") == result
                ):
                    operator_payload = {
                        "status": "failed",
                        "executed": False,
                        "success": False,
                        "verified": False,
                        "partial": False,
                        "requested_count": _bounded_nonnegative_int(
                            result.get("requested_count"), 1
                        ),
                        "processed_count": 0,
                        "receipts": [],
                        "final_snapshots": {},
                        "error": {
                            "code": "approval_receipt_mismatch",
                            "message": (
                                "The approved result did not match a successful "
                                "terminal execution receipt."
                            ),
                        },
                    }
                    result = operator_payload
                last_calibration_iq_operator_result = operator_payload
                calibration_iq_operator_results.append(
                    last_calibration_iq_operator_result
                )
            call_id = approved_tool.get("call_id") or "approved_call"
            self._track_active_subject(
                conversation_id=conversation_id,
                name=name,
                result=result,
                call_id=call_id,
                invocation_context=effective_context,
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": _bounded_tool_result_json(
                        {"result": result, "execution_receipt": receipt}
                    ),
                }
            )
            approved_events = [
                {
                    "type": "tool_result",
                    "name": name,
                    "result": result,
                    "receipt": receipt,
                }
            ]
            receipt_artifact = {"type": "execution_receipt", "data": receipt}
            artifacts.append(receipt_artifact)
            approved_events.append({"type": "artifact", "artifact": receipt_artifact})
            card_type = artifact_type_for_tool(name, result)
            verified_image = (
                name == "image_generate"
                and card_type == "generated_image"
                and isinstance(result, dict)
                and receipt.get("tool_name") == "image_generate"
                and receipt.get("status") == "succeeded"
                and receipt.get("executed") is True
                and receipt.get("success") is True
                and receipt.get("result") == result
            )
            # A result-shaped success without a matching successful receipt is
            # not a generated-image card. Keep it visible only as failed status.
            if name == "image_generate" and not verified_image:
                card_type = "image_generation_status"
            if card_type and isinstance(result, dict):
                artifact = {"type": card_type, "data": result}
                artifacts.append(artifact)
                approved_events.append({"type": "artifact", "artifact": artifact})

            # Approval-gated media completion seals the turn. In particular,
            # image completion must never return to Qwen where the verified
            # image could be treated as permission to launch image-to-video.
            if name == "image_generate":
                summary = (
                    "The verified generated image is ready in the chat card."
                    if verified_image
                    else image_failure_summary(result)
                )
                message_id = self.store.add_message(
                    conversation_id,
                    "assistant",
                    summary,
                    worker_used=self.router.active_name,
                    artifacts=artifacts,
                )
                if len(history) <= 1:
                    self.store.touch_conversation(
                        conversation_id, title=user_message[:60]
                    )
                for approved_event in approved_events:
                    yield approved_event
                yield {"type": "token", "text": summary}
                yield {
                    "type": "done",
                    "message_id": message_id,
                    "worker": self.router.active_name,
                    "artifacts": artifacts,
                }
                return

            # A verified protected video result is already the full answer.
            # Persist the receipt/card before exposing either, then emit fixed
            # prose so the model cannot invent an absolute media URL or recast
            # one video mode as the other.
            verified_video = (
                name == "video_generate"
                and card_type == "generated_video"
                and isinstance(result, dict)
                and receipt.get("tool_name") == "video_generate"
                and receipt.get("status") == "succeeded"
                and receipt.get("executed") is True
                and receipt.get("success") is True
                and receipt.get("result") == result
            )
            if verified_video:
                video_mode = result.get("mode") if isinstance(result, dict) else None
                summary = (
                    "The verified generative image-to-video clip is ready in the chat card."
                    if video_mode == "image_to_video"
                    else "The verified procedural source animation is ready in the chat card."
                )
                message_id = self.store.add_message(
                    conversation_id,
                    "assistant",
                    summary,
                    worker_used=self.router.active_name,
                    artifacts=artifacts,
                )
                if len(history) <= 1:
                    self.store.touch_conversation(
                        conversation_id, title=user_message[:60]
                    )
                for approved_event in approved_events:
                    yield approved_event
                yield {"type": "token", "text": summary}
                yield {
                    "type": "done",
                    "message_id": message_id,
                    "worker": self.router.active_name,
                    "artifacts": artifacts,
                }
                return

            # A failed protected video result is also the full answer. Persist
            # its receipt/status card before emitting anything and use fixed
            # receipt-grounded prose. A second model call can otherwise invent
            # success or produce a misleading transport-error toast after the
            # authoritative failure has already been recorded.
            failed_video = (
                name == "video_generate"
                and card_type == "video_generation_status"
                and isinstance(result, dict)
                and receipt.get("tool_name") == "video_generate"
                and receipt.get("status") == "failed"
                and receipt.get("success") is False
                and receipt.get("result") == result
            )
            if failed_video:
                summary = video_failure_summary(result)
                message_id = self.store.add_message(
                    conversation_id,
                    "assistant",
                    summary,
                    worker_used=self.router.active_name,
                    artifacts=artifacts,
                )
                if len(history) <= 1:
                    self.store.touch_conversation(
                        conversation_id, title=user_message[:60]
                    )
                for approved_event in approved_events:
                    yield approved_event
                yield {"type": "token", "text": summary}
                yield {
                    "type": "done",
                    "message_id": message_id,
                    "worker": self.router.active_name,
                    "artifacts": artifacts,
                }
                return

            for approved_event in approved_events:
                yield approved_event

        paused = False
        # Same-turn fingerprint cache for read_only tool calls: a model can
        # (and, observed live, did) request the identical read-only tool with
        # identical arguments several times in one turn. Only the first
        # invocation touches the handler; later identical requests reuse that
        # result instead of re-running it and re-rendering its card.
        read_only_call_cache: dict[tuple[str, str], Any] = {}
        for round_index in range(MAX_TOOL_ROUNDS + 1):
            synthesis_only = round_index == MAX_TOOL_ROUNDS
            if synthesis_only:
                # Six tool-bearing model rounds are allowed. The extra model
                # call exists only so the sixth result can become a truthful
                # user-facing answer; no schema is advertised and no seventh
                # tool request can reach the gateway.
                log.warning("Tool loop hit the %d-round cap", MAX_TOOL_ROUNDS)
                round_tools: list[dict] = []
                round_messages = [
                    *messages,
                    {"role": "user", "content": FINAL_SYNTHESIS_MESSAGE},
                ]
            else:
                try:
                    tools = self.registry.model_tools(
                        role,
                        calibration_iq_evidence=calibration_iq_evidence,
                        scrapex_evidence=scrapex_evidence,
                    )
                except TypeError:
                    # Backward-compatible lightweight registries retain their
                    # original fixed catalog.
                    pass
                round_tools = tools
                round_messages = messages
            # Evidence returned during this round cannot authorize another
            # call authored in the same model batch. It becomes usable only on
            # the next model round, after the result is visible to the model.
            round_calibration_iq_evidence = calibration_iq_evidence
            call_calibration_iq_evidence = round_calibration_iq_evidence
            next_calibration_iq_evidence = calibration_iq_evidence
            round_scrapex_evidence = scrapex_evidence
            call_scrapex_evidence = round_scrapex_evidence
            next_scrapex_evidence = scrapex_evidence
            tool_calls: list[dict] = []
            round_text = ""
            sealed_round_tokens: list[dict] = []

            async for event in self.client.stream(round_messages, tools=round_tools):
                if event["type"] == "content":
                    round_text += event["text"]
                    # A model can emit optimistic prose before a tool call in
                    # the same streamed round. Hold round text until the tool
                    # choice is known so a late website call cannot expose a
                    # success claim before its artifact is durably committed.
                    sealed_round_tokens.append({"type": "token", "text": event["text"]})
                elif event["type"] == "tool_call":
                    tool_calls.append(event)

            if len(tool_calls) > MAX_TOOL_CALLS_PER_ROUND:
                log.warning(
                    "Model requested %d tools in one round; executing the first %d",
                    len(tool_calls),
                    MAX_TOOL_CALLS_PER_ROUND,
                )
                tool_calls = tool_calls[:MAX_TOOL_CALLS_PER_ROUND]

            synthesis_boundary_failed = bool(
                synthesis_only and (tool_calls or not round_text.strip())
            )
            if synthesis_boundary_failed:
                if tool_calls:
                    log.warning(
                        "Model requested %d tool call(s) during synthesis-only round; "
                        "none were executed",
                        len(tool_calls),
                    )
                # Drop both the impossible call and any provisional narration
                # emitted beside it. Protected result guards below still get
                # first refusal; ordinary turns receive the bounded boundary.
                tool_calls = []
                round_text = ""
                sealed_round_tokens = []

            if (
                no_tool_self_check_enabled
                and round_index == 0
                and not tool_calls
            ):
                self_check = await model_owned_no_tool_self_check(
                    self.client,
                    messages,
                    tools,
                    round_text,
                    require_tool=no_tool_self_check_requires_tool,
                )
                if self_check.tool_calls:
                    # The first draft and internal review prompt are temporary
                    # review context. Keep only the model-owned tool decision in
                    # the normal protocol history; neither checker prose nor the
                    # unsupported draft reaches the user or persisted chat.
                    tool_calls = list(self_check.tool_calls)[:MAX_TOOL_CALLS_PER_ROUND]
                    round_text = ""
                    sealed_round_tokens = []
                elif self_check.accept_draft:
                    for token_event in sealed_round_tokens:
                        yield token_event
                    full_text += round_text
                    break
                else:
                    yield {"type": "token", "text": NO_TOOL_SELF_CHECK_FALLBACK}
                    full_text += NO_TOOL_SELF_CHECK_FALLBACK
                    break

            operator_turn_active = bool(calibration_iq_operator_results)
            work_prep_turn_active = bool(calibration_iq_work_prep_results)
            calibration_iq_protected_turn_active = bool(
                operator_turn_active or work_prep_turn_active
            )
            guarded_calibration_iq_response = bool(
                calibration_iq_protected_turn_active and not tool_calls
            )
            guarded_web_response = bool(
                last_web_research_result is not None
                and not tool_calls
                and (
                    _WEB_ACCESS_DENIAL_RE.search(round_text)
                    or (
                        web_research_result_is_verified(last_web_research_result)
                        and (
                            not round_text.strip()
                            or _WEB_RESEARCH_REFUSAL_RE.search(round_text)
                        )
                    )
                )
            )
            # A round that produced a tool call may also have carried
            # speculative prose ahead of it -- local models routinely narrate
            # before deciding to call a tool, unlike tightly RLHF'd hosted
            # models. That prose is provisional by construction: the model
            # itself decided it needed more evidence in the same breath, so
            # it must never reach the user or the persisted transcript as if
            # it were a settled answer (a premature "I don't have access to
            # X" is the observed failure mode). This subsumes the narrower
            # website/operator-only checks this replaced -- any tool call
            # this round, not just those two families, now seals its round.
            if (
                not tool_calls
                and not calibration_iq_protected_turn_active
                and not guarded_web_response
            ):
                if synthesis_boundary_failed:
                    yield {"type": "token", "text": TOOL_ROUND_CAP_FALLBACK}
                else:
                    for token_event in sealed_round_tokens:
                        yield token_event

            if guarded_calibration_iq_response:
                guarded_text = _calibration_iq_protected_terminal_summary(
                    calibration_iq_operator_results,
                    calibration_iq_work_prep_results,
                )
                yield {"type": "token", "text": guarded_text}
                full_text = guarded_text
                calibration_iq_truth_emitted = True
            elif guarded_web_response:
                guarded_text = web_research_fallback_summary(last_web_research_result)
                yield {"type": "token", "text": guarded_text}
                full_text += guarded_text
            elif not tool_calls and not calibration_iq_protected_turn_active:
                full_text += (
                    TOOL_ROUND_CAP_FALLBACK
                    if synthesis_boundary_failed
                    else round_text
                )

            if not tool_calls:
                break

            # Record the assistant's tool-call turn so the model sees its own
            # request alongside the result on the next pass.
            messages.append(
                {
                    "role": "assistant",
                    "content": round_text or "",
                    "tool_calls": [
                        {
                            "id": c.get("id") or f"call_{round_index}_{i}",
                            "type": "function",
                            "function": {
                                "name": c["name"],
                                "arguments": c["arguments"],
                            },
                        }
                        for i, c in enumerate(tool_calls)
                    ],
                }
            )

            paused = False
            for i, call in enumerate(tool_calls):
                try:
                    args = json.loads(call["arguments"] or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}

                call_id = call.get("id") or f"call_{round_index}_{i}"
                is_website_call = call.get("name") == "website_preview_generate"
                calibration_iq_evidence_for_call = call_calibration_iq_evidence
                if call.get("name") in _CALIBRATION_IQ_OPERATOR_TOOLS:
                    # One staged write attempt consumes the unlock. A later
                    # mutation must refresh exact state again, while backend
                    # optimistic-concurrency checks remain authoritative.
                    call_calibration_iq_evidence = None
                    next_calibration_iq_evidence = None
                sealed_events: list[dict] = []
                website_result = None
                async for ev in self._execute(
                    call["name"],
                    args,
                    messages,
                    artifacts,
                    conversation_id=conversation_id,
                    approval_context=effective_context,
                    call_id=call_id,
                    call_cache=read_only_call_cache,
                    calibration_iq_evidence=calibration_iq_evidence_for_call,
                    scrapex_evidence=call_scrapex_evidence,
                ):
                    if ev["type"] == "approval":
                        paused = True
                    if (
                        calibration_iq_staging_enabled
                        and call.get("name") == "calibration_iq_ro"
                        and ev.get("type") == "tool_result"
                    ):
                        next_calibration_iq_evidence = calibration_iq_evidence_from_result(
                            "calibration_iq_ro",
                            ev.get("result"),
                            conversation_id=conversation_id,
                            message_id=int(effective_context.get("message_id") or 0),
                            source_tool_call_id=call_id,
                            previous=next_calibration_iq_evidence,
                        )
                    if (
                        call.get("name") in {"scrapex_read", "scrapex_adas_map"}
                        and ev.get("type") == "tool_result"
                    ):
                        visible_result = tool_result_visible_to_model(
                            call["name"], ev.get("result")
                        )
                        previous_next_scrapex_evidence = next_scrapex_evidence
                        visible_scrapex_evidence = scrapex_evidence_from_result(
                            call["name"],
                            args,
                            visible_result,
                            conversation_id=conversation_id,
                            message_id=int(effective_context.get("message_id") or 0),
                            source_tool_call_id=call_id,
                            previous=previous_next_scrapex_evidence,
                        )
                        # Opaque identities can be minted only from the exact
                        # structurally intact result shown to the model. A
                        # no-retry quarantine is a safety revocation, so apply
                        # it from the trusted full handler result even when a
                        # pathological oversized detail could not be projected.
                        trusted_scrapex_evidence = scrapex_evidence_from_result(
                            call["name"],
                            args,
                            ev.get("result"),
                            conversation_id=conversation_id,
                            message_id=int(effective_context.get("message_id") or 0),
                            source_tool_call_id=call_id,
                            previous=previous_next_scrapex_evidence,
                        )
                        next_scrapex_evidence = scrapex_apply_new_quarantine(
                            visible_scrapex_evidence,
                            trusted_scrapex_evidence,
                        )
                        call_scrapex_evidence = scrapex_apply_new_quarantine(
                            call_scrapex_evidence,
                            next_scrapex_evidence,
                        )
                    if (
                        call.get("name") in _CALIBRATION_IQ_OPERATOR_TOOLS
                        and ev.get("type") == "tool_result"
                    ):
                        operator_result = ev.get("result")
                        last_calibration_iq_operator_result = (
                            operator_result
                            if isinstance(operator_result, dict)
                            else {"status": "unverified_result"}
                        )
                        calibration_iq_operator_results.append(
                            last_calibration_iq_operator_result
                        )
                    if (
                        call.get("name") == _CALIBRATION_IQ_WORK_PREP_TOOL
                        and ev.get("type") == "tool_result"
                    ):
                        work_prep_result = ev.get("result")
                        calibration_iq_work_prep_results.append(
                            work_prep_result
                            if isinstance(work_prep_result, dict)
                            else {
                                "mode": args.get("mode"),
                                "status": "unverified_result",
                                "success": False,
                                "verified": False,
                            }
                        )
                    if (
                        call.get("name") == "web_research_current"
                        and ev.get("type") == "tool_result"
                    ):
                        web_result = ev.get("result")
                        last_web_research_result = (
                            web_result if isinstance(web_result, dict) else {"ok": False}
                        )
                    if is_website_call:
                        sealed_events.append(ev)
                        if ev.get("type") == "tool_result":
                            website_result = ev.get("result")
                        if (
                            ev.get("type") == "artifact"
                            and (ev.get("artifact") or {}).get("type")
                            == "website_preview"
                        ):
                            website_result = (ev.get("artifact") or {}).get("data")
                    else:
                        yield ev

                if is_website_call:
                    result = website_result if isinstance(website_result, dict) else {}
                    is_update = args.get("operation") == "update_latest" or result.get(
                        "status"
                    ) in {"updated_preview", "unchanged_preview", "update_failed"}
                    summary = website_result_summary(result, update=is_update)
                    message_id, summary, artifacts, result = self._persist_website_turn(
                        conversation_id,
                        summary,
                        artifacts,
                        result,
                        update=is_update,
                    )
                    sealed_events = self._rewrite_website_events(sealed_events, result)
                    full_text = summary
                    if len(history) <= 1 and summary:
                        self.store.touch_conversation(
                            conversation_id, title=user_message[:60]
                        )
                    for sealed_event in sealed_events:
                        yield sealed_event
                    yield {"type": "token", "text": summary}
                    yield {
                        "type": "done",
                        "message_id": message_id,
                        "worker": self.router.active_name,
                        "artifacts": artifacts,
                    }
                    return

                if paused:
                    # An approval is a hard turn boundary. Do not execute any
                    # later call the model happened to emit in the same batch;
                    # those calls were selected without the user's decision
                    # and may themselves mutate state.
                    break

            calibration_iq_evidence = next_calibration_iq_evidence
            scrapex_evidence = next_scrapex_evidence
            if paused:
                # Stop here. The UI shows the approval card; approving it
                # starts a new turn carrying approved_tool.
                break
        if (
            paused
            and not calibration_iq_truth_emitted
            and (
                calibration_iq_operator_results
                or calibration_iq_work_prep_results
            )
        ):
            # A routine operator action may complete before a later destructive
            # action pauses the turn for approval. Preserve the completed
            # receipt truth in this turn instead of saving a blank assistant
            # message; the approval continuation owns only the protected call.
            guarded_text = _calibration_iq_protected_terminal_summary(
                calibration_iq_operator_results,
                calibration_iq_work_prep_results,
            )
            yield {"type": "token", "text": guarded_text}
            full_text = guarded_text
            calibration_iq_truth_emitted = True

        if (
            not paused
            and not calibration_iq_truth_emitted
            and (
                calibration_iq_operator_results
                or calibration_iq_work_prep_results
            )
        ):
            guarded_text = _calibration_iq_protected_terminal_summary(
                calibration_iq_operator_results,
                calibration_iq_work_prep_results,
            )
            yield {"type": "token", "text": guarded_text}
            full_text = guarded_text

        message_id = self.store.add_message(
            conversation_id,
            "assistant",
            full_text,
            worker_used=self.router.active_name,
            artifacts=artifacts,
        )
        if len(history) <= 1 and full_text:
            self.store.touch_conversation(conversation_id, title=user_message[:60])

        yield {
            "type": "done",
            "message_id": message_id,
            "worker": self.router.active_name,
            "artifacts": artifacts,
        }

    async def _execute(
        self,
        name: str,
        args: dict,
        messages: list[dict],
        artifacts: list[dict],
        *,
        conversation_id: int,
        approval_context: Optional[dict],
        call_id: str = "call_0",
        call_cache: Optional[dict[tuple[str, str], Any]] = None,
        calibration_iq_evidence: Optional[CalibrationIQTurnEvidence] = None,
        scrapex_evidence: Optional[ScrapeXTurnEvidence] = None,
    ) -> AsyncIterator[dict]:
        yield {"type": "tool_start", "name": name, "args": args}

        def feed(payload: Any) -> None:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": tool_result_json_for_model(name, payload),
                }
            )

        # research_ro is a search-and-import composite, not a plain mutation:
        # calling it twice for the same RO re-runs the same ADAS SI search
        # and reports the same result (import_document is already
        # idempotent on its own). It sits at operator_authorized tier, not
        # read_only, so it's excluded from the dedupe cache below by
        # default -- deliberately, since most operator-tier calls are real
        # mutations that must never be silently replayed from a stale
        # cache. A composite made up entirely of research_ro actions is the
        # one safe exception: when the model retries the same RO after
        # seeing "missing documentation" hoping for a different answer, this
        # lets the existing cache absorb the repeat instead of re-searching
        # and re-embedding a full receipt, which is what was blowing the
        # turn's context budget on multi-RO sweeps.
        def _is_pure_research_ro_call(tool_name: str, tool_args: Any) -> bool:
            if tool_name != "calibration_iq_operator":
                return False
            actions = tool_args.get("actions") if isinstance(tool_args, dict) else None
            if not isinstance(actions, list) or not actions:
                return False
            return all(
                isinstance(a, dict) and a.get("operation") == "research_ro" for a in actions
            )

        dedupe_key: Optional[tuple[str, str]] = None
        tier_fn = getattr(self.registry, "tier", None)
        is_dedupeable = (callable(tier_fn) and tier_fn(name) == "read_only") or (
            _is_pure_research_ro_call(name, args)
        )
        if call_cache is not None and is_dedupeable:
            try:
                canonical_args = json.dumps(args, sort_keys=True, default=str)
            except TypeError:
                canonical_args = repr(args)
            dedupe_key = (name, canonical_args)
            if dedupe_key in call_cache:
                cached_result = call_cache[dedupe_key]
                feed(cached_result)
                yield {
                    "type": "tool_result",
                    "name": name,
                    "result": cached_result,
                    "deduplicated": True,
                }
                # The first call already rendered this result's card; a
                # verbatim repeat within the same turn must not render it
                # again.
                return

        try:
            from ..services import research_navigator_agent

            navigator_model_token = research_navigator_agent.bind_model_client(
                self.client
            )
            try:
                result = await self.registry.invoke(
                    name,
                    args,
                    message_id=(approval_context or {}).get("message_id"),
                    conversation_id=conversation_id,
                    tool_call_id=call_id,
                    user_id=(approval_context or {}).get("user_id"),
                    role=(approval_context or {}).get("role"),
                    calibration_iq_evidence=calibration_iq_evidence,
                    scrapex_evidence=scrapex_evidence,
                )
            finally:
                research_navigator_agent.reset_model_client(navigator_model_token)
        except NeedsApproval as pending:
            context = approval_context or {}
            if not all(
                context.get(key) for key in ("session_id", "user_id", "message_id")
            ):
                payload = {
                    "status": "blocked",
                    "message": "Protected action identity is incomplete; nothing was run.",
                }
                feed(payload)
                yield {"type": "tool_result", "name": name, "result": payload}
                return
            approval_id = self.store.create_approval(
                name,
                pending.summary,
                {"name": name, "args": pending.tool_args},
                conversation_id=conversation_id,
                session_id=str(context["session_id"]),
                user_id=str(context["user_id"]),
                message_id=int(context["message_id"]),
                tool_call_id=call_id,
                logged_args=self.registry.log_args(name, args),
            )
            record = self.store.get_approval(approval_id) or {}
            public_record = self.registry.public_approval(record)
            if record.get("status") in {"succeeded", "failed", "denied", "expired"}:
                receipt = self.store.get_execution_receipt(approval_id)
                replay = {
                    "status": record["status"],
                    "executed": bool((receipt or {}).get("executed")),
                    "success": bool((receipt or {}).get("success")),
                    "result": (receipt or {}).get("result"),
                    "execution_receipt": receipt,
                    "replayed": True,
                }
                feed(replay)
                yield {
                    "type": "tool_result",
                    "name": name,
                    "result": replay,
                    "receipt": receipt,
                }
                if receipt:
                    artifact = {"type": "execution_receipt", "data": receipt}
                    artifacts.append(artifact)
                    yield {"type": "artifact", "artifact": artifact}
                return
            if record.get("status") == "executing":
                payload = {
                    "status": "executing",
                    "executed": False,
                    "message": "This exact protected action is already executing.",
                }
                feed(payload)
                yield {"type": "tool_result", "name": name, "result": payload}
                return
            feed(
                {
                    "status": "awaiting_approval",
                    "message": (
                        "This action needs Otis's approval before it can run: "
                        f"{public_record.get('summary', 'Protected action')}"
                    ),
                }
            )
            approval = {
                "id": approval_id,
                "tool": public_record.get("tool", name),
                "summary": public_record.get("summary", f"Run {name}"),
                "args": public_record.get("args", {}),
                "status": record.get("status", "pending"),
                "action_digest": record.get("action_digest"),
                "idempotency_key": record.get("idempotency_key"),
            }
            artifact = {"type": "approval_request", "data": approval}
            artifacts.append(artifact)
            yield {
                "type": "approval",
                "approval": approval,
            }
            return
        except ToolBlocked as exc:
            feed({"status": "blocked", "message": str(exc)})
            yield {
                "type": "tool_result",
                "name": name,
                "result": {"status": "blocked", "message": str(exc)},
            }
            return
        except (ToolError, ValueError) as exc:
            feed({"status": "error", "message": str(exc)})
            yield {
                "type": "tool_result",
                "name": name,
                "result": {"status": "error", "message": str(exc)},
            }
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed", name)
            payload = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
            feed(payload)
            yield {"type": "tool_result", "name": name, "result": payload}
            return

        self._track_active_subject(
            conversation_id=conversation_id,
            name=name,
            result=result,
            call_id=call_id,
            invocation_context=approval_context or {},
        )
        feed(result)
        event_result = (
            tool_result_for_model(name, result)
            if name == "website_preview_generate"
            else result
        )
        yield {"type": "tool_result", "name": name, "result": event_result}
        if dedupe_key is not None and call_cache is not None:
            call_cache[dedupe_key] = result

        card_type = artifact_type_for_tool(name, result)
        if card_type and isinstance(result, dict):
            artifact = {"type": card_type, "data": result}
            artifacts.append(artifact)
            yield {"type": "artifact", "artifact": artifact}

    def _track_active_subject(
        self,
        *,
        conversation_id: int,
        name: str,
        result: Any,
        call_id: str,
        invocation_context: dict[str, Any],
    ) -> None:
        """Persist only a subject proven by one non-cached authoritative result."""
        if not hasattr(self.store, "set_conversation_subject"):
            return
        try:
            # Local import avoids a package-load cycle: services import the
            # orchestrator while registering execution-only capabilities.
            from ..services.conversation_subjects import (
                track_active_subject_from_tool_result,
            )

            track_active_subject_from_tool_result(
                self.store,
                conversation_id=conversation_id,
                tool_name=name,
                result=result,
                tool_call_id=call_id,
                message_id=invocation_context.get("message_id"),
                user_id=invocation_context.get("user_id"),
            )
        except Exception:  # noqa: BLE001 - subject continuity is fail-soft
            log.exception(
                "Could not update active subject from %s in conversation %s",
                name,
                conversation_id,
            )
