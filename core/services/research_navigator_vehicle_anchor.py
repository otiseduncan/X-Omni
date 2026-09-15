"""Force each managed primary Navigator task through an exact-VIN anchor.

ALLDATA keeps browser state between tasks. A new primary research objective must
therefore never inherit the prior vehicle/article merely because the page looks
usable. For a valid exact VIN, this guard forces the first identity check false
so the existing Navigator preflight reselects that VIN mechanically.

Dependency tasks deliberately preserve the already-verified vehicle/page context
so supporting documents can be followed without resetting to the vehicle picker.

There is intentionally no year/make/model fallback here. SI research without a
valid VIN must stop before Navigator task creation.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_navigator_vehicle_anchor_installed_v3__"
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_ANCHOR_STATE: ContextVar[dict[str, Any] | None] = ContextVar(
    "xomni_navigator_vehicle_anchor_state_v3",
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
    """Force one exact-VIN reselection for every managed primary task only."""
    if getattr(module, _INSTALLED_ATTR, False):
        return

    original_target_signal = module._target_already_selected
    original_run_task = module._run_task

    @wraps(original_target_signal)
    async def target_already_selected_with_anchor(
        settings: Any,
        provider: str,
        target: dict[str, Any],
    ):
        state = _ANCHOR_STATE.get()
        if (
            isinstance(state, dict)
            and state.get("role") == "primary"
            and not state.get("forced")
            and _managed_settings(settings)
            and _exact_vin(target)
        ):
            state["forced"] = True
            return False
        return await original_target_signal(settings, provider, target)

    @wraps(original_run_task)
    async def run_task_with_vehicle_anchor(*args: Any, **kwargs: Any):
        role = str(kwargs.get("role") or "primary")
        token = _ANCHOR_STATE.set(
            {
                "forced": role != "primary",
                "role": role,
            }
        )
        try:
            return await original_run_task(*args, **kwargs)
        finally:
            _ANCHOR_STATE.reset(token)

    module._target_already_selected = target_already_selected_with_anchor
    module._run_task = run_task_with_vehicle_anchor
    setattr(module, _INSTALLED_ATTR, True)
