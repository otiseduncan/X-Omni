"""Tighten X's research/truth contract without deterministic routing.

The model still owns intent, references, source choice, and tool arguments. This
installer rewrites existing prompt sentences in place after live conversation
failures; it does not inspect user text, force a tool, or grow the static prompt
with duplicated routing machinery.
"""

from __future__ import annotations

_INSTALLED_ATTR = "__xomni_conversation_contract_guard_installed__"


def _replace_once(value: str, old: str, new: str) -> str:
    return value.replace(old, new, 1) if old in value else value


def install() -> None:
    from ..orchestrator import prompt

    if getattr(prompt, _INSTALLED_ATTR, False):
        return

    prompt.MODEL_FIRST_CONTRACT = _replace_once(
        prompt.MODEL_FIRST_CONTRACT,
        "Answer general technical, conceptual, or conversational questions directly from your own knowledge with no tool call.",
        "Answer generic conceptual questions directly; OEM-specific ADAS requirements or procedures need returned technical evidence.",
    )
    prompt.MODEL_FIRST_CONTRACT = _replace_once(
        prompt.MODEL_FIRST_CONTRACT,
        "use it for ad-hoc technical questions or a vehicle not tied to CIQ.",
        "use it for general OEM/ADAS SI questions even when a CIQ RO is active.",
    )
    prompt.MODEL_FIRST_CONTRACT = _replace_once(
        prompt.MODEL_FIRST_CONTRACT,
        "An RO's OEM procedures are always `research_si` here—not `delegate_research`;",
        "Only CIQ-attached RO procedure work is `research_si` here—not `delegate_research`; a follow-up asking whether an already-started `research_si` job finished is a status read of that job, not a new `delegate_research` request;",
    )
    prompt.TRUTH_AND_AUTHORIZATION = _replace_once(
        prompt.TRUTH_AND_AUTHORIZATION,
        "Never claim a search, read, mutation, acquisition, or test happened without a matching result in this turn; a turn that executed nothing has done nothing.",
        "Never claim a search, read, mutation, acquisition, test, or background-job state without a matching current-turn result; a turn that executed nothing has done nothing.",
    )

    setattr(prompt, _INSTALLED_ATTR, True)
