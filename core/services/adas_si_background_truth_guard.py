"""Keep model-owned SI status wording inside the authoritative job state.

The normal conversation model still decides what Otis meant and how to answer.
This guard adds the same bounded language-only truth review already used after
Calibration IQ mutations when the current turn has just returned an authoritative
ADAS SI background-job result.  It does not route a request or execute a tool.

It also repairs the model-owned no-tool review instruction for active background
work: the old text named ``adas_map_sweep`` unconditionally, even when Core's
record described service-information research.  The review now chooses the
matching read contract from the record itself.
"""

from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_si_background_truth_guard_v1__"
_STATUS_RESULT: ContextVar[dict[str, Any] | None] = ContextVar(
    "xomni_si_background_status_result", default=None
)

_BACKGROUND_REVIEW = (
    "Internal evidence check; this is not a new user request. Core's own record of "
    "background work, current as of this message: {record} First, compare the withheld "
    "draft with that record. Never say background work is completed while the record says "
    "it is still running, and never invent a result for a pending objective. If the draft "
    "needs fresher progress or a result not contained in the record, do not output "
    "NO_TOOL_NEEDED: call query_ciq for the matching background service. Use "
    "kind=adas_si_research for service-information research and kind=adas_map_sweep for "
    "an ADAS Map sweep. Then answer only from what that read returns."
)


def _is_status_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    action = str(result.get("action") or "").strip().casefold()
    return action in {"adas_si_research", "adas_si_research_status"} and bool(
        str(result.get("status") or "").strip()
    )


def note_status(result: Any) -> None:
    if _is_status_result(result):
        _STATUS_RESULT.set(dict(result))


def _fail_closed_status(result: Any) -> str:
    payload = result if isinstance(result, dict) else {}
    message = " ".join(str(payload.get("message") or "").split())
    if message:
        return message[:900]
    status = str(payload.get("status") or "").casefold()
    if status in {"running", "already_running"}:
        return "Service-information research is still running; no final result exists yet."
    if status == "completed":
        return "Service-information research finished; use the result card for the verified outcome."
    if status == "no_job":
        return "No service-information research job is recorded."
    return "I couldn't verify the service-information research status beyond the returned result."


def install(research_module: Any) -> None:
    if getattr(research_module, _INSTALLED_ATTR, False):
        return

    from ..orchestrator import loop as loop_module
    from ..orchestrator import truth_review as truth_review_module

    service_class = research_module.AdasSiResearchService
    original_start = service_class.start
    original_status = service_class.status

    @wraps(original_start)
    async def start_with_status_truth(self: Any, args: dict[str, Any]):
        result = await original_start(self, args)
        note_status(result)
        return result

    @wraps(original_status)
    async def status_with_status_truth(self: Any, args: dict[str, Any]):
        result = await original_status(self, args)
        note_status(result)
        return result

    service_class.start = start_with_status_truth
    service_class.status = status_with_status_truth

    original_required = loop_module.calibration_iq_mutation_truth_review_required
    original_reviewed_text = loop_module.Orchestrator._calibration_iq_truth_reviewed_text

    @wraps(original_required)
    def truth_review_required(
        operator_results: list[dict[str, Any]],
        work_prep_results: list[dict[str, Any]],
    ) -> bool:
        return bool(
            original_required(operator_results, work_prep_results)
            or _STATUS_RESULT.get() is not None
        )

    async def review_background_status(
        orchestrator: Any,
        messages: list[dict[str, Any]],
        candidate: str,
        status_result: dict[str, Any],
    ) -> str:
        loop_module.fit_messages_to_window(
            messages,
            [],
            context_tokens=int(getattr(orchestrator.settings, "context_tokens", 32_768)),
            reserve_tokens=int(getattr(orchestrator.settings, "max_response_tokens", 1_536)) + 1_500,
        )
        candidate = str(candidate or "").strip()
        if not candidate:
            evidence_json = loop_module._bounded_tool_result_json(  # noqa: SLF001
                {"adas_si_background_status": status_result}
            )
            candidate = (
                await truth_review_module.synthesize_candidate(
                    orchestrator.client, messages, evidence_json
                )
            ).strip()
            if not candidate:
                return _fail_closed_status(status_result)

        review = await truth_review_module.review_candidate(
            orchestrator.client, messages, candidate
        )
        if review is None:
            return _fail_closed_status(status_result)
        if review.valid:
            return candidate

        regenerated = (
            await truth_review_module.regenerate_candidate(
                orchestrator.client, messages, candidate, review
            )
        ).strip()
        if regenerated:
            second = await truth_review_module.review_candidate(
                orchestrator.client, messages, regenerated
            )
            if second is not None and second.valid:
                return regenerated
        return _fail_closed_status(status_result)

    @wraps(original_reviewed_text)
    async def truth_reviewed_text(
        orchestrator: Any,
        messages: list[dict[str, Any]],
        candidate: str,
        operator_results: list[dict[str, Any]],
        work_prep_results: list[dict[str, Any]],
    ) -> str:
        status_result = _STATUS_RESULT.get()
        if original_required(operator_results, work_prep_results):
            try:
                return await original_reviewed_text(
                    orchestrator,
                    messages,
                    candidate,
                    operator_results,
                    work_prep_results,
                )
            finally:
                _STATUS_RESULT.set(None)
        if status_result is None:
            return await original_reviewed_text(
                orchestrator,
                messages,
                candidate,
                operator_results,
                work_prep_results,
            )
        try:
            return await review_background_status(
                orchestrator, messages, candidate, status_result
            )
        finally:
            _STATUS_RESULT.set(None)

    loop_module.calibration_iq_mutation_truth_review_required = truth_review_required
    loop_module.Orchestrator._calibration_iq_truth_reviewed_text = truth_reviewed_text
    loop_module.BACKGROUND_REVIEW_PREFIX = _BACKGROUND_REVIEW
    setattr(research_module, _INSTALLED_ATTR, True)
