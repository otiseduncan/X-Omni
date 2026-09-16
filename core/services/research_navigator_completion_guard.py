"""Prevent X from ending an ALLDATA Navigator task before evidence is tested.

X owns semantic navigation and chooses every link. This guard enforces only a
mechanical completion boundary: component landing routes are navigation state,
and an ALLDATA article/guid route that has never been submitted with ``extract``
has not yet been tested by the independent reviewer.

The guard does not choose a child link, match calibration vocabulary, or decide
whether a page is relevant. It only prevents ``done`` from replacing the
candidate-submission/review step on a page X deliberately navigated into.
"""

from __future__ import annotations

from functools import wraps
from typing import Any
from urllib.parse import urlsplit

_INSTALLED_ATTR = "__xomni_navigator_completion_guard_v2__"
_AGENT_INSTALLED_ATTR = "__xomni_navigator_completion_prompt_v2__"


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
    route = _route_text(url)
    if not route:
        return False
    if "/article/" in route or "/guid/" in route:
        return False
    return "/vehicle/" in route and "/component/" in route and "/filter/nofilter" in route


def is_alldata_document_route(url: object) -> bool:
    """Whether the live route is an ALLDATA article/guid document context."""
    route = _route_text(url)
    return bool(route and ("/article/" in route or "/guid/" in route))


def _candidate_extracted(verification: Any) -> bool:
    if not isinstance(verification, dict):
        return False
    data = verification.get("data") if isinstance(verification.get("data"), dict) else {}
    return bool(data.get("candidate_extracted"))


def _premature_done_failure(task_id: str, url: str, *, document_route: bool) -> dict[str, Any]:
    if document_route:
        message = (
            "Completion refused: X navigated into an ALLDATA article/guid route but has not "
            "submitted any candidate from this task. An article route can still be a section, "
            "index, introduction, or the actual procedure; the URL alone does not decide that. "
            "Inspect the live page. If it contains the requested executable procedure, read it "
            "and call extract for independent review. If it exposes a deeper repair/procedure "
            "link, follow that link and continue. Do not call done from an untested article page."
        )
        code = "premature_done_on_untested_article"
    else:
        message = (
            "Completion refused: the current ALLDATA route is a vehicle/component landing "
            "page, not extracted procedure evidence, and no candidate has been submitted. "
            "Re-read the live controls and continue navigating. Open a plausible child "
            "procedure/article, read it, and call extract for independent review."
        )
        code = "premature_done_on_routing_page"
    return {
        "service": "ScrapeX",
        "action": "done",
        "status": "invalid_request",
        "success": False,
        "executed": False,
        "verified": False,
        "http_status": 422,
        "error": {"code": code, "message": message},
        "detail": {
            "code": code,
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

        verification = await original_navigator(settings, {"action": "verify", "task_id": task_id})
        if _candidate_extracted(verification):
            return await original_navigator(settings, args)

        observation = await original_navigator(settings, {"action": "observe", "task_id": task_id})
        data = observation.get("data") if isinstance(observation, dict) and isinstance(observation.get("data"), dict) else {}
        url = str(data.get("url") or "")
        if is_alldata_routing_page(url):
            return _premature_done_failure(task_id, url, document_route=False)
        if is_alldata_document_route(url):
            return _premature_done_failure(task_id, url, document_route=True)
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
            "\n\nCOMPLETION BOUNDARY: reaching the requested component is not completion. "
            "An ALLDATA article/guid URL is also not proof by itself; article routes can be "
            "indexes, introductions, section pages, or actual procedures. If the current page "
            "contains executable procedure steps, read it and call extract for independent "
            "review. If it presents a deeper repair/service/procedure link, follow it. Never "
            "call done from an article/guid page that has not been submitted as a candidate."
        )

    agent_module._system_prompt = system_prompt_with_completion_boundary
    try:
        function = agent_module.NAVIGATOR_AGENT_TOOL_SCHEMA["function"]
        function["description"] = str(function.get("description") or "") + (
            " Do not end on an untested ALLDATA article/guid page. Submit the current page "
            "with extract if it is plausibly the procedure, or follow a deeper live procedure "
            "link when the page is only an index/section."
        )
    except (KeyError, TypeError):
        pass
    setattr(agent_module, _AGENT_INSTALLED_ATTR, True)


def install(agent_module: Any, scrapex_module: Any) -> None:
    _install_scrapex(scrapex_module)
    _install_agent(agent_module)
