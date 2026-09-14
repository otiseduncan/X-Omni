from __future__ import annotations

import re
from contextvars import ContextVar
from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_navigator_vehicle_anchor_installed_v2__"
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_PICKER_URL = "https://my.alldata.com/repair/#/select-vehicle"
_ANCHOR_STATE: ContextVar[dict[str, Any] | None] = ContextVar(
    "xomni_navigator_vehicle_anchor_state_v2",
    default=None,
)


def _exact_vin(target: Any) -> str:
    if not isinstance(target, dict):
        return ""
    vin = "".join(str(target.get("vin") or "").split()).upper()
    return vin if _VIN_RE.fullmatch(vin) else ""


def _ymm(target: Any) -> bool:
    if not isinstance(target, dict):
        return False
    try:
        year = int(target.get("year"))
    except (TypeError, ValueError):
        return False
    return (
        1900 <= year <= 2100
        and bool(str(target.get("make") or "").strip())
        and bool(str(target.get("model") or "").strip())
    )


def _managed_settings(settings: Any) -> bool:
    value = getattr(settings, "scrapex_project_path", None)
    return value is not None and bool(str(value).strip())


def install(module: Any) -> None:
    """Anchor every managed primary task and preserve dependency context.

    VIN targets keep the existing exact-VIN selector. Y/M/M-only targets are
    mechanically returned to ALLDATA's vehicle picker immediately after the
    ScrapeX task is created, before the model receives its first observation.
    X then chooses from the rendered picker; ScrapeX's target signal remains
    the authority on whether year/make/model/trim actually match.
    """
    if getattr(module, _INSTALLED_ATTR, False):
        return

    original_target_signal = module._target_already_selected
    original_run_task = module._run_task
    original_navigator = module.scrapex_svc.navigator
    original_prompt = module._system_prompt
    original_note = module._vehicle_selection_note

    @wraps(original_navigator)
    async def navigator_with_ymm_anchor(settings: Any, args: dict[str, Any]):
        result = await original_navigator(settings, args)
        state = _ANCHOR_STATE.get()
        if not isinstance(state, dict) or state.get("role") != "primary":
            return result
        if state.get("mode") != "year_make_model" or state.get("picker_opened"):
            return result
        if str((args or {}).get("action") or "") != "create_task":
            return result
        if not (
            isinstance(result, dict)
            and result.get("success")
            and result.get("verified")
        ):
            return result

        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        task_id = str(data.get("id") or data.get("task_id") or "").strip()
        if not task_id:
            return result
        state["picker_opened"] = True
        anchored = await original_navigator(
            settings,
            {"action": "open", "task_id": task_id, "url": _PICKER_URL},
        )
        if (
            isinstance(anchored, dict)
            and anchored.get("success")
            and anchored.get("verified")
        ):
            state["picker_anchor_verified"] = True
            return result
        return {
            **result,
            "success": False,
            "verified": False,
            "status": "vehicle_anchor_failed",
            "error": {
                "message": (
                    "The Navigator task was created, but the vehicle picker could not be "
                    "opened for year/make/model identity. No model navigation was started."
                )
            },
        }

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
            and (_exact_vin(target) or _ymm(target))
        ):
            state["forced"] = True
            return False
        return await original_target_signal(settings, provider, target)

    @wraps(original_run_task)
    async def run_task_with_vehicle_anchor(*args: Any, **kwargs: Any):
        role = str(kwargs.get("role") or "primary")
        target = (
            kwargs.get("target")
            if isinstance(kwargs.get("target"), dict)
            else {}
        )
        mode = (
            "vin"
            if _exact_vin(target)
            else ("year_make_model" if _ymm(target) else "unknown")
        )
        token = _ANCHOR_STATE.set(
            {
                "forced": role != "primary",
                "role": role,
                "mode": mode,
                "picker_opened": False,
                "picker_anchor_verified": False,
            }
        )
        try:
            return await original_run_task(*args, **kwargs)
        finally:
            _ANCHOR_STATE.reset(token)

    @wraps(original_prompt)
    def system_prompt_with_identity_mode(*args: Any, **kwargs: Any) -> str:
        text = str(original_prompt(*args, **kwargs))
        target = (
            args[0]
            if args and isinstance(args[0], dict)
            else kwargs.get("target") or {}
        )
        if not _exact_vin(target) and _ymm(target):
            text += (
                "\n\nVEHICLE IDENTITY: Calibration IQ has no valid VIN for this repair order. "
                "The runtime has mechanically reset this primary task to the vehicle picker. "
                "Select the requested year, make, and model from what the live picker actually "
                "shows. If trim/configuration choices remain and the supplied target does not "
                "distinguish them, do not guess a variant; use only evidence that applies to "
                "the verified year/make/model family and preserve any configuration uncertainty."
            )
        return text

    @wraps(original_note)
    def vehicle_selection_note_with_ymm(
        selected: Any,
        target: dict[str, Any],
    ) -> str:
        text = str(original_note(selected, target))
        if selected is False and not _exact_vin(target) and _ymm(target):
            text += (
                " The task has been reset to the vehicle picker because no VIN is recorded; "
                "select the requested year/make/model from the rendered choices before research."
            )
        return text

    module.scrapex_svc.navigator = navigator_with_ymm_anchor
    module._target_already_selected = target_already_selected_with_anchor
    module._run_task = run_task_with_vehicle_anchor
    module._system_prompt = system_prompt_with_identity_mode
    module._vehicle_selection_note = vehicle_selection_note_with_ymm
    setattr(module, _INSTALLED_ATTR, True)
