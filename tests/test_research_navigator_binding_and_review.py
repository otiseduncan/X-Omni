"""The Navigator's hands and its second pair of eyes.

Observation-bound actions (ref, mark, visual), stale-target refusals that
come back as fresh observations, the isolated semantic reviewer that decides
acceptance, dependency pursuit, progress-aware budgets, and the research
receipt -- all driven against a fake ScrapeX and a scripted model.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from core.services import research_navigator_agent as agent
from core.services import research_semantic_review as review_mod
from tests.test_research_navigator_agent import _navigator_result


def _accept(**overrides: Any) -> dict[str, Any]:
    verdict = {
        "classification": "ACTUAL_PROCEDURE",
        "procedure_type": "STATIC_RADAR",
        "vehicle_match": "MATCHES",
        "evidence": {field: "PRESENT" for field in review_mod.EVIDENCE_FIELDS},
        "dependencies": [],
        "decision": "ACCEPT",
        "confidence": 0.9,
        "evidence_summary": "Radar aiming steps with target distance.",
        "malformed": False,
    }
    verdict.update(overrides)
    return verdict


class _Reviewer:
    """A scripted reviewer that records exactly what it was shown."""

    def __init__(self, verdicts: list[dict[str, Any]]):
        self.verdicts = list(verdicts)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.verdicts:
            return self.verdicts.pop(0)
        return _accept()


class _Navigator:
    """A ScrapeX with observation ids, marks, and stale refusals."""

    def __init__(self, *, stale_once: str | None = None, marks: bool = True):
        self.calls: list[dict[str, Any]] = []
        self.observations = 0
        self.tasks = 0
        self.captures: list[dict[str, Any]] = []
        self.stale_once = stale_once
        self.marks = marks
        self.extracted = False

    def _page(self, *, with_marks: bool = False, url: str = "https://my.alldata.com/page") -> dict[str, Any]:
        self.observations += 1
        data = {
            "observation_id": f"obs_{self.observations}",
            "page_identity": "pid",
            "url": url,
            "title": "Front Radar (ADAS) - Adjustment",
            "viewport": {"width": 1280, "height": 720},
            "page_text": "Install the reflector 2.5 m from the bumper. Front radar adjustment steps.",
            "breadcrumb": ["Collision Avoidance Sensor", "Adjustment"],
            "elements": [
                {"ref": "e1", "role": "link", "name": "Adjustment", "expanded": None},
                {"ref": "e2", "role": "searchbox", "name": "Search", "expanded": None},
            ],
            "controls_without_refs": 1,
            "loop_warning": None,
            "backtrack_available": True,
        }
        if with_marks and self.marks:
            data["marks"] = [{"mark": 21, "tag": "span", "name": "print", "classes": "icon-print", "box": [1200, 40, 24, 24]}]
        return data

    async def __call__(self, settings, args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        self.calls.append(dict(args))
        action = args.get("action")
        if action == "create_task":
            self.tasks += 1
            return _navigator_result("create_task", status="created", data={"id": f"task-{self.tasks}", "provider": "alldata", "target": args["target"], "topic": args["topic"]})
        if action == "observe":
            return _navigator_result("observe", status="observed", data=self._page(with_marks=bool(args.get("marks"))))
        if action in {"click", "fill", "type", "press", "click_mark", "click_visual", "select_vehicle", "back", "open", "scroll", "wait", "extract", "done"}:
            if self.stale_once and action == self.stale_once:
                self.stale_once = None
                return {
                    "service": "ScrapeX", "action": action, "status": "conflict", "success": False,
                    "executed": False, "verified": False, "http_status": 409,
                    "error": {"code": "remote_error", "message": "ScrapeX refused the action."},
                    "detail": {"code": "stale_target", "message": "'e1' moved from where it was observed.", "observed_box": {"x": 1, "y": 1, "w": 2, "h": 2}},
                }
            if action == "extract":
                self.extracted = True
            data = self._page(url=f"https://my.alldata.com/page/{self.observations + 1}")
            data["action_executed"] = True
            data["is_search_action"] = action in {"fill", "type", "select_vehicle"}
            if action in {"click_mark", "click_visual"}:
                data["action_target"] = {"kind": "mark" if action == "click_mark" else "visual", "observation_id": args.get("observation_id")}
            if action == "select_vehicle":
                data["action_target"] = {"kind": "vehicle", "selected": True, "vin": args.get("vin"), "label": "2025 Kia K4"}
            return _navigator_result(action, status="acted", work_complete=(action == "done"), data=data)
        if action == "verify":
            verified = self.extracted
            return _navigator_result("verify", status="verified" if verified else "unverified", success=verified, verified=verified, work_complete=verified, data={"vehicle_verified": True, "subject_verified": verified, "procedure_leaf_verified": verified, "content_extracted": verified, "verified": verified, "reason": None if verified else "no extract yet", "provider": "alldata", "source_url": "https://my.alldata.com/page/3", "title": "Front Radar (ADAS) - Adjustment"})
        if action == "get_evidence":
            return _navigator_result("get_evidence", status="read", data={"task_id": f"task-{self.tasks}", "provider": "alldata", "source_url": "https://my.alldata.com/page/3", "title": "Front Radar (ADAS) - Adjustment", "observation_id": f"obs_{self.observations}", "breadcrumb": ["Collision Avoidance Sensor", "Adjustment"], "extracted_text": "Install the reflector 2.5 m from the bumper. " * 40, "extracted_text_sha256": "a" * 64, "referenced_links": ["Removal and Replacement", "Wheel Alignment"], "verified": self.extracted})
        raise AssertionError(f"unexpected action {action}")


async def _capture(settings, task_id, **kwargs):  # noqa: ARG001
    _capture.calls.append({"task_id": task_id, **kwargs})
    return {"success": True, "verified": True, "work_complete": True, "status": "captured", "data": {"task_id": task_id, "relative_path": f"2025/Kia/K4/{task_id}.pdf", "sha256": "f" * 64, "text_sidecar": f"2025/Kia/K4/{task_id}.text.txt", "title": "Front Radar (ADAS) - Adjustment"}}


_capture.calls = []


async def _screenshot(settings, task_id, observation_id=None):  # noqa: ARG001
    _screenshot.calls.append((task_id, observation_id))
    return b"\xff\xd8\xfffake", "image/jpeg"


_screenshot.calls = []


class _Client:
    def __init__(self, turns: list[list[tuple[str, dict]] | None]):
        self._turns = list(turns)
        self.messages_seen: list[list[dict[str, Any]]] = []

    async def stream(self, messages, tools=None, max_tokens=None, tool_choice=None):  # noqa: ARG002
        self.messages_seen.append(copy.deepcopy(list(messages)))
        if not self._turns:
            return
        turn = self._turns.pop(0)
        if turn is None:
            yield {"type": "content", "text": "Done."}
            return
        for index, (action, args) in enumerate(turn):
            yield {"type": "tool_call", "id": f"call_{index}", "name": "navigator_browse", "arguments": json.dumps({"action": action, **args})}
        yield {"type": "usage", "usage": {"prompt_tokens": 4000 + 100 * len(self.messages_seen)}, "timings": {}}


@pytest.fixture
def wired(monkeypatch):
    navigator = _Navigator()
    _capture.calls.clear()
    _screenshot.calls.clear()
    monkeypatch.setattr(agent, "scrapex_svc", type("_S", (), {"navigator": navigator, "navigator_capture": _capture, "navigator_screenshot": _screenshot}))
    return navigator


TARGET = {"year": 2025, "make": "Kia", "model": "K4", "vin": "KNAF24A28S5000001"}
OBJECTIVE = {"objective": "front radar sensor calibration", "system": "Front Radar Sensor - SCC / AEB / FCW", "component": "front radar", "repair_order": "2400711902"}


async def _run(client, reviewer=None, **kwargs):
    return await agent.run_navigator_search(client=client, settings=object(), provider="alldata", target=TARGET, topic="front radar sensor calibration", objective=OBJECTIVE, reviewer=reviewer or _Reviewer([]), **kwargs)


# ------------------------------------------------------------ binding


@pytest.mark.asyncio
async def test_ref_actions_carry_the_observation_they_were_chosen_from(wired):
    client = _Client([[("click", {"ref": "e1"})], [("type", {"ref": "e2", "text": "KNAF24A28S5000001"})], None])
    await _run(client)
    click = next(call for call in wired.calls if call["action"] == "click")
    typed = next(call for call in wired.calls if call["action"] == "type")
    assert click["observation_id"] == "obs_1"
    # After the click a new observation arrived; the next action binds to it.
    assert typed["observation_id"] != "obs_1"
    assert typed["text"] == "KNAF24A28S5000001"
    # Screenshots are requested for exactly the observation they accompany.
    assert _screenshot.calls[0] == ("task-1", "obs_1")


@pytest.mark.asyncio
async def test_missing_ref_can_fall_back_to_marks_then_a_visual_point(wired):
    client = _Client([
        [("observe_marks", {})],
        [("click_mark", {"mark": 21})],
        [("click_visual", {"x_norm": 0.95, "y_norm": 0.06})],
        None,
    ])
    await _run(client)
    observe_marks = [call for call in wired.calls if call["action"] == "observe" and call.get("marks")]
    assert observe_marks, "observe_marks must ask ScrapeX for marks"
    marked_observation = observe_marks[-1]
    click_mark = next(call for call in wired.calls if call["action"] == "click_mark")
    assert click_mark["mark"] == 21
    assert click_mark["observation_id"].startswith("obs_")
    visual = next(call for call in wired.calls if call["action"] == "click_visual")
    assert visual["x_norm"] == 0.95 and visual["y_norm"] == 0.06
    assert visual["observation_id"].startswith("obs_")
    # The model was shown the marks it could click.
    marks_message = client.messages_seen[1][-1]["content"]
    shown = marks_message[0]["text"] if isinstance(marks_message, list) else marks_message
    assert '"mark": 21' in shown and "icon-print" in shown
    del marked_observation


@pytest.mark.asyncio
async def test_visual_actions_require_an_observation_identity():
    assert agent._validate_args("click_visual", {"x_norm": 1.5, "y_norm": 0.2})
    assert agent._validate_args("click_mark", {"mark": 0})
    assert agent._validate_args("click_mark", {}) is not None
    assert agent._validate_args("select_vehicle", {}) is not None
    assert agent._validate_args("click_visual", {"x_norm": 0.5, "y_norm": 0.5}) is None


@pytest.mark.asyncio
async def test_stale_target_refusal_returns_a_fresh_observation_never_a_substitute_click(monkeypatch):
    navigator = _Navigator(stale_once="click")
    _screenshot.calls.clear()
    monkeypatch.setattr(agent, "scrapex_svc", type("_S", (), {"navigator": navigator, "navigator_screenshot": _screenshot}))
    client = _Client([[("click", {"ref": "e1"})], None])
    result = await _run(client)
    # Exactly one click was attempted and refused; nothing else was clicked
    # on the model's behalf.
    clicks = [call for call in navigator.calls if call["action"] in {"click", "click_mark", "click_visual"}]
    assert len(clicks) == 1
    receipt = json.loads([m for m in client.messages_seen[1] if m.get("role") == "tool"][-1]["content"])
    assert "stale_target" in receipt["error"] or "moved" in receipt["error"]
    assert "observe_marks" in receipt["fallback_hint"]
    # The model was handed a fresh observation to choose from again.
    heading = json.dumps(client.messages_seen[1][-1], default=str)
    assert "rejected and did not execute" in heading
    assert result["research_receipt"]["stale_action_rejections"] == 1


@pytest.mark.asyncio
async def test_select_vehicle_fast_path_is_dispatched_with_the_vin(wired):
    client = _Client([[("select_vehicle", {"vin": "KNAF24A28S5000001"})], None])
    await _run(client)
    call = next(call for call in wired.calls if call["action"] == "select_vehicle")
    assert call == {"action": "select_vehicle", "task_id": "task-1", "vin": "KNAF24A28S5000001"}
    prompt = client.messages_seen[0][0]["content"]
    assert "VIN: KNAF24A28S5000001" in prompt
    assert "Historical provider notes (non-binding" in prompt


# ------------------------------------------------------------- critic


@pytest.mark.asyncio
async def test_reviewer_gets_the_objective_and_evidence_but_never_the_navigator_transcript(wired):
    reviewer = _Reviewer([_accept()])
    client = _Client([[("click", {"ref": "e1"})], [("extract", {})], None])
    result = await _run(client, reviewer=reviewer, capture=True)
    assert result["verified"] is True and result["status"] == "verified"
    assert len(reviewer.calls) == 1
    shown = reviewer.calls[0]
    assert shown["objective"]["objective"] == "front radar sensor calibration"
    assert shown["objective"]["system"] == "Front Radar Sensor - SCC / AEB / FCW"
    assert shown["vehicle"] == TARGET
    assert shown["candidate"]["title"] == "Front Radar (ADAS) - Adjustment"
    assert shown["candidate"]["url"] == "https://my.alldata.com/page/3"
    assert "reflector" in shown["candidate"]["text"]
    assert shown["candidate"]["referenced_links"] == ["Removal and Replacement", "Wheel Alignment"]
    assert shown["screenshot"] is not None
    assert "messages" not in shown and "history" not in shown
    # Review happens after ScrapeX's mechanical verify, before the loop stops.
    actions = [call["action"] for call in wired.calls]
    assert actions.index("verify") < actions.index("get_evidence")
    # An acceptance ends the loop: no further model call is made, and the
    # verdict goes into the capture and the trace rather than the transcript.
    assert len(client.messages_seen) == 2
    review_trace = [item for item in result["agent_trace"] if item.get("action") == "semantic_review"]
    assert review_trace and review_trace[0]["decision"] == "ACCEPT"
    assert _capture.calls[0]["semantic_review"]["decision"] == "ACCEPT"
    assert result["documents"][0]["captured"] is True
    assert result["documents"][0]["artifact"]["sha256"] == "f" * 64


@pytest.mark.asyncio
async def test_review_reject_and_continue_keep_the_loop_going_and_never_accept(wired):
    reviewer = _Reviewer([
        _accept(classification="REMOVAL_REPLACEMENT", decision="REJECT", evidence_summary="This is removal and installation."),
        _accept(classification="GENERAL_DESCRIPTION", decision="CONTINUE_SEARCH", evidence_summary="An overview."),
    ])
    client = _Client([[("extract", {})], [("back", {})], [("extract", {})], None])
    result = await _run(client, reviewer=reviewer, capture=True)
    assert result["verified"] is False
    assert result["captured"] is False
    assert _capture.calls == []
    assert [call["decision"] for call in result["research_receipt"]["critic_decisions"]] == ["REJECT", "CONTINUE_SEARCH"]
    second_turn = json.dumps(client.messages_seen[1], default=str)
    assert "REJECTED" in second_turn and "removal and installation" in second_turn
    assert "semantic review did not accept" in " ".join(result["incomplete_reasons"]).casefold()


@pytest.mark.asyncio
async def test_uncertain_or_malformed_review_never_becomes_an_acceptance(wired):
    reviewer = _Reviewer([review_mod.malformed_review("prose instead of a verdict")])
    client = _Client([[("extract", {})], None])
    result = await _run(client, reviewer=reviewer, capture=True)
    assert result["verified"] is False
    assert result["status"] == "uncertain"
    assert result["captured"] is False and _capture.calls == []
    assert result["research_receipt"]["critic_decisions"][0]["malformed"] is True


@pytest.mark.asyncio
async def test_accepted_with_dependencies_pursues_each_as_its_own_task(wired):
    reviewer = _Reviewer([
        _accept(decision="ACCEPT_WITH_DEPENDENCIES", dependencies=[{"title": "Wheel Alignment Pre-check", "reason": "The aiming procedure requires alignment first."}]),
        _accept(classification="REQUIRED_SUPPORTING_PROCEDURE", procedure_type="OTHER"),
    ])
    client = _Client([[("extract", {})], [("click", {"ref": "e1"})], [("extract", {})], None])
    result = await _run(client, reviewer=reviewer, capture=True)
    assert result["status"] == "verified" and result["complete"] is True
    created = [call for call in wired.calls if call["action"] == "create_task"]
    assert [call["topic"] for call in created] == ["front radar sensor calibration", "Wheel Alignment Pre-check"]
    dependency = result["dependencies"][0]
    assert dependency["status"] == "resolved"
    assert dependency["reason"].startswith("The aiming procedure")
    assert dependency["originating_document"] == "Front Radar (ADAS) - Adjustment"
    assert dependency["resolved_artifact"]["sha256"] == "f" * 64
    assert [doc["role"] for doc in result["documents"]] == ["primary", "dependency"]
    # The dependency's reviewer call carries why the objective needs it.
    assert "Wheel Alignment Pre-check" in reviewer.calls[1]["objective"]["dependency_context"]
    assert len(_capture.calls) == 2
    assert result["task_ids"] == ["task-1", "task-2"]


@pytest.mark.asyncio
async def test_unresolved_dependency_is_reported_as_incompleteness(wired):
    reviewer = _Reviewer([
        _accept(decision="ACCEPT_WITH_DEPENDENCIES", dependencies=[{"title": "Target Setup", "reason": "Needed to place the target."}]),
    ])
    client = _Client([[("extract", {})], None])
    result = await _run(client, reviewer=reviewer, capture=True)
    assert result["verified"] is True
    assert result["status"] == "incomplete" and result["complete"] is False
    assert result["dependencies"][0]["status"] == "unresolved"
    assert any("Target Setup" in reason for reason in result["incomplete_reasons"])


@pytest.mark.asyncio
async def test_a_dependency_the_reviewer_rejects_on_sight_is_dismissed_not_missing(wired):
    reviewer = _Reviewer([
        _accept(decision="ACCEPT_WITH_DEPENDENCIES", dependencies=[{"title": "Removal and Replacement", "reason": "linked at the foot of the page"}]),
        _accept(classification="REMOVAL_REPLACEMENT", decision="REJECT", evidence_summary="Removal steps only; not needed to perform the aiming."),
    ])
    client = _Client([[("extract", {})], [("extract", {})], None])
    result = await _run(client, reviewer=reviewer, capture=True)
    assert result["dependencies"][0]["status"] == "dismissed"
    assert "not needed" in result["dependencies"][0]["reason_dismissed"]
    assert result["status"] == "verified" and result["complete"] is True
    assert result["incomplete_reasons"] == []
    # Only the accepted procedure was filed.
    assert [call["task_id"] for call in _capture.calls] == ["task-1"]


@pytest.mark.asyncio
async def test_documents_listed_under_a_plain_accept_are_noted_not_pursued(wired):
    reviewer = _Reviewer([_accept(dependencies=[{"title": "Wheel Alignment", "reason": "related information", "quote": "Related information: Wheel Alignment"}])])
    client = _Client([[("extract", {})], None])
    result = await _run(client, reviewer=reviewer)
    assert result["status"] == "verified" and result["complete"] is True
    assert result["dependencies"][0]["status"] == "noted"
    assert [call["topic"] for call in wired.calls if call["action"] == "create_task"] == ["front radar sensor calibration"]


@pytest.mark.asyncio
async def test_dependency_limit_is_a_resource_bound_not_a_depth_rule(wired):
    dependencies = [{"title": f"Doc {index}", "reason": "needed"} for index in range(5)]
    reviewer = _Reviewer([_accept(decision="ACCEPT_WITH_DEPENDENCIES", dependencies=dependencies)])
    client = _Client([[("extract", {})]] + [None] * 10)
    result = await _run(client, reviewer=reviewer, max_dependencies=2)
    statuses = [dep["status"] for dep in result["dependencies"]]
    assert statuses.count("not_pursued") == 3
    assert result["status"] == "incomplete"
    assert any("dependency limit" in reason for reason in result["incomplete_reasons"])


@pytest.mark.asyncio
async def test_follow_dependency_ends_the_primary_task_and_pursues_the_named_document(wired):
    reviewer = _Reviewer([
        _accept(classification="GENERAL_DESCRIPTION", decision="FOLLOW_DEPENDENCY", dependencies=[{"title": "Front Radar Aiming", "reason": "This overview names the aiming procedure."}]),
        _accept(),
    ])
    client = _Client([[("extract", {})], [("extract", {})], None])
    result = await _run(client, reviewer=reviewer)
    created = [call["topic"] for call in wired.calls if call["action"] == "create_task"]
    assert created == ["front radar sensor calibration", "Front Radar Aiming"]
    assert result["documents"][0]["accepted"] is False
    assert result["documents"][1]["accepted"] is True
    assert result["verified"] is True


@pytest.mark.asyncio
async def test_review_can_be_switched_off_for_mechanical_comparison(wired):
    reviewer = _Reviewer([])
    client = _Client([[("extract", {})], None])
    result = await _run(client, reviewer=reviewer, review=False)
    assert reviewer.calls == []
    assert result["verified"] is True
    assert result["provenance"]["semantic_review"] is False


# ----------------------------------------------------------- budgets


@pytest.mark.asyncio
async def test_repeated_non_progress_stalls_before_the_hard_ceiling(monkeypatch):
    class _StuckNavigator(_Navigator):
        def _page(self, *, with_marks=False, url="https://my.alldata.com/page"):
            self.observations += 1
            return {
                "observation_id": "obs_same", "url": "https://my.alldata.com/same", "title": "Same",
                "viewport": {"width": 10, "height": 10}, "page_text": "same page",
                "elements": [{"ref": "e1", "role": "link", "name": "Same", "expanded": None}],
                "loop_warning": None, "backtrack_available": False,
            }

    navigator = _StuckNavigator()
    monkeypatch.setattr(agent, "scrapex_svc", type("_S", (), {"navigator": navigator}))
    client = _Client([[("scroll", {"delta_y": 1600})]] * 30)
    result = await _run(client, max_turns=30)
    assert result["agent_stopped_reason"] == "stalled"
    assert result["research_receipt"]["metrics"]["turns_used"] < 30
    assert "stalled" in " ".join(result["incomplete_reasons"])


@pytest.mark.asyncio
async def test_receipt_records_actions_observations_urls_decisions_and_artifacts(wired):
    reviewer = _Reviewer([_accept()])
    client = _Client([[("click", {"ref": "e1"})], [("extract", {})], None])
    result = await _run(client, reviewer=reviewer, capture=True)
    receipt = result["research_receipt"]
    assert receipt["objective"]["objective"] == "front radar sensor calibration"
    assert receipt["objective"]["vehicle"]["vin"] == TARGET["vin"]
    assert receipt["provider"] == "alldata"
    assert receipt["task_ids"] == ["task-1"]
    assert [item["action"] for item in receipt["actions"]] == ["click", "extract"]
    assert receipt["actions"][0]["observation_id"] == "obs_1"
    assert len(receipt["observation_ids"]) >= 2
    assert "https://my.alldata.com/page" in receipt["visited_urls"]
    assert receipt["candidates"][0]["mechanically_verified"] is True
    assert receipt["critic_decisions"][0]["decision"] == "ACCEPT"
    assert receipt["artifacts"][0]["sha256"] == "f" * 64
    assert receipt["final_status"] == "verified"
    assert receipt["incomplete_reasons"] == []
    assert receipt["metrics"]["model_calls"] == 2
    assert receipt["metrics"]["prompt_tokens_max"] >= 4000
    # No hidden reasoning: the receipt carries summaries, not the transcript.
    assert "messages" not in json.dumps(receipt)


@pytest.mark.asyncio
async def test_a_second_run_while_one_holds_the_browser_is_refused_not_queued(wired):
    async with agent.NAVIGATOR_LOCK:
        result = await _run(_Client([None]))
    assert result["status"] == "navigator_busy"
    assert result["attempted"] is False
