from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.services import research_navigator_completion_guard as guard


def test_component_filter_route_is_routing_page_but_article_is_not():
    assert guard.is_alldata_routing_page(
        "https://my.alldata.com/repair/#/vehicle/64505/component/7821/filter/noFilter"
    ) is True
    assert guard.is_alldata_routing_page(
        "https://my.alldata.com/repair/#/article/64505/component/7821/itype/5/nonstandard/123"
    ) is False
    assert guard.is_alldata_routing_page(
        "https://my.alldata.com/repair/#/guid/na-ty2016-2023tacom-RM100000000VEVU_html"
    ) is False


def test_done_is_rejected_on_routing_page_until_candidate_exists():
    calls = []
    candidate = {"value": False}

    async def navigator(_settings, args):
        calls.append(dict(args))
        action = args.get("action")
        if action == "verify":
            return {"success": True, "data": {"candidate_extracted": candidate["value"]}}
        if action == "observe":
            return {
                "success": True,
                "data": {
                    "observation_id": "obs-2",
                    "url": "https://my.alldata.com/repair/#/vehicle/64505/component/7821/filter/noFilter",
                },
            }
        if action == "done":
            return {"success": True, "verified": True, "data": {"done": True}}
        return {"success": True, "data": {}}

    scrapex = SimpleNamespace(navigator=navigator)
    guard._install_scrapex(scrapex)

    rejected = asyncio.run(scrapex.navigator(None, {"action": "done", "task_id": "task-1"}))
    assert rejected["success"] is False
    assert rejected["executed"] is False
    assert rejected["detail"]["code"] == "premature_done_on_routing_page"
    assert not any(call.get("action") == "done" for call in calls)

    candidate["value"] = True
    accepted = asyncio.run(scrapex.navigator(None, {"action": "done", "task_id": "task-1"}))
    assert accepted["success"] is True
    assert any(call.get("action") == "done" for call in calls)


def test_nonrouting_page_can_finish_without_candidate():
    calls = []

    async def navigator(_settings, args):
        calls.append(dict(args))
        if args.get("action") == "verify":
            return {"success": True, "data": {"candidate_extracted": False}}
        if args.get("action") == "observe":
            return {
                "success": True,
                "data": {"url": "https://my.alldata.com/repair/#/select-vehicle"},
            }
        return {"success": True, "verified": True, "data": {"done": True}}

    scrapex = SimpleNamespace(navigator=navigator)
    guard._install_scrapex(scrapex)
    result = asyncio.run(scrapex.navigator(None, {"action": "done", "task_id": "task-2"}))
    assert result["success"] is True
    assert calls[-1]["action"] == "done"
