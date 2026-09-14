"""Force each managed *primary* Navigator task to begin from an exact-VIN anchor.

The ALLDATA browser profile is persistent, so a completed top-level objective can
leave the browser sitting deep inside the previous procedure. The Navigator's
mechanical exact-VIN preflight normally skips reselection when the current page
still proves the requested vehicle is selected, which can make a new primary
objective inherit stale article context.

ScrapeX's existing ``select_vehicle`` fast path is the correct primary reset
primitive: it opens ALLDATA's vehicle picker, types the exact VIN, and proves the
rendered vehicle page shows that VIN. This installer forces only the *first*
target-signal check inside each managed **primary** task to report "not
selected" when a valid VIN is present. Later checks delegate to the live signal.

Dependency tasks are different. They belong to the same already-verified vehicle
and are created because the accepted procedure just named a supporting document.
Resetting those tasks through the vehicle picker throws away the useful current
procedure/reference context and spends their small bounded turn budget getting
back to where they started. Dependency tasks therefore retain the live vehicle
context while still being mechanically re-verified by the normal target signal.

The forced anchor remains limited to settings that explicitly identify a managed
ScrapeX project. Hermetic adapter/unit callers retain their original behavior.
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


def _managed_settings(settings: Any) -> bool:
    value = getattr(settings, "scrapex_project_path", None)
    return value is not None and bool(str(value).strip())


def install(module: Any) -> None:
    """Force one exact-VIN reselection at the beginning of each managed primary."""
    if getattr(module, _INSTALLED_ATTR, False):
        return

    original_target_signal = module._target_already_selected
    original_run_task = module._run_task

    @wraps(original_target_signal)
    async def target_already_selected_with_anchor(
        settings: Any, provider: str, target: dict[str, Any]
    ):
        state = _ANCHOR_STATE.get()
        if (
            state is not None
            and not state.get("forced")
            and _managed_settings(settings)
            and _exact_vin(target)
        ):
            # False makes the existing agent invoke ScrapeX select_vehicle,
            # whose exact-VIN proof remains the authority on the new page.
            state["forced"] = True
            return False
        return await original_target_signal(settings, provider, target)

    @wraps(original_run_task)
    async def run_task_with_vehicle_anchor(*args: Any, **kwargs: Any):
        role = str(kwargs.get("role") or "primary")
        # A dependency is already inside the same objective/vehicle and should
        # keep the procedure context that named it. Mark its anchor as already
        # consumed so the normal live target check decides identity.
        token = _ANCHOR_STATE.set({"forced": role != "primary"})
        try:
            return await original_run_task(*args, **kwargs)
        finally:
            _ANCHOR_STATE.reset(token)

    module._target_already_selected = target_already_selected_with_anchor
    module._run_task = run_task_with_vehicle_anchor
    setattr(module, _INSTALLED_ATTR, True)
