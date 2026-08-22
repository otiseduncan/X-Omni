"""Carry conversational procedure follow-ups into the active vehicle/system research.

After X has already been asked for a specific calibration procedure, phrases such
as "show me the exact procedure", "pull it up", or "where is it?" refer to the
same vehicle/system. They must not become a fresh generic ADAS SI query.
"""

from __future__ import annotations

import re
import threading
from typing import Any

from . import adas_calibration_depth

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False

_CONTINUATION_RE = re.compile(
    r"^\s*(?:can\s+you\s+|could\s+you\s+|please\s+)?(?:"
    r"show\s+(?:me\s+)?(?:the\s+)?(?:exact\s+)?procedure|"
    r"give\s+(?:me\s+)?(?:the\s+)?(?:exact\s+)?procedure|"
    r"pull\s+(?:it|that|the\s+procedure)\s+up|"
    r"open\s+(?:it|that|the\s+procedure)|"
    r"where\s+(?:is|does)\s+(?:it|that|the\s+procedure)|"
    r"show\s+(?:me\s+)?where|"
    r"what(?:'s|\s+is)\s+the\s+exact\s+procedure"
    r")\s*[?.!]*\s*$",
    re.IGNORECASE,
)


def procedure_followup(message: object) -> bool:
    text = " ".join(str(message or "").split()).strip()
    return bool(text and len(text) <= 160 and _CONTINUATION_RE.match(text))


def _content(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    return " ".join(str(message.get("content") or "").split()).strip()


def _prior_subject(history: list[dict[str, Any]], current: str) -> str:
    skipped_current = False
    for message in reversed(history or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _content(message)
        if not text:
            continue
        if not skipped_current and text == current:
            skipped_current = True
            continue
        if procedure_followup(text):
            continue
        if adas_calibration_depth.calibration_intent(text):
            return text
    return ""


def resolve_followup(message: object, history: list[dict[str, Any]]) -> str:
    current = " ".join(str(message or "").split()).strip()
    if not procedure_followup(current):
        return current
    subject = _prior_subject(history, current)
    if not subject:
        return current
    return (
        f"{subject.rstrip('.!?')}. Find and return the exact OEM calibration procedure for this same vehicle and system. "
        "Check ADAS SI first, then the authenticated ALLDATA Repair/Collision vehicle workflow, then official OEM/manufacturer sources if needed. "
        "Do not substitute procedures from another model or year. If the exact procedure is found, summarize the steps and preserve the authoritative source in ADAS SI when appropriate."
    )


def install() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        try:
            from ..orchestrator import loop as loop_mod
            previous = loop_mod.Orchestrator._run
            if not getattr(previous, "_xomni_procedure_followup", False):
                async def run(self, conversation_id, user_message, approved_tool, approval_context):
                    if approved_tool or not procedure_followup(user_message):
                        async for event in previous(
                            self, conversation_id, user_message, approved_tool, approval_context
                        ):
                            yield event
                        return

                    history = self.store.get_messages(conversation_id)
                    resolved = resolve_followup(user_message, history)
                    async for event in previous(
                        self, conversation_id, resolved, approved_tool, approval_context
                    ):
                        yield event

                run._xomni_procedure_followup = True  # type: ignore[attr-defined]
                loop_mod.Orchestrator._run = run
        except Exception:  # noqa: BLE001
            pass
        _INSTALLED = True
