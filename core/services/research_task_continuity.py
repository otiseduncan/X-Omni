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
                    try:
                        from ..config import Settings

                        store = research_task.get_store(Settings.load().root)
                        task = store.get(str(conversation_id))
                        history = self.store.get_messages(conversation_id)
                        merged = merge_active_task(user_message, task, len(history))
                    except Exception:  # noqa: BLE001 - continuity is best-effort, never blocks the turn
                        pass

                    async for event in previous(self, conversation_id, merged, approved_tool, approval_context):
                        yield event

                run._xomni_task_continuity = True  # type: ignore[attr-defined]
                loop_mod.Orchestrator._run = run
        except Exception:  # noqa: BLE001 - loop may load after isolated service tests
            pass
        _INSTALLED = True
