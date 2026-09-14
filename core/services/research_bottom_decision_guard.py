"""Keep Navigator reasoning alive at the end of a fully-read procedure page.

This is an execution guard, not a semantic router.  X still decides whether the
current page is the requested service-information procedure.  The guard only
records the mechanical fact that the live task is already at the bottom and
refuses another downward scroll on that unchanged task.  That prevents an
impossible browser action from consuming the repeated-no-effect budget before X
gets to make the actual semantic choice: extract this page or leave it.
"""

from __future__ import annotations

from typing import Any

_INSTALLED_ATTR = "__xomni_bottom_decision_guard_installed__"
_AGENT_INSTALLED_ATTR = "__xomni_bottom_decision_prompt_installed__"

_PAGE_CHANGING_ACTIONS = frozenset(
    {"click", "press", "open", "back", "click_mark", "click_visual", "select_vehicle"}
)


def _bottom_from_data(data: Any) -> bool | None:
    if not isinstance(data, dict):
        return None
    position = data.get("scroll_position")
    if not isinstance(position, dict):
        return None
    value = position.get("at_page_bottom")
    return value if type(value) is bool else None


def _bottom_scroll_failure(task_id: str) -> dict[str, Any]:
    message = (
        "The current Navigator observation is already at the bottom of this page. "
        "Another downward scroll cannot reveal new content. X must decide from the "
        "page already observed: call extract if it is the requested procedure, or "
        "leave this page and continue searching if it is not."
    )
    return {
        "service": "ScrapeX",
        "action": "scroll",
        "status": "invalid_request",
        "success": False,
        "executed": False,
        "verified": False,
        "http_status": 422,
        "error": {"code": "scroll_at_page_bottom", "message": message},
        "detail": {
            "code": "scroll_at_page_bottom",
            "task_id": task_id,
            "at_page_bottom": True,
            "message": message,
        },
    }


def _install_scrapex(scrapex_module: Any) -> None:
    if getattr(scrapex_module, _INSTALLED_ATTR, False):
        return

    original_navigator = scrapex_module.navigator
    bottom_tasks: set[str] = set()

    async def navigator_with_bottom_guard(settings: Any, args: dict[str, Any]):
        action = str(args.get("action") or "") if isinstance(args, dict) else ""
        task_id = str(args.get("task_id") or "").strip() if isinstance(args, dict) else ""

        if (
            action == "scroll"
            and task_id
            and task_id in bottom_tasks
            and isinstance(args.get("delta_y"), int)
            and not isinstance(args.get("delta_y"), bool)
            and int(args["delta_y"]) > 0
        ):
            return _bottom_scroll_failure(task_id)

        result = await original_navigator(settings, args)
        if not isinstance(result, dict):
            return result

        data = result.get("data") if isinstance(result.get("data"), dict) else None
        if action == "create_task" and result.get("success") is True and data is not None:
            created_id = str(data.get("id") or data.get("task_id") or "").strip()
            if created_id:
                bottom_tasks.discard(created_id)
            return result

        if not task_id:
            return result

        bottom = _bottom_from_data(data)
        if bottom is True:
            bottom_tasks.add(task_id)
        elif bottom is False:
            bottom_tasks.discard(task_id)
        elif action in _PAGE_CHANGING_ACTIONS and result.get("success") is True:
            # If a successful navigation result omitted a scroll position, do
            # not carry an end-of-page fact across to a potentially new page.
            bottom_tasks.discard(task_id)
        return result

    scrapex_module.navigator = navigator_with_bottom_guard
    setattr(scrapex_module, _INSTALLED_ATTR, True)


def _install_agent(agent_module: Any) -> None:
    if getattr(agent_module, _AGENT_INSTALLED_ATTR, False):
        return

    original_system_prompt = agent_module._system_prompt
    original_observation_summary = agent_module._observation_summary

    def system_prompt_with_bottom_contract(*args: Any, **kwargs: Any) -> str:
        base = str(original_system_prompt(*args, **kwargs))
        return base + (
            "\n\nBROWSER END-OF-PAGE INVARIANT: when the latest observation says "
            "at_page_bottom=true or carries page_bottom_reached, there is no more "
            "content below. Do not request another downward scroll. Make the semantic "
            "decision yourself from the page you have read: if it is the requested "
            "procedure, call extract; if it is not, leave the page and continue the "
            "search. Repeating an unchanged browser action is never a substitute for "
            "that decision."
        )

    def observation_summary_with_bottom_contract(result: dict[str, Any]) -> dict[str, Any]:
        summary = original_observation_summary(result)
        position = summary.get("scroll_position") if isinstance(summary, dict) else None
        if isinstance(position, dict) and position.get("at_page_bottom") is True:
            summary["bottom_decision_contract"] = {
                "scroll_down_allowed": False,
                "decision_required": True,
                "instruction": (
                    "The entire current page has been reached. Decide whether it is the "
                    "requested procedure. If yes, call extract now. If no, leave this page "
                    "and continue searching. Do not scroll down again."
                ),
            }
        return summary

    agent_module._system_prompt = system_prompt_with_bottom_contract
    agent_module._observation_summary = observation_summary_with_bottom_contract

    try:
        function = agent_module.NAVIGATOR_AGENT_TOOL_SCHEMA["function"]
        function["description"] = str(function.get("description") or "") + (
            " Once the latest observation reports the bottom of the page, another "
            "downward scroll is invalid; decide whether to extract or leave the page."
        )
        delta = function["parameters"]["properties"]["delta_y"]
        delta["description"] = str(delta.get("description") or "") + (
            " Do not use a positive value after at_page_bottom=true."
        )
    except (KeyError, TypeError):
        # The prompt and runtime guard are authoritative; schema wording is only
        # an additional model-facing reminder if this schema shape is present.
        pass

    setattr(agent_module, _AGENT_INSTALLED_ATTR, True)


def install(agent_module: Any, scrapex_module: Any) -> None:
    """Install the mechanical bottom guard and the model-facing decision contract."""
    _install_scrapex(scrapex_module)
    _install_agent(agent_module)
