"""Tighten X's model-facing research/truth contract without deterministic routing.

The model still owns intent, references, source choice, and tool arguments.  This
installer only makes several boundaries explicit after live conversation failures:

* OEM-specific ADAS requirements are evidence questions, not memory questions.
* an active CIQ subject must not hijack an explicitly general ADAS SI question.
* background-work claims require a current tool result proving that state.
* RO identifiers are copied from the user/subject; never invented.

No user text is inspected here and no tool is forced.
"""

from __future__ import annotations

from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_conversation_contract_guard_installed__"

_PROMPT_APPEND = """

## OEM research and follow-up boundary
A manufacturer-specific ADAS requirement is not a generic knowledge question. Questions about calibration or inspection prerequisites, bumper on/off state, windshield acceptability, collision inspection, aiming/setup conditions, triggers, procedures, or OEM policy require returned technical evidence. For a general OEM/reference question, use `delegate_research` with ADAS SI first even when a CIQ RO is the active subject. If Otis explicitly says "check ADAS SI" without tying the request to a named RO or saying this/that RO/vehicle, do not inherit the active RO. Use `stage_action research_si` only when he is asking to obtain/file the missing SI for a CIQ RO or an explicitly named CIQ scope.

Never say an SI/ADAS Map/research job is running, pending, queued, completed, or about to post unless a current-turn execution/status result proves that exact state. Never invent an RO number or short-form identifier. Use an RO only when Otis supplied it in the current request or the active subject is clearly referenced by a pronoun/follow-up.
""".strip()


def install() -> None:
    from ..orchestrator import prompt
    from ..tools import meta

    if getattr(prompt, _INSTALLED_ATTR, False):
        return

    if _PROMPT_APPEND not in prompt.TRUTH_AND_AUTHORIZATION:
        prompt.TRUTH_AND_AUTHORIZATION = (
            prompt.TRUTH_AND_AUTHORIZATION.rstrip() + "\n\n" + _PROMPT_APPEND
        )

    meta.DELEGATE_RESEARCH_SCHEMA["description"] = (
        str(meta.DELEGATE_RESEARCH_SCHEMA.get("description") or "").rstrip()
        + " General OEM/manufacturer ADAS questions belong here even if a CIQ RO is "
          "the active conversation subject. An explicit 'check ADAS SI' request that "
          "is not tied to a named/current RO belongs here; do not inherit the active RO."
    )

    meta.QUERY_CIQ_SCHEMA["description"] = (
        str(meta.QUERY_CIQ_SCHEMA.get("description") or "").rstrip()
        + " kind=status proves service/data-plane health only; it is not a business "
          "count and must never be described as active ROs, vehicles, or calibrations."
    )

    original_stage_action_schema = meta.stage_action_schema

    @wraps(original_stage_action_schema)
    def stage_action_schema(*args: Any, **kwargs: Any) -> dict[str, Any]:
        schema = original_stage_action_schema(*args, **kwargs)
        description = str(schema.get("description") or "").rstrip()
        schema["description"] = (
            description
            + " research_si is for obtaining/filing SI for a CIQ RO or explicitly "
              "named CIQ scope. Do not select research_si merely because a prior RO "
              "is active when Otis is asking a general OEM/ADAS SI technical question; "
              "use delegate_research for that. Never invent a repair_order_id."
        )
        return schema

    meta.stage_action_schema = stage_action_schema
    setattr(prompt, _INSTALLED_ATTR, True)
