"""Complete no-VIN vehicle identity mechanically before X reasons about SI.

The existing vehicle anchor resets a managed year/make/model task to ALLDATA's
picker.  Resetting alone is not identity proof: the live five-car run showed a
Ford task inheriting the Toyota article left by the previous task.  This layer
runs ScrapeX's bounded Y/M/M selector immediately after task creation, then
requires ScrapeX's target signal to confirm the same target before the first
model observation is allowed through.

No semantic procedure choice lives here.  The payload is built only from the
already-resolved CIQ/case target.  ScrapeX owns the browser selection mechanics;
X owns every subsequent service-information reasoning decision.
"""

from __future__ import annotations

import json
from functools import wraps
from typing import Any
from urllib.parse import quote

_INSTALLED_ATTR = "__xomni_ymm_mechanical_anchor_installed_v1__"


def _managed(settings: Any) -> bool:
    value = getattr(settings, "scrapex_project_path", None)
    return value is not None and bool(str(value).strip())


def _ymm(target: Any) -> dict[str, Any] | None:
    if not isinstance(target, dict):
        return None
    try:
        year = int(target.get("year"))
    except (TypeError, ValueError):
        return None
    make = str(target.get("make") or "").strip()
    model = str(target.get("model") or "").strip()
    if not (1900 <= year <= 2100 and make and model):
        return None
    vin = "".join(str(target.get("vin") or "").split())
    if vin:
        return None
    return {
        "year": year,
        "make": make,
        "model": model,
        "trim": str(target.get("trim") or "").strip() or None,
        "engine": str(target.get("engine") or "").strip() or None,
    }


def _task_id(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return str(data.get("id") or data.get("task_id") or "").strip()


def _failure(result: dict[str, Any], *, code: str, message: str, detail: Any = None) -> dict[str, Any]:
    output = {
        **result,
        "success": False,
        "verified": False,
        "status": code,
        "error": {"code": code, "message": message},
    }
    if detail is not None:
        output["error"]["detail"] = detail
    return output


def install(agent_module: Any, anchor_module: Any, scrapex_module: Any) -> None:
    """Install after ``research_navigator_vehicle_anchor``."""
    if getattr(agent_module, _INSTALLED_ATTR, False):
        return

    original_navigator = scrapex_module.navigator

    @wraps(original_navigator)
    async def navigator_with_mechanical_ymm(settings: Any, args: dict[str, Any]):
        result = await original_navigator(settings, args)
        if not isinstance(args, dict) or str(args.get("action") or "") != "create_task":
            return result
        if not _managed(settings):
            return result
        if not (
            isinstance(result, dict)
            and result.get("success") is True
            and result.get("verified") is True
        ):
            return result

        state = anchor_module._ANCHOR_STATE.get()  # noqa: SLF001 - companion installer
        if not isinstance(state, dict) or state.get("role") != "primary":
            return result
        if state.get("mode") != "year_make_model":
            return result

        target = args.get("target") if isinstance(args.get("target"), dict) else {}
        ymm = _ymm(target)
        if ymm is None:
            return result
        task_id = _task_id(result)
        if not task_id:
            return _failure(
                result,
                code="vehicle_anchor_failed",
                message="ScrapeX created the Navigator task without a usable task id.",
            )

        # ``text`` is a bounded field already accepted by ScrapeX's internal
        # Navigator action request.  The model never authors this JSON; it is
        # serialized here from the trusted resolved target.
        payload = json.dumps(ymm, separators=(",", ":"), ensure_ascii=True)
        try:
            acted = await scrapex_module._request(  # noqa: SLF001 - same service boundary
                settings,
                "POST",
                f"/api/navigator/tasks/{quote(task_id, safe='')}/act",
                body={"action": "select_vehicle", "text": payload},
                timeout=scrapex_module.OPERATOR_TIMEOUT,
                may_mutate=True,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed before model navigation
            return _failure(
                result,
                code="vehicle_anchor_failed",
                message=(
                    "ScrapeX could not complete the year/make/model vehicle selection "
                    f"before research ({type(exc).__name__})."
                ),
            )

        action_target = acted.get("action_target") if isinstance(acted, dict) else None
        action_target = action_target if isinstance(action_target, dict) else {}
        if action_target.get("selected") is not True:
            ambiguous = action_target.get("needs_operator") is True
            return _failure(
                result,
                code="vehicle_identity_ambiguous" if ambiguous else "vehicle_anchor_failed",
                message=(
                    "ALLDATA returned multiple plausible configurations for this "
                    "year/make/model; X did not guess a vehicle variant."
                    if ambiguous
                    else "ScrapeX could not mechanically prove the requested year/make/model selection."
                ),
                detail={
                    "reason": acted.get("action_detail") if isinstance(acted, dict) else None,
                    "candidates": action_target.get("candidates"),
                    "observed": action_target,
                },
            )

        # Verify through the provider's independent target signal too.  This
        # catches a click that visually looked successful but did not bind the
        # browser's actual vehicle context.
        signal = await scrapex_module.navigator_current_target_signal(
            settings, "alldata", target
        )
        selected = (
            (signal.get("data") or {}).get("selected")
            if isinstance(signal, dict) and isinstance(signal.get("data"), dict)
            else None
        )
        if selected is not True:
            return _failure(
                result,
                code="vehicle_anchor_failed",
                message=(
                    "ScrapeX clicked a matching Y/M/M candidate, but its independent "
                    "target signal did not prove that vehicle selected."
                ),
                detail=signal,
            )

        # The older anchor intentionally forces the first identity read false
        # so VIN tasks always reselect.  Y/M/M has just been freshly selected
        # here, so consume that one-shot guard and let the agent see the real
        # proven state instead of sending X back through the picker again.
        state["forced"] = True
        state["picker_anchor_verified"] = True
        state["ymm_mechanically_selected"] = True
        state["ymm_selection"] = action_target
        return result

    scrapex_module.navigator = navigator_with_mechanical_ymm
    # research_navigator_agent holds the same module object, so replacing the
    # service function above is enough for its next _run_task call.
    setattr(agent_module, _INSTALLED_ATTR, True)
