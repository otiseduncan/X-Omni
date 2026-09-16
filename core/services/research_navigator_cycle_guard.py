"""Mechanical cycle detection for model-driven ALLDATA navigation.

X owns navigation meaning and chooses every browser action.  This guard only
remembers exact rendered page states that X has already been shown during one
Navigator task.  A return to an identical state is a revisit, not fresh
progress, even when X reached it through a different menu path (A -> B -> A).

That distinction matters because the base Navigator compares only with the
immediately previous page.  A two-page menu cycle therefore looked like steady
progress and kept refunding the stall budget.  The guard changes no click, route,
URL, or semantic decision: it marks revisits as mechanical non-progress and
adds an explicit model-facing warning to reassess the current live controls.

ALLDATA's ADAS Quick Reference remains a provider hint, never a forced route.
When a cycle is detected the warning reminds X that, *if it is visible in the
current observation*, it is a useful provider-native index worth considering.
"""

from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_navigator_cycle_guard_v1__"

_STATE: ContextVar[dict[str, Any] | None] = ContextVar(
    "xomni_navigator_cycle_guard_state",
    default=None,
)


def _new_state() -> dict[str, Any]:
    return {
        "seen_page_states": set(),
        "observation_revisits": {},
        "latest_observation_id": None,
        "latest_revisit": False,
        "revisit_count": 0,
    }


def record_page_state(
    state: dict[str, Any], *, observation_id: Any, page_state: str
) -> bool:
    """Record one observation and return whether it revisits an earlier state.

    The same observation is inspected more than once by the Navigator loop; that
    must not turn a newly-seen page into a revisit on its second internal read.
    """

    observation_key = str(observation_id or "").strip()
    if not observation_key:
        state["latest_observation_id"] = None
        state["latest_revisit"] = False
        return False

    known = state["observation_revisits"]
    if observation_key in known:
        revisit = bool(known[observation_key])
    else:
        revisit = page_state in state["seen_page_states"]
        known[observation_key] = revisit
        state["seen_page_states"].add(page_state)
        if revisit:
            state["revisit_count"] = int(state.get("revisit_count") or 0) + 1

    state["latest_observation_id"] = observation_key
    state["latest_revisit"] = revisit
    return revisit


def cycle_warning(revisit_count: int) -> str:
    return (
        "NAVIGATION CYCLE DETECTED: this exact rendered page state was already "
        "observed earlier in this task. Returning here is not new progress. Do "
        "not repeat the route that led back here; re-read the current live controls "
        "and choose a genuinely different strategy. If ALLDATA's ADAS Quick "
        "Reference is visible in THIS observation, it is a provider-native ADAS "
        "component index worth inspecting before another generic menu drill-down. "
        "That is a navigation option, not a forced route; choose the next action "
        f"from the live page yourself. Revisit count: {revisit_count}."
    )


def install(agent_module: Any) -> None:
    """Install task-local revisit accounting without choosing navigation semantics."""

    if getattr(agent_module, _INSTALLED_ATTR, False):
        return

    original_run_task = agent_module._run_task
    original_page_state = agent_module._page_state
    original_visual_content = agent_module._visual_observation_content
    original_budget_note = agent_module._Budget.note
    original_system_prompt = agent_module._system_prompt

    @wraps(original_run_task)
    async def run_task_with_cycle_memory(*args: Any, **kwargs: Any):
        token = _STATE.set(_new_state())
        try:
            return await original_run_task(*args, **kwargs)
        finally:
            _STATE.reset(token)

    @wraps(original_page_state)
    def page_state_with_cycle_memory(summary: dict[str, Any]) -> str:
        page_state = original_page_state(summary)
        state = _STATE.get()
        if state is not None and isinstance(summary, dict):
            record_page_state(
                state,
                observation_id=summary.get("observation_id"),
                page_state=page_state,
            )
        return page_state

    @wraps(original_budget_note)
    def note_with_cycle_accounting(
        self: Any,
        kind: str,
        *,
        progress: bool,
        cost: int = 1,
        **detail: Any,
    ) -> None:
        state = _STATE.get()
        if (
            state is not None
            and progress
            and kind in {"new_url", "new_page_state"}
            and state.get("latest_revisit") is True
        ):
            detail = {
                **detail,
                "revisited_page_state": True,
                "revisit_count": int(state.get("revisit_count") or 0),
            }
            return original_budget_note(
                self,
                "revisited_page_state",
                progress=False,
                cost=max(1, int(cost)),
                **detail,
            )
        return original_budget_note(
            self,
            kind,
            progress=progress,
            cost=cost,
            **detail,
        )

    @wraps(original_visual_content)
    def visual_content_with_cycle_warning(
        heading: str,
        summary: dict[str, Any],
        screenshot: Any,
    ):
        state = _STATE.get()
        observation_id = str((summary or {}).get("observation_id") or "").strip()
        revisit = bool(
            state is not None
            and observation_id
            and state["observation_revisits"].get(observation_id) is True
        )
        if revisit:
            heading = (
                f"{heading} "
                + cycle_warning(int(state.get("revisit_count") or 0))
            )
        return original_visual_content(heading, summary, screenshot)

    @wraps(original_system_prompt)
    def system_prompt_with_cycle_strategy(*args: Any, **kwargs: Any) -> str:
        base = str(original_system_prompt(*args, **kwargs))
        return base + (
            "\n\nNAVIGATION STRATEGY: changing pages is not automatically progress. If you "
            "return to a rendered state you already saw in this task, treat that as a "
            "cycle and change strategy rather than drilling the same menu hierarchy again. "
            "Provider-native indexes can be better starting points than generic service "
            "menus. In ALLDATA, when ADAS Quick Reference is actually visible on the exact "
            "selected vehicle page, inspect it as a high-value index of ADAS systems and "
            "procedure links instead of repeatedly reopening the same generic menus. This "
            "is a heuristic only: reason from the live observation and choose the action "
            "yourself; no fixed path is required."
        )

    agent_module._run_task = run_task_with_cycle_memory
    agent_module._page_state = page_state_with_cycle_memory
    agent_module._Budget.note = note_with_cycle_accounting
    agent_module._visual_observation_content = visual_content_with_cycle_warning
    agent_module._system_prompt = system_prompt_with_cycle_strategy
    setattr(agent_module, _INSTALLED_ATTR, True)
