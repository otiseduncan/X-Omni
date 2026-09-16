from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.services import research_navigator_completion_guard as guard


def test_component_filter_route_and_document_routes_are_distinct():
    assert guard.is_alldata_routing_page(
        "https://my.alldata.com/repair/#/vehicle/64505/component/7821/filter/noFilter"
    ) is True
    article = "https://my.alldata.com/repair/#/article/64505/component/3399/itype/376/nonstandard/317397/isSelfReferenceLink/false"
    assert guard.is_alldata_routing_page(article) is False
    assert guard.is_alldata_document_route(article) is True
    assert guard.is_alldata_document_route(
        "https://my.alldata.com/repair/#/guid/na-ty2016-2023tacom-RM100000000VEVU_html"
    ) is True


def _scrapex_for(url: str, candidate: dict[str, bool]):
    calls = []

    async def navigator(_settings, args):
        calls.append(dict(args))
        action = args.get("action")
        if action == "verify":
            return {"success": True, "data": {"candidate_extracted": candidate["value"]}}
        if action == "observe":
            return {
                "success": True,
                "data": {"observation_id": "obs-2", "url": url},
            }
        if action == "done":
            return {"success": True, "verified": True, "data": {"done": True}}
        return {"success": True, "data": {}}

    scrapex = SimpleNamespace(navigator=navigator)
    guard._install_scrapex(scrapex)
    return scrapex, calls


def test_done_is_rejected_on_component_landing_until_candidate_exists():
    candidate = {"value": False}
    scrapex, calls = _scrapex_for(
        "https://my.alldata.com/repair/#/vehicle/64505/component/7821/filter/noFilter",
        candidate,
    )
    rejected = asyncio.run(scrapex.navigator(None, {"action": "done", "task_id": "task-1"}))
    assert rejected["success"] is False
    assert rejected["executed"] is False
    assert rejected["detail"]["code"] == "premature_done_on_routing_page"
    assert not any(call.get("action") == "done" for call in calls)

    candidate["value"] = True
    accepted = asyncio.run(scrapex.navigator(None, {"action": "done", "task_id": "task-1"}))
    assert accepted["success"] is True


def test_done_is_rejected_on_untested_article_route():
    candidate = {"value": False}
    scrapex, calls = _scrapex_for(
        "https://my.alldata.com/repair/#/article/64505/component/3399/itype/376/nonstandard/317397/isSelfReferenceLink/false",
        candidate,
    )
    rejected = asyncio.run(scrapex.navigator(None, {"action": "done", "task_id": "task-bsm"}))
    assert rejected["success"] is False
    assert rejected["detail"]["code"] == "premature_done_on_untested_article"
    assert "article/guid" in rejected["error"]["message"]
    assert not any(call.get("action") == "done" for call in calls)

    candidate["value"] = True
    accepted = asyncio.run(scrapex.navigator(None, {"action": "done", "task_id": "task-bsm"}))
    assert accepted["success"] is True


def test_picker_can_finish_without_candidate():
    candidate = {"value": False}
    scrapex, calls = _scrapex_for(
        "https://my.alldata.com/repair/#/select-vehicle",
        candidate,
    )
    result = asyncio.run(scrapex.navigator(None, {"action": "done", "task_id": "task-2"}))
    assert result["success"] is True
    assert calls[-1]["action"] == "done"
