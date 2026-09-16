"""The Navigator loop enforcing its structural contract, end to end.

Driven against a fake ScrapeX whose URLs, page states, and evidence are set
per test, and a scripted model. Nothing here depends on what a page means:
the loop is judged only on what it dispatched, refused, remembered, and
reported.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from core.services import research_navigator_agent as agent
from core.services import research_navigator_contract as contract
from tests.test_research_navigator_agent import _navigator_result
from tests.test_research_navigator_binding_and_review import (
    OBJECTIVE,
    TARGET,
    _accept,
    _capture,
    _Client,
    _Reviewer,
    _screenshot,
    _target_signal,
)

LANDING = "https://my.alldata.com/repair/#/vehicle/9001/component/77/filter/noFilter"
ARTICLE = "https://my.alldata.com/repair/#/article/9001/component/77/itype/376/nonstandard/1"
PICKER = "https://my.alldata.com/repair/#/select-vehicle"
MANAGED = SimpleNamespace(scrapex_project_path=r"X:\ScrapeX")


class _Site:
    """A ScrapeX whose rendered pages are chosen by the test.

    Pages are named by key ("landing", "article", "picker", or any other key
    for a plain page); ``route`` decides which page each executed action
    lands on. Evidence is keyed by the page extracted,
    so two different pages are two different candidates.
    """

    def __init__(self, *, start: str = "a", route=None, at_bottom: bool = False):
        self.calls: list[dict[str, Any]] = []
        self.tasks = 0
        self.observations = 0
        self.current = start
        self.start = start
        self.route = route or (lambda action, args, current: current)
        self.at_bottom = at_bottom
        self.extracted_from: str | None = None

    def _page(self) -> dict[str, Any]:
        self.observations += 1
        key = self.current
        url = {"landing": LANDING, "article": ARTICLE, "picker": PICKER}.get(key, f"https://my.alldata.com/page/{key}")
        return {
            "observation_id": f"obs_{self.observations}",
            "url": url,
            "title": f"Page {key}",
            "viewport": {"width": 1280, "height": 720},
            "page_text": f"Contents of page {key}.",
            "elements": [{"ref": f"{key}1", "role": "link", "name": f"Link on {key}", "expanded": None}],
            "scroll_position": {"scroll_y": 0, "scroll_height": 2000, "viewport_height": 720, "at_page_bottom": self.at_bottom},
            "loop_warning": None,
            "backtrack_available": True,
        }

    async def __call__(self, settings, args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        self.calls.append(dict(args))
        action = args.get("action")
        if action == "create_task":
            self.tasks += 1
            self.current = self.start
            self.extracted_from = None
            return _navigator_result("create_task", status="created", data={"id": f"task-{self.tasks}", "provider": "alldata", "target": args["target"], "topic": args["topic"]})
        if action == "observe":
            return _navigator_result("observe", status="observed", data=self._page())
        if action == "verify":
            ok = self.extracted_from is not None
            return _navigator_result("verify", status="verified" if ok else "unverified", success=ok, verified=ok, work_complete=ok, data={"vehicle_verified": True, "navigation_performed": ok, "candidate_extracted": ok, "content_extracted": ok, "verified": ok, "reason": None if ok else "no extract", "provider": "alldata"})
        if action == "get_evidence":
            key = self.extracted_from or self.current
            return _navigator_result("get_evidence", status="read", data={"task_id": f"task-{self.tasks}", "provider": "alldata", "source_url": f"https://my.alldata.com/page/{key}", "title": f"Page {key}", "observation_id": f"obs_{self.observations}", "extracted_text": f"Procedure text of page {key}. " * 30, "extracted_text_sha256": key * 64, "referenced_links": [], "verified": True})
        if action == "extract":
            self.extracted_from = self.current
        else:
            self.current = self.route(action, args, self.current)
        data = self._page()
        data["action_executed"] = True
        if action == "select_vehicle":
            _target_signal.selected = True
            data["action_target"] = {"kind": "vehicle", "selected": True, "vin": args.get("vin"), "label": "2025 Kia K4"}
        return _navigator_result(action, status="acted", work_complete=(action == "done"), data=data)

    def actions(self, task: int | None = None) -> list[str]:
        out, current = [], 0
        for call in self.calls:
            if call["action"] == "create_task":
                current += 1
            if task is None or current == task:
                out.append(call["action"])
        return out


def _wire(monkeypatch, site: _Site) -> _Site:
    _capture.calls.clear()
    _screenshot.calls.clear()
    _target_signal.calls.clear()
    _target_signal.selected = False
    monkeypatch.setattr(
        agent,
        "scrapex_svc",
        type("_S", (), {
            "navigator": site,
            "navigator_capture": _capture,
            "navigator_screenshot": _screenshot,
            "navigator_current_target_signal": _target_signal,
        }),
    )
    return site


async def _run(client, reviewer=None, settings=None, **kwargs):
    return await agent.run_navigator_search(
        client=client,
        settings=settings if settings is not None else object(),
        provider="alldata",
        target=TARGET,
        topic="front radar sensor calibration",
        objective=OBJECTIVE,
        reviewer=reviewer or _Reviewer([]),
        **kwargs,
    )


def _tool_receipts(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]


def _refusals(result: dict[str, Any]) -> list[str]:
    return [item["refused"] for item in result["agent_trace"] if item.get("refused")]


def _click_to_b(action: str, args: dict[str, Any], current: str) -> str:
    return "b" if action == "click" else current


def _continue(**extra: Any) -> dict[str, Any]:
    return _accept(classification="GENERAL_DESCRIPTION", decision="CONTINUE_SEARCH", evidence_summary="An overview, not the steps.", **extra)


# ------------------------------------------------------------ completion


@pytest.mark.asyncio
async def test_done_on_a_component_landing_page_is_refused_without_reaching_scrapex(monkeypatch):
    site = _wire(monkeypatch, _Site(start="landing"))
    reviewer = _Reviewer([_continue()])
    client = _Client([[("done", {})], [("extract", {})], [("done", {})]])
    result = await _run(client, reviewer=reviewer, max_turns=12)

    first = site.actions(task=1)
    # The first done never reached ScrapeX; the one after a candidate did.
    assert first.count("done") == 1
    assert first.index("extract") < first.index("done")
    assert "Completion refused" in _tool_receipts(client.messages_seen[1])[-1]["error"]
    assert _refusals(result) == ["premature_done_on_routing_page"]


@pytest.mark.asyncio
async def test_done_on_an_untested_article_route_is_refused(monkeypatch):
    site = _wire(monkeypatch, _Site(start="article"))
    client = _Client([[("done", {})], None, None])
    result = await _run(client, max_turns=4)
    assert "done" not in site.actions(task=1)
    assert "premature_done_on_untested_article" in _refusals(result)


@pytest.mark.asyncio
async def test_done_on_the_vehicle_picker_is_an_explicit_failure_not_refused(monkeypatch):
    site = _wire(monkeypatch, _Site(start="picker"))
    client = _Client([[("done", {})]])
    result = await _run(client, max_turns=4)
    assert "done" in site.actions(task=1)
    assert result["verified"] is False


# ------------------------------------------------------------ end of page


@pytest.mark.asyncio
async def test_a_downward_scroll_at_the_bottom_is_refused_and_an_upward_one_runs(monkeypatch):
    site = _wire(monkeypatch, _Site(start="a", at_bottom=True))
    client = _Client([[("scroll", {"delta_y": 1600})], [("scroll", {"delta_y": -800})], None, None])
    result = await _run(client, max_turns=6)
    scrolls = [call for call in site.calls if call["action"] == "scroll"]
    assert [call["delta_y"] for call in scrolls][:1] == [-800]
    assert _refusals(result)[:1] == ["scroll_at_page_bottom"]


# ------------------------------------------------------------ prose replies


@pytest.mark.asyncio
async def test_a_prose_reply_is_reminded_once_and_the_task_continues(monkeypatch):
    site = _wire(monkeypatch, _Site(start="a", route=_click_to_b))
    client = _Client([None, [("click", {"ref": "a1"})], None, None])
    result = await _run(client, max_turns=6)
    assert "click" in site.actions(task=1)
    assert contract.prose_reminder() in json.dumps(client.messages_seen[1])
    refused = [item for item in result["agent_trace"] if item.get("action") == "prose_reply_refused"]
    assert refused


# ------------------------------------------------------------ cycles


@pytest.mark.asyncio
async def test_a_return_to_an_earlier_page_state_is_a_cycle_not_progress(monkeypatch):
    def toggle(action, args, current):
        return "b" if current == "a" else "a"

    site = _wire(monkeypatch, _Site(start="a", route=toggle))
    client = _Client([[("click", {"ref": "a1"})], [("click", {"ref": "b1"})], None, None])
    result = await _run(client, max_turns=6)

    assert site.actions(task=1).count("click") == 2
    # A -> B -> A: the third page state is the first one again.
    warned = json.dumps(client.messages_seen[2], default=str)
    assert "NAVIGATION CYCLE" in warned
    assert result["research_receipt"]["page_state_revisits"] >= 1


# ------------------------------------------------------------ candidates


@pytest.mark.asyncio
async def test_the_same_rejected_page_is_not_reviewed_twice(monkeypatch):
    _wire(monkeypatch, _Site(start="a"))
    reviewer = _Reviewer([_continue()])
    client = _Client([[("extract", {})], [("extract", {})], None, None])
    result = await _run(client, reviewer=reviewer, max_turns=6)
    assert len(reviewer.calls) == 1
    repeated = [item for item in result["agent_trace"] if item.get("action") == "semantic_review" and item.get("repeated_candidate")]
    assert repeated


@pytest.mark.asyncio
async def test_a_rejected_candidate_keeps_the_search_going_to_an_accepted_one(monkeypatch):
    site = _wire(monkeypatch, _Site(start="a", route=_click_to_b))
    reviewer = _Reviewer([_continue(objective_match="DIFFERENT_COMPONENT"), _accept()])
    client = _Client([[("extract", {})], [("click", {"ref": "a1"})], [("extract", {})]])
    result = await _run(client, reviewer=reviewer, capture=True, max_turns=8)
    assert [call["candidate"]["url"] for call in reviewer.calls] == [
        "https://my.alldata.com/page/a",
        "https://my.alldata.com/page/b",
    ]
    assert result["verified"] is True
    assert len(_capture.calls) == 1
    assert site.tasks == 1


# ------------------------------------------------------------ attempts


@pytest.mark.asyncio
async def test_a_stalled_primary_continues_in_a_fresh_task_that_remembers_rejections(monkeypatch):
    site = _wire(monkeypatch, _Site(start="a", route=_click_to_b))
    reviewer = _Reviewer([_continue(), _accept()])
    client = _Client([
        # attempt 1: submits page a, is turned down, then quits in prose twice
        [("extract", {})], None, None,
        # attempt 2: goes somewhere else and submits that
        [("click", {"ref": "a1"})], [("extract", {})],
    ])
    result = await _run(client, reviewer=reviewer, capture=True, max_turns=30)

    assert site.tasks == 2
    assert result["verified"] is True
    assert result["primary_attempts"] == 2
    second_opening = json.dumps(client.messages_seen[3], default=str)
    assert "attempt 2" in second_opening
    assert "Page a" in second_opening and "NOT accepted" in second_opening
    events = result["research_receipt"]["objective_events"]
    assert events and events[0]["kind"] == "primary_attempt_restarted"


@pytest.mark.asyncio
async def test_attempts_stop_at_the_limit_and_the_failure_says_how_each_ended(monkeypatch):
    _wire(monkeypatch, _Site(start="a"))
    client = _Client([None, None] * contract.MAX_PRIMARY_ATTEMPTS + [[("click", {"ref": "a1"})]])
    result = await _run(client, max_turns=60)
    assert result["primary_attempts"] == contract.MAX_PRIMARY_ATTEMPTS
    assert result["verified"] is False
    reasons = " ".join(result["incomplete_reasons"])
    assert f"{contract.MAX_PRIMARY_ATTEMPTS} primary attempt(s)" in reasons
    assert "model_finished" in reasons


# ------------------------------------------------------------ vehicle


@pytest.mark.asyncio
async def test_every_managed_primary_attempt_reselects_the_exact_vin_and_dependencies_do_not(monkeypatch):
    site = _wire(monkeypatch, _Site(start="a"))
    _target_signal.selected = True  # the browser *looks* like the right vehicle
    reviewer = _Reviewer([
        _accept(decision="ACCEPT_WITH_DEPENDENCIES", dependencies=[{"title": "Wheel Alignment Pre-check", "reason": "Required first.", "quote": "Perform wheel alignment."}]),
        _accept(classification="REQUIRED_SUPPORTING_PROCEDURE", procedure_type="OTHER"),
    ])
    client = _Client([[("extract", {})], [("extract", {})]])
    await _run(client, reviewer=reviewer, settings=MANAGED, capture=True, max_turns=40)

    primary, dependency = site.actions(task=1), site.actions(task=2)
    assert "select_vehicle" in primary
    assert "select_vehicle" not in dependency
    selection = next(call for call in site.calls if call["action"] == "select_vehicle")
    assert selection["vin"] == TARGET["vin"]


# ------------------------------------------------------------ dependency capacity


@pytest.mark.asyncio
async def test_required_dependencies_add_turns_and_a_plain_accept_does_not(monkeypatch):
    _wire(monkeypatch, _Site(start="a"))
    reviewer = _Reviewer([
        _accept(decision="ACCEPT_WITH_DEPENDENCIES", dependencies=[
            {"title": "Wheel Alignment", "reason": "Required first.", "quote": "Align wheels."},
            {"title": "Radar Cover Inspection", "reason": "Required first.", "quote": "Inspect cover."},
        ]),
    ])
    client = _Client([[("extract", {})]])
    result = await _run(client, reviewer=reviewer, max_turns=40)
    events = [e for e in result["research_receipt"]["objective_events"] if e["kind"] == "dependency_capacity_added"]
    assert events and events[0]["objective_turn_limit"] == 40 + 2 * agent.DEPENDENCY_TURNS_EACH

    _wire(monkeypatch, _Site(start="a"))
    plain = _Reviewer([_accept(dependencies=[{"title": "Wheel Alignment", "reason": "Noted.", "quote": "Align."}])])
    result = await _run(_Client([[("extract", {})]]), reviewer=plain, max_turns=40)
    assert not result["research_receipt"]["objective_events"]
    assert all(dep["status"] == "noted" for dep in result["dependencies"])
