"""Keep required SI dependencies from being starved by the primary search.

The Navigator objective budget historically used one shared turn counter for the
primary procedure and every supporting document.  A difficult primary could use
all 40 turns before an ACCEPT_WITH_DEPENDENCIES verdict was even available, so
perfectly valid prerequisites were immediately marked ``not_pursued`` without a
browser task.

The module already exposes ``DEPENDENCY_TURN_SHARE`` (currently 0.5), which is
intended to bound supporting-document work.  This installer makes that share a
real reserve *after* the primary has been judged: when the reviewer says the
objective requires another document, the objective budget is expanded so the
original primary allowance remains intact and the configured dependency share
is available for the dependency tasks.  The primary itself never gets access to
that reserve because expansion happens only after it returns.

Dependency titles are also de-duplicated mechanically (case/spacing/punctuation
only) before the objective queue sees them.  Nothing here decides whether a
document is semantically required or where to navigate; X's independent review
still owns those decisions.
"""

from __future__ import annotations

import math
import re
from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_dependency_completion_guard_installed__"
_REQUIRED_DECISIONS = frozenset({"ACCEPT_WITH_DEPENDENCIES", "FOLLOW_DEPENDENCY"})
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
        existing_index = by_key.get(key)
        if existing_index is None:
            by_key[key] = len(out)
            out.append(item)
            continue
        existing = out[existing_index]
        # The same reviewer sometimes names the same prerequisite twice with
        # one entry carrying a better explanation. Preserve the more useful
        # evidence without creating a second browser task.
        for field in ("reason", "quote"):
            current = str(existing.get(field) or "").strip()
            candidate = str(item.get(field) or "").strip()
            if len(candidate) > len(current):
                existing[field] = candidate
    return out


def _normalize_verdict(verdict: Any) -> Any:
    if not isinstance(verdict, dict):
        return verdict
    if "dependencies" not in verdict:
        return verdict
    verdict["dependencies"] = _dedupe_dependencies(verdict.get("dependencies"))
    return verdict


def _normalize_outcome(outcome: Any) -> Any:
    if not isinstance(outcome, dict):
        return outcome
    seen: set[int] = set()
    verdicts: list[dict[str, Any]] = []
    for value in (outcome.get("review"), *(outcome.get("reviews") or [])):
        if isinstance(value, dict) and id(value) not in seen:
            seen.add(id(value))
            verdicts.append(value)
    for candidate in outcome.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        value = candidate.get("review")
        if isinstance(value, dict) and id(value) not in seen:
            seen.add(id(value))
            verdicts.append(value)
    for verdict in verdicts:
        _normalize_verdict(verdict)
    return outcome


def _required_dependencies(outcome: Any) -> list[dict[str, Any]]:
    if not isinstance(outcome, dict):
        return []
    verdict = outcome.get("review") if isinstance(outcome.get("review"), dict) else {}
    if str(verdict.get("decision") or "") not in _REQUIRED_DECISIONS:
        return []
    return _dedupe_dependencies(verdict.get("dependencies"))


def _reserve_dependency_budget(module: Any, budget: Any, *, primary_limit: int) -> None:
    slots = int(getattr(budget, "dependency_slots", 0) or 0)
    if slots <= 0 or primary_limit <= 0:
        return
    try:
        share = float(getattr(module, "DEPENDENCY_TURN_SHARE", 0.5))
    except (TypeError, ValueError):
        share = 0.5
    if not 0.0 < share < 1.0:
        return

    # If dependencies are configured to receive 50% of the objective budget,
    # a 40-turn primary needs an 80-turn total envelope: 40 primary + 40
    # reserved dependency capacity. Expansion happens only after the primary
    # returns, so those extra turns cannot make the primary wander longer.
    expanded_limit = int(math.ceil(primary_limit / (1.0 - share)))
    current_limit = int(getattr(budget, "max_turns", primary_limit) or primary_limit)
    if expanded_limit <= current_limit:
        return
    budget.max_turns = expanded_limit
    events = getattr(budget, "progress_events", None)
    if isinstance(events, list):
        events.append(
            {
                "kind": "dependency_budget_reserved",
                "progress": True,
                "primary_turn_limit": primary_limit,
                "objective_turn_limit": expanded_limit,
                "dependency_share": share,
                "dependency_slots": slots,
            }
        )


def install(module: Any) -> None:
    """Install dependency de-duplication and post-primary turn reservation."""
    if getattr(module, _INSTALLED_ATTR, False):
        return

    original_run_task = module._run_task

    @wraps(original_run_task)
    async def run_task_with_dependency_completion(*args: Any, **kwargs: Any):
        budget = kwargs.get("budget")
        role = str(kwargs.get("role") or "primary")
        try:
            primary_limit = int(getattr(budget, "max_turns", 0) or 0)
        except (TypeError, ValueError):
            primary_limit = 0

        outcome = await original_run_task(*args, **kwargs)
        outcome = _normalize_outcome(outcome)

        if (
            role == "primary"
            and budget is not None
            and _required_dependencies(outcome)
        ):
            _reserve_dependency_budget(module, budget, primary_limit=primary_limit)
        return outcome

    module._run_task = run_task_with_dependency_completion
    setattr(module, _INSTALLED_ATTR, True)
