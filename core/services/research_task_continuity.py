"""Task-aware follow-up continuity, replacing per-phrase regex matchers.

Previously "check ALLDATA for it" (research_followup.py) and "show me the
exact procedure" (research_procedure_followup.py) each needed their own
hand-enumerated set of phrasings, because nothing remembered what "it" was.
That doesn't scale -- every new way a technician phrases the same follow-up
needs another pattern added somewhere.

research_task.py now records the vehicle/subject/status of the most recent
research request per conversation (research_workflow._persist_research_task).
This module resolves a short follow-up generically against that task: if the
new message doesn't name its own, different vehicle, and doesn't carry much
independent topical content of its own, the active task's vehicle/subject is
merged in ahead of it -- regardless of the exact words used. Vehicle identity
is a hard, parseable fact; conversational phrasing is not something worth
enumerating.
"""

from __future__ import annotations

import re
import threading
from typing import Optional

from . import research_task

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]{3,}")


def _content_token_count(text: str) -> int:
    from . import research_workflow  # local import avoids a module-load-order dependency

    stop = research_workflow._STOPWORDS  # noqa: SLF001 - shared stopword set, not a new phrase table
    return sum(1 for token in _WORD_RE.findall(text) if token.casefold() not in stop)


def looks_like_continuation(message: object) -> bool:
    """Generic, phrasing-agnostic test: is this short enough and low-content
    enough to plausibly be referring back to something already in progress,
    rather than introducing its own new topic?
    """
    text = " ".join(str(message or "").split()).strip()
    if not text or len(text) > 200:
        return False
    from . import adas_calibration_depth

    if adas_calibration_depth.calibration_intent(text):
        return True
    # Deliberately tight: a low informative-word count is a generic,
    # phrasing-agnostic signal for "this is probably referring back to
    # something," not a precise classifier. A higher threshold catches more
    # real follow-ups but also risks folding a genuinely unrelated short
    # message ("remind me tomorrow") into the active research subject.
    return _content_token_count(text) <= 2


def _record_adas_only_task(conversation_id: object, query: str, result_count: Optional[int], turn_count: int) -> None:
    """Best-effort: a plain ADAS SI search (the model's own tool choice, not
    routed through full_research) still names a vehicle/topic worth
    remembering. Without this, a turn that misses locally and never reaches
    ALLDATA leaves the task store untouched -- so a later "check ALLDATA for
    it" would merge against whatever older task happened to still be there
    (reported: a Ford F-150 question picked up a stale Hyundai Palisade task
    from earlier in the conversation, because the F-150 turn never updated
    anything).
    """
    try:
        from . import research_alldata_navigation as nav
        from ..config import Settings

        vehicle = nav.vehicle_from_query(query)
        if not vehicle.get("label"):
            return  # not enough identity in this query to be worth recording
        topic = nav.topic_from_query(query, vehicle)
        store = research_task.get_store(Settings.load().root)
        existing = store.get(str(conversation_id))
        same_vehicle = bool(
            existing
            and existing.vehicle_year == vehicle.get("year")
            and existing.vehicle_make == vehicle.get("make")
        )
        store.save(research_task.ResearchTask(
            conversation_id=str(conversation_id),
            vehicle_year=vehicle.get("year"),
            vehicle_make=vehicle.get("make"),
            vehicle_model_trim=vehicle.get("model_trim"),
            vehicle_label=vehicle.get("label"),
            subject=topic[:200],
            local_status="found" if (result_count or 0) > 0 else "missing",
            # Only carry forward ALLDATA/OEM/acquisition status when this is
            # still the same vehicle -- a different vehicle is a fresh task,
            # not a continuation, and should not inherit an unrelated
            # vehicle's "verified" evidence.
            alldata_status=existing.alldata_status if same_vehicle and existing else "not_started",
            alldata_evidence_url=(existing.alldata_evidence_url if same_vehicle and existing else None),
            oem_web_status=existing.oem_web_status if same_vehicle and existing else "not_started",
            oem_web_evidence_url=(existing.oem_web_evidence_url if same_vehicle and existing else None),
            acquisition_status=existing.acquisition_status if same_vehicle and existing else "pending",
            turn_count_at_update=turn_count,
        ))
    except Exception:  # noqa: BLE001 - continuity is best-effort, never blocks the turn
        pass


def merge_active_task(
    message: object, task: Optional["research_task.ResearchTask"], current_turn_count: int
) -> str:
    text = " ".join(str(message or "").split()).strip()
    if task is None or not text:
        return text
    if task.is_stale(current_turn_count):
        return text
    if not looks_like_continuation(text):
        return text

    from . import research_alldata_navigation as nav

    new_vehicle = nav.vehicle_from_query(text)
    if new_vehicle.get("year") and new_vehicle.get("make") and (
        new_vehicle["year"] != task.vehicle_year or new_vehicle["make"] != task.vehicle_make
    ):
        return text  # names its own, different vehicle -- a fresh request, not a follow-up

    subject = task.subject_line()
    if not subject:
        return text
    return f"{subject.rstrip('.!?')}. {text}"


def install() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        try:
            from ..orchestrator import loop as loop_mod

            previous = loop_mod.Orchestrator._run
            if not getattr(previous, "_xomni_task_continuity", False):
                async def run(self, conversation_id, user_message, approved_tool, approval_context):
                    if approved_tool:
                        async for event in previous(
                            self, conversation_id, user_message, approved_tool, approval_context
                        ):
                            yield event
                        return

                    merged = user_message
                    history_len = 0
                    try:
                        from ..config import Settings

                        store = research_task.get_store(Settings.load().root)
                        task = store.get(str(conversation_id))
                        history = self.store.get_messages(conversation_id)
                        history_len = len(history)
                        merged = merge_active_task(user_message, task, history_len)
                    except Exception:  # noqa: BLE001 - continuity is best-effort, never blocks the turn
                        pass

                    # Watch (without altering) this turn's tool events for a plain
                    # ADAS SI search the model chose on its own -- the only path
                    # research_workflow._persist_research_task doesn't cover, since
                    # it only fires when full_research actually runs.
                    adas_query: Optional[str] = None
                    adas_result_count: Optional[int] = None
                    async for event in previous(self, conversation_id, merged, approved_tool, approval_context):
                        if event.get("name") == "adas_si_search":
                            if event.get("type") == "tool_start":
                                adas_query = str((event.get("args") or {}).get("query") or "") or adas_query
                            elif event.get("type") == "tool_result":
                                result = event.get("result")
                                if isinstance(result, dict):
                                    adas_result_count = len(result.get("results") or [])
                        yield event

                    if adas_query:
                        _record_adas_only_task(conversation_id, adas_query, adas_result_count, history_len + 1)

                run._xomni_task_continuity = True  # type: ignore[attr-defined]
                loop_mod.Orchestrator._run = run
        except Exception:  # noqa: BLE001 - loop may load after isolated service tests
            pass
        _INSTALLED = True
