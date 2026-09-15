"""Tighten X's research/truth contract without deterministic routing.

The model still owns intent, references, source choice, and tool arguments. This
installer adds one compact evidence boundary after live conversation failures;
it does not inspect user text or force a tool.
"""

from __future__ import annotations

_INSTALLED_ATTR = "__xomni_conversation_contract_guard_installed__"

_PROMPT_APPEND = (
    "OEM-specific ADAS requirements, procedures, prerequisites, and policies need "
    "returned technical evidence. For a general OEM or explicit ADAS SI question, "
    "use delegate_research even if an RO is active; use research_si only to obtain/"
    "attach SI for a named CIQ RO or scope. Never claim background work is running "
    "or complete without a current result, and never invent an RO identifier."
)


def install() -> None:
    from ..orchestrator import prompt

    if getattr(prompt, _INSTALLED_ATTR, False):
        return
    if _PROMPT_APPEND not in prompt.TRUTH_AND_AUTHORIZATION:
        prompt.TRUTH_AND_AUTHORIZATION = (
            prompt.TRUTH_AND_AUTHORIZATION.rstrip() + "\n\n" + _PROMPT_APPEND
        )
    setattr(prompt, _INSTALLED_ATTR, True)
