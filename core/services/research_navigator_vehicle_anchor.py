"""Force each Navigator task to begin from an exact-VIN vehicle anchor.

The ALLDATA browser profile is persistent, so a completed task can leave the
browser sitting deep inside the previous procedure. ``research_navigator_agent``
already has a mechanical exact-VIN preflight, but it skips that preflight when
the current page still proves the requested vehicle is selected. That makes a
new task inherit the previous article as its starting context.

ScrapeX's existing ``select_vehicle`` fast path is the correct reset primitive:
it always opens ALLDATA's vehicle picker, types the exact VIN, and proves the
rendered vehicle page shows that VIN. This installer therefore forces only the
*first* target-signal check inside each Navigator task to report "not selected"
when a valid VIN is present. The agent then runs its normal ``select_vehicle``
preflight. Later target checks in the same task delegate to the real live
signal so verification remains unchanged.

No procedure meaning, menu choice, or provider hierarchy is decided here.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_navigator_vehicle_anchor_installed__"
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_ANCHOR_STATE: ContextVar[dict[str, bool] | None] = ContextVar(
    "xomni_navigator_vehicle_anchor_state",
    default=None,
)


def _exact_vin(target: Any) -> str:
    if not isinstance(target, dict):
        return ""
    vin = "".join(str(target.get("vin") or "").split()).upper()
    return vin if _VIN_RE.fullmatch(vin) else ""


def install(module: Any) -> None:
    """Force one exact-VIN reselection at the beginning of every Navigator task."""
    if getattr(module, _INSTALLED_ATTR, False):
        return

    original_target_signal = module._target_already_selected
    original_run_task = module._run_task

    @wraps(original_target_signal)
    async def target_already_selected_with_anchor(
        settings: Any, provider: str, target: dict[str, Any]
    ):
        state = _ANCHOR_STATE.get()
        if state is not None and not state.get("forced") and _exact_vin(target):
            # The agent interprets False by invoking ScrapeX's existing exact-VIN
            # select_vehicle fast path. That path itself opens the vehicle picker,
            # so this is the reset without adding a new browser action or endpoint.
            state["forced"] = True
            return False
        return await original_target_signal(settings, provider, target)

    @wraps(original_run_task)
    async def run_task_with_vehicle_anchor(*args: Any, **kwargs: Any):
        token = _ANCHOR_STATE.set({"forced": False})
        try:
            return await original_run_task(*args, **kwargs)
        finally:
            _ANCHOR_STATE.reset(token)

    module._target_already_selected = target_already_selected_with_anchor
    module._run_task = run_task_with_vehicle_anchor
    setattr(module, _INSTALLED_ATTR, True)
