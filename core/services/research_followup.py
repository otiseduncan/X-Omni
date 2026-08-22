"""Conversation carry-over for short research-provider follow-ups.

A field conversation should behave like a conversation.  After the user asks
for a specific vehicle/system procedure, a follow-up such as "check ALLDATA for
it" must keep that vehicle and topic instead of turning the literal words
"check all data for it" into a new search query.

This wrapper is deliberately narrow: it only resolves short provider/source
follow-ups with a deictic reference (it/that/there/this) or an otherwise empty
provider instruction.  The original user message remains persisted unchanged;
only the research execution query is expanded from recent same-conversation
context.
"""

from __future__ import annotations

import re
import threading
from typing import Any

from . import adas_calibration_depth, research_workflow

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False

_PROVIDER_RE = re.compile(
    r"\b(?:all\s*data|alldata|oem|manufacturer|manufacturer'?s|official\s+(?:oem|site|web)|"
    r"web|internet|external\s+sources?|all\s+sources?)\b",
    re.IGNORECASE,
)
_DEICTIC_RE = re.compile(
    r"\b(?:it|that|this|there|them|those|same\s+(?:thing|vehicle|car|procedure|system))\b",
    re.IGNORECASE,
)
_PROVIDER_ACTION_RE = re.compile(
    r"\b(?:check|search|look|look\s+up|find|research|try|use|go\s+to|open|see)\b",
    re.IGNORECASE,
)
_VEHICLE_HINT_RE = re.compile(
    r"\b(?:19|20)\d{2}\b|\b\d{2}\s+(?:jeep|subaru|toyota|lexus|honda|ford|"
    r"chevrolet|gmc|cadillac|buick|nissan|infiniti|hyundai|kia|genesis|bmw|"
    r"mercedes|volkswagen|vw|audi|dodge|ram|chrysler)\b",
    re.IGNORECASE,
)
_SUBJECT_RE = re.compile(
    r"\b(?:calibrat|aim|alignment|beam\s+axis|blind\s+spot|\bbsm\b|eyesight|"
    r"camera|radar|adas|sensor|module|procedure|position\s+statement|repair)\b",
    re.IGNORECASE,
)


def provider_followup(message: object) -> bool:
    text = " ".join(str(message or "").split()).strip()
    if not text or len(text) > 180:
        return False
    if not (_PROVIDER_RE.search(text) and _PROVIDER_ACTION_RE.search(text)):
        return False
    # "check ALLDATA for it" is the primary case.  Also accept terse provider
    # commands such as "check ALLDATA" only when they carry no new vehicle/topic.
    if _DEICTIC_RE.search(text):
        return True
    return not (_VEHICLE_HINT_RE.search(text) or _SUBJECT_RE.search(text))


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
        # get_messages already contains the newly persisted current user turn.
        if not skipped_current and text == " ".join(current.split()).strip():
            skipped_current = True
            continue
        if provider_followup(text):
            continue
        if adas_calibration_depth.calibration_intent(text):
            return text
        if _VEHICLE_HINT_RE.search(text) and _SUBJECT_RE.search(text):
            return text
    return ""


def resolve_followup(message: object, history: list[dict[str, Any]]) -> str:
    current = " ".join(str(message or "").split()).strip()
    if not provider_followup(current):
        return current
    subject = _prior_subject(history, current)
    if not subject:
        return current

    folded = current.casefold()
    if re.search(r"\ball\s*data\b|\balldata\b", folded):
        provider_instruction = (
            "Check ALLDATA for this same vehicle and system. If ALLDATA does not "
            "answer it, continue to official OEM/manufacturer sources."
        )
    elif re.search(r"\boem\b|manufacturer|official", folded):
        provider_instruction = "Continue this same research through official OEM/manufacturer sources."
    else:
        provider_instruction = "Continue this same research through ALLDATA and official OEM/manufacturer sources."
    return f"{subject.rstrip('.!?')}. {provider_instruction}"


def install() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        try:
            from ..orchestrator import loop as loop_mod

            previous = loop_mod.Orchestrator._run
            if not getattr(previous, "_xomni_research_followup", False):
                async def run(self, conversation_id, user_message, approved_tool, approval_context):
                    if approved_tool or not provider_followup(user_message):
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

                run._xomni_research_followup = True  # type: ignore[attr-defined]
                loop_mod.Orchestrator._run = run
        except Exception:  # noqa: BLE001 - loop may load after isolated service tests
            pass
        _INSTALLED = True
