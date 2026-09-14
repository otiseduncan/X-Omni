from __future__ import annotations

from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_dependency_budget_extension_installed__"
DEPENDENCY_TURNS_EACH = 12
MAX_OBJECTIVE_TURNS = 76


def _required_count(outcome: Any, slots: int) -> int:
    if not isinstance(outcome, dict) or slots <= 0:
        return 0
    verdict = outcome.get("review") if isinstance(outcome.get("review"), dict) else {}
    if str(verdict.get("decision") or "") not in {
        "ACCEPT_WITH_DEPENDENCIES",
        "FOLLOW_DEPENDENCY",
    }:
        return 0
    seen: set[str] = set()
    for raw in verdict.get("dependencies") or []:
        if not isinstance(raw, dict):
            continue
        key = " ".join(str(raw.get("title") or "").casefold().split())
        if key:
            seen.add(key)
    return min(slots, len(seen))


def install(module: Any) -> None:
    """Keep the primary's full budget; add bounded turns only for required docs."""
    if getattr(module, _INSTALLED_ATTR, False):
        return
    original = module._run_task

    @wraps(original)
    async def run_task_with_dependency_capacity(*args: Any, **kwargs: Any):
        outcome = await original(*args, **kwargs)
        if str(kwargs.get("role") or "primary") != "primary":
            return outcome
        budget = kwargs.get("budget")
        if budget is None:
            return outcome
        slots = max(0, int(getattr(budget, "dependency_slots", 0) or 0))
        required = _required_count(outcome, slots)
        if required <= 0:
            return outcome

        before = max(1, int(getattr(budget, "max_turns", 1) or 1))
        desired = min(
            MAX_OBJECTIVE_TURNS,
            before + DEPENDENCY_TURNS_EACH * required,
        )
        if desired <= before:
            return outcome
        budget.max_turns = desired
        events = getattr(budget, "progress_events", None)
        if isinstance(events, list):
            events.append(
                {
                    "kind": "dependency_capacity_added",
                    "progress": True,
                    "primary_turn_limit": before,
                    "objective_turn_limit": desired,
                    "required_dependencies": required,
                    "dependency_turns_each": DEPENDENCY_TURNS_EACH,
                }
            )
        return outcome

    module._run_task = run_task_with_dependency_capacity
    setattr(module, _INSTALLED_ATTR, True)
