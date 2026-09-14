"""Reserve part of one SI objective's hard turn ceiling for required documents.

The Navigator owns one hard model-turn ceiling per research objective. Before
this guard, the primary procedure could consume all of that ceiling and only
then return ``ACCEPT_WITH_DEPENDENCIES``; the required supporting documents
were therefore marked ``not_pursued`` without ever receiving a browser task.

``research_navigator_agent`` already defines ``DEPENDENCY_TURN_SHARE`` and
``MAX_DEPENDENCIES``. This installer makes those existing limits real without
raising the objective ceiling: it gives the primary task only the non-reserved
portion of the budget, then returns the turns it actually used to the shared
objective budget. The untouched reserve is available to the dependency queue
that the agent already implements.

Dependency titles are also de-duplicated mechanically (case/spacing/punctuation
only) before that queue sees them. Nothing here decides whether a supporting
document is semantically required or where to navigate; X's independent review
still owns both decisions.
"""

from __future__ import annotations

import re
from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_dependency_completion_guard_installed__"
_TITLE_SEPARATORS = re.compile(r"[^a-z0-9]+")


def _dependency_key(value: Any) -> str:
    text = " ".join(str(value or "").casefold().split())
    return " ".join(_TITLE_SEPARATORS.sub(" ", text).split())


def _dedupe_dependencies(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    by_key: dict[str, int] = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        title = " ".join(str(item.get("title") or "").split())
        if not title:
            continue
        item["title"] = title
        key = _dependency_key(title)
        if not key:
            continue
        index = by_key.get(key)
        if index is None:
            by_key[key] = len(out)
            out.append(item)
            continue
        existing = out[index]
        # Preserve the more informative grounded explanation when the same
        # dependency is emitted twice with punctuation/spacing variation.
        for field in ("reason", "quote"):
            current = str(existing.get(field) or "").strip()
            candidate = str(item.get(field) or "").strip()
            if len(candidate) > len(current):
                existing[field] = candidate
    return out


def _normalize_outcome(outcome: Any) -> Any:
    if not isinstance(outcome, dict):
        return outcome
    seen: set[int] = set()
    verdicts: list[dict[str, Any]] = []
    values: list[Any] = [outcome.get("review")]
    values.extend(outcome.get("reviews") or [])
    for candidate in outcome.get("candidates") or []:
        if isinstance(candidate, dict):
            values.append(candidate.get("review"))
    for verdict in values:
        if not isinstance(verdict, dict) or id(verdict) in seen:
            continue
        seen.add(id(verdict))
        verdicts.append(verdict)
    for verdict in verdicts:
        if "dependencies" in verdict:
            verdict["dependencies"] = _dedupe_dependencies(verdict.get("dependencies"))
    return outcome


def _dependency_reserve(module: Any, budget: Any) -> tuple[int, int]:
    """Return (primary_turn_cap, reserved_dependency_turns)."""
    total = max(1, int(getattr(budget, "max_turns", 1) or 1))
    slots = max(0, int(getattr(budget, "dependency_slots", 0) or 0))
    if slots <= 0 or total <= 4:
        return total, 0
    try:
        share = float(getattr(module, "DEPENDENCY_TURN_SHARE", 0.5))
    except (TypeError, ValueError):
        share = 0.5
    if not 0.0 < share < 1.0:
        return total, 0

    # Match the agent's existing per-dependency calculation exactly. For the
    # current 40-turn / 3-slot / 0.5 contract this is 6 turns per dependency,
    # 18 reserved total, 22 available to the primary. The hard 40-turn ceiling
    # never changes.
    per_dependency = max(4, int(total * share / slots))
    reserved = min(total - 4, per_dependency * slots)
    primary_cap = max(4, total - reserved)
    return primary_cap, reserved


def install(module: Any) -> None:
    """Install hard-ceiling dependency reservation and mechanical de-duplication."""
    if getattr(module, _INSTALLED_ATTR, False):
        return

    original_run_task = module._run_task

    @wraps(original_run_task)
    async def run_task_with_dependency_completion(*args: Any, **kwargs: Any):
        parent_budget = kwargs.get("budget")
        role = str(kwargs.get("role") or "primary")

        # Only the primary needs a reservation. Dependency tasks are already
        # handed a bounded sub-budget by _run_objective and must not recursively
        # reserve from themselves.
        if role != "primary" or parent_budget is None:
            return _normalize_outcome(await original_run_task(*args, **kwargs))

        primary_cap, reserved = _dependency_reserve(module, parent_budget)
        if reserved <= 0:
            return _normalize_outcome(await original_run_task(*args, **kwargs))

        primary_budget = module._Budget(
            max_turns=primary_cap,
            dependency_slots=0,
        )
        delegated = dict(kwargs)
        delegated["budget"] = primary_budget
        outcome = _normalize_outcome(await original_run_task(*args, **delegated))

        parent_budget.turns_used += primary_budget.turns_used
        parent_budget.stall_points = primary_budget.stall_points
        parent_budget.progress_events.extend(primary_budget.progress_events)
        parent_budget.progress_events.append(
            {
                "kind": "dependency_budget_reserved",
                "progress": True,
                "hard_turn_limit": parent_budget.max_turns,
                "primary_turn_limit": primary_cap,
                "reserved_dependency_turns": reserved,
                "dependency_slots": parent_budget.dependency_slots,
            }
        )
        return outcome

    module._run_task = run_task_with_dependency_completion
    setattr(module, _INSTALLED_ATTR, True)
