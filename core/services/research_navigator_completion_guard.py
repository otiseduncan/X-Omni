"""Prevent X from ending an ALLDATA Navigator task on a routing page.

X owns semantic navigation and chooses every link.  This guard enforces only a
mechanical completion boundary: an ALLDATA vehicle/component landing route is
navigation state, not captured procedure evidence.  If X calls ``done`` there
before submitting any candidate with ``extract``, the completion is refused and
the current rendered page is returned for another reasoning turn.

The guard does not choose a child link, match calibration vocabulary, or decide
whether any page is relevant.  Once a candidate has been extracted/reviewed, the
normal Navigator completion behavior remains available.
"""

from __future__ import annotations

from functools import wraps
from typing import Any
from urllib.parse import urlsplit

_INSTALLED_ATTR = "__xomni_navigator_completion_guard_v1__"
_AGENT_INSTALLED_ATTR = "__xomni_navigator_completion_prompt_v1__"


def _route_text(url: object) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value.casefold()
    return f"{parsed.path}#{parsed.fragment}".casefold()


def is_alldata_routing_page(url: object) -> bool:
    """True for provider routes that identify a vehicle/component landing page.

    Article/guid routes are explicit document routes and are never classified as
    routing pages here.  ``filter/noFilter`` is ALLDATA's component landing route
    seen in the live Repair/Collision Navigator; it exposes child procedure links
    but is not itself the procedure document.
    """

    route = _route_text(url)
    if not route:
        return False
    if "/article/" in route or "/guid/" in route:
        return False
    return "/vehicle/" in route and "/component/" in route and "/filter/nofilter" in route


def _candidate_extracted(verification: Any) -> bool:
    if not isinstance(verification, dict):
        return False
    data = verification.get("data") if isinstance(verification.get("data"), dict) else {}
    return bool(data.get("candidate_extracted"))


def _premature_done_failure(task_id: str, url: str) -> dict[str, Any]:
    message = (
        "Completion refused: the current ALLDATA route is a vehicle/component "
        "landing page, not extracted procedure evidence, and no candidate has "
        "been submitted in this task. Re-read the live controls on this page and "
        "continue navigating. Open a plausible procedure/article from the current "
        "component or provider-native index, read it, and call extract to submit it "
        "for independent review. X chooses which live link to follow; this guard "
        "does not choose one."
    )
    return {
        "service": "ScrapeX",
        "action": "done",
        "status": "invalid_request",
        "success": False,
        "executed": False,
        "verified": False,
        "http_status": 422,
        "error": {"code": "premature_done_on_routing_page", "message": message},
        "detail": {
            "code": "premature_done_on_routing_page",
            "task_id": task_id,
            "url": url,
            "candidate_extracted": False,
            "message": message,
        },
    }


def _install_scrapex(scrapex_module: Any) -> None:
    if getattr(scrapex_module, _INSTALLED_ATTR, False):
        return

    original_navigator = scrapex_module.navigator

    @wraps(original_navigator)
    async def navigator_with_completion_boundary(settings: Any, args: dict[str, Any]):
        action = str(args.get("action") or "").casefold() if isinstance(args, dict) else ""
        task_id = str(args.get("task_id") or "").strip() if isinstance(args, dict) else ""

        if action != "done" or not task_id:
            return await original_navigator(settings, args)

        # Ask the provider task for its own current mechanical facts.  These are
        # reads only; no navigation or semantic interpretation occurs here.
        verification = await original_navigator(
            settings, {"action": "verify", "task_id": task_id}
        )
        if _candidate_extracted(verification):
            return await original_navigator(settings, args)

        observation = await original_navigator(
            settings, {"action": "observe", "task_id": task_id}
        )
        data = observation.get("data") if isinstance(observation, dict) and isinstance(observation.get("data"), dict) else {}
        url = str(data.get("url") or "")
        if is_alldata_routing_page(url):
            return _premature_done_failure(task_id, url)

        return await original_navigator(settings, args)

    scrapex_module.navigator = navigator_with_completion_boundary
    setattr(scrapex_module, _INSTALLED_ATTR, True)


def _install_agent(agent_module: Any) -> None:
    if getattr(agent_module, _AGENT_INSTALLED_ATTR, False):
        return

    original_system_prompt = agent_module._system_prompt

    @wraps(original_system_prompt)
    def system_prompt_with_completion_boundary(*args: Any, **kwargs: Any) -> str:
        base = str(original_system_prompt(*args, **kwargs))
        return base + (
            "\n\nCOMPLETION BOUNDARY: an ALLDATA ADAS Quick Reference row, vehicle/component "
            "landing page, category menu, or procedure list is navigation context, not the "
            "procedure itself. Reaching the requested component is not completion. When the "
            "current live page exposes plausible child procedure/article links for that "
            "component, open and inspect them one at a time. Call extract on a plausible "
            "fully-read procedure so the independent reviewer can judge it. Do not call done "
            "merely because you reached the correct component or system page."
        )

    agent_module._system_prompt = system_prompt_with_completion_boundary

    try:
        function = agent_module.NAVIGATOR_AGENT_TOOL_SCHEMA["function"]
        function["description"] = str(function.get("description") or "") + (
            " Reaching a component landing page or ADAS Quick Reference row is not completion; "
            "follow plausible child procedure links and submit a candidate with extract before "
            "ending when such links are available."
        )
    except (KeyError, TypeError):
        pass

    setattr(agent_module, _AGENT_INSTALLED_ATTR, True)


def install(agent_module: Any, scrapex_module: Any) -> None:
    _install_scrapex(scrapex_module)
    _install_agent(agent_module)
