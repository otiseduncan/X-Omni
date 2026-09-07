"""Tests for the model-driven Navigator ALLDATA agent loop.

These exercise the control flow (task creation, tool dispatch, turn budget,
repeated-failure detection) and, most importantly, that verification always
comes from ScrapeX's own verify action -- never from the model's own narration --
by driving a fake scrapex.navigator() rather than a
real ScrapeX service.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.services import research_navigator_agent


def _navigator_result(action: str, *, data: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    result = {
        "service": "ScrapeX",
        "action": action,
        "status": "ok",
        "success": True,
        "executed": True,
        "verified": True,
        "data": data,
    }
    result.update(overrides)
    return result


class _FakeNavigator:
    """Stands in for core.services.scrapex.navigator(settings, args)."""

    def __init__(self, *, create_ok: bool = True):
        self.calls: list[dict[str, Any]] = []
        self._create_ok = create_ok
        self.verified_after_extract = False
        self.verification_sequence: list[bool] = []
        self._extracted = False
        self.extract_count = 0

    async def __call__(self, settings, args: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        self.calls.append(dict(args))
        action = args.get("action")

        if action == "create_task":
            if not self._create_ok:
                return {
                    "service": "ScrapeX",
                    "action": "create_task",
                    "status": "invalid_request",
                    "success": False,
                    "executed": False,
                    "verified": False,
                    "error": {"code": "invalid_request", "message": "boom"},
                }
            return _navigator_result(
                "create_task",
                status="created",
                data={
                    "id": "task-1",
                    "provider": args["provider"],
                    "target": args["target"],
                    "topic": args["topic"],
                },
            )

        if action == "observe":
            return _navigator_result(
                "observe",
                status="observed",
                data={
                    "url": "https://my.alldata.com/vehicle-select",
                    "title": "Vehicle Select",
                    "elements": [
                        {"ref": "e1", "role": "textbox", "name": "Vehicle search", "expanded": None},
                        {"ref": "e2", "role": "button", "name": "Search", "expanded": None},
                    ],
                    "loop_warning": None,
                    "backtrack_available": False,
                },
            )

        if action in {"click", "fill", "press", "back", "open", "scroll", "wait", "extract", "done"}:
            if action == "extract":
                self._extracted = True
                self.extract_count += 1
            return _navigator_result(
                action,
                status="acted",
                work_complete=(action == "done"),
                data={
                    "url": "https://my.alldata.com/leaf",
                    "title": "Procedure",
                    "elements": [{"ref": "e9", "role": "heading", "name": "Procedure", "expanded": None}],
                    "loop_warning": None,
                    "backtrack_available": True,
                    "action_executed": True,
                    "is_search_action": action == "fill",
                },
            )

        if action == "verify":
            verified = (
                self.verification_sequence.pop(0)
                if self.verification_sequence
                else bool(self.verified_after_extract and self._extracted)
            )
            return _navigator_result(
                "verify",
                status="verified" if verified else "unverified",
                success=verified,
                verified=verified,
                work_complete=verified,
                data={
                    "vehicle_verified": True,
                    "subject_verified": verified,
                    "procedure_leaf_verified": verified,
                    "content_extracted": verified,
                    "verified": verified,
                    "reason": None if verified else "Not enough evidence.",
                    "provider": "alldata",
                },
            )

        if action == "get_evidence":
            return _navigator_result(
                "get_evidence",
                status="read",
                data={
                    "task_id": "task-1",
                    "provider": "alldata",
                    "source_url": "https://my.alldata.com/leaf",
                    "extracted_text": "Blind spot monitor calibration procedure text.",
                    "verified": bool(self.verified_after_extract and self._extracted),
                },
            )

        raise AssertionError(f"unexpected navigator action: {action}")


class _ScriptedClient:
    """Yields one scripted turn's worth of tool_call events per .stream() call."""

    def __init__(self, turns: list[list[tuple[str, dict]] | None]):
        self._turns = list(turns)
        self.messages_seen: list[list[dict[str, Any]]] = []

    async def stream(self, messages, tools=None, max_tokens=None):  # noqa: ARG002
        self.messages_seen.append(list(messages))
        if not self._turns:
            return
        turn = self._turns.pop(0)
        if turn is None:
            yield {"type": "content", "text": "Done."}
            return
        for index, (action, args) in enumerate(turn):
            payload = {"action": action, **args}
            yield {
                "type": "tool_call",
                "id": f"call_{index}",
                "name": "navigator_browse",
                "arguments": json.dumps(payload),
            }


@pytest.mark.asyncio
async def test_happy_path_reaches_verified_via_scrapex_verify_not_model_narration(monkeypatch):
    navigator = _FakeNavigator()
    navigator.verified_after_extract = True
    monkeypatch.setattr(research_navigator_agent, "scrapex_svc", type("_S", (), {"navigator": navigator}))

    client = _ScriptedClient([
        [("fill", {"ref": "e1", "text": "2023 Toyota Camry"}), ("click", {"ref": "e2"})],
        [("extract", {})],
        [("done", {})],
    ])

    result = await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2023, "make": "Toyota", "model": "Camry"},
        topic="blind spot monitor calibration",
    )

    assert result["attempted"] is True
    assert result["verified"] is True
    assert result["captured"] is False
    assert result["agent_stopped_reason"] == "verified_after_extract"
    assert result["task_id"] == "task-1"
    assert result["source_url"] == "https://my.alldata.com/leaf"
    assert "calibration procedure" in result["extracted_text"]

    actions_called = [call["action"] for call in navigator.calls]
    assert actions_called[0] == "create_task"
    assert actions_called[1] == "observe"
    assert "verify" in actions_called
    assert "get_evidence" in actions_called
    # Candidate verification happens immediately after extract, while the
    # browser context is still live; a final verify still runs as the
    # authoritative epilogue before evidence/capture.
    extract_index = actions_called.index("extract")
    verify_indexes = [i for i, action in enumerate(actions_called) if action == "verify"]
    assert verify_indexes[0] == extract_index + 1
    assert "done" not in actions_called


@pytest.mark.asyncio
async def test_verified_navigation_captures_only_when_explicitly_requested(monkeypatch):
    navigator = _FakeNavigator()
    navigator.verified_after_extract = True
    captured_tasks: list[str] = []

    async def capture(_settings, task_id):
        captured_tasks.append(task_id)
        return {
            "success": True,
            "verified": True,
            "work_complete": True,
            "data": {
                "task_id": task_id,
                "relative_path": "2023/Toyota/Camry/ALLDATA/Procedure.pdf",
            },
        }

    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator, "navigator_capture": capture}),
    )
    client = _ScriptedClient([
        [("extract", {})],
        [("done", {})],
    ])

    result = await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2023, "make": "Toyota", "model": "Camry"},
        topic="blind spot monitor calibration",
        capture=True,
    )

    assert result["verified"] is True
    assert result["captured"] is True
    assert captured_tasks == ["task-1"]


@pytest.mark.asyncio
async def test_model_never_calling_extract_never_verifies_even_if_it_claims_success(monkeypatch):
    navigator = _FakeNavigator()
    navigator.verified_after_extract = True  # would verify IF extract had been called
    monkeypatch.setattr(research_navigator_agent, "scrapex_svc", type("_S", (), {"navigator": navigator}))

    client = _ScriptedClient([
        [("click", {"ref": "e2"})],
        None,  # model just stops and claims success in plain text
    ])

    result = await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2023, "make": "Toyota", "model": "Camry"},
        topic="blind spot monitor calibration",
    )

    assert result["verified"] is False
    assert result["agent_stopped_reason"] == "model_finished"


@pytest.mark.asyncio
async def test_rejected_extract_is_fed_back_and_model_can_backtrack_to_a_verified_leaf(monkeypatch):
    navigator = _FakeNavigator()
    navigator.verification_sequence = [False, True, True]
    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator}),
    )
    client = _ScriptedClient([
        [("extract", {})],
        [("back", {})],
        [("extract", {})],
    ])

    result = await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2023, "make": "Toyota", "model": "Camry"},
        topic="blind spot monitor calibration",
    )

    assert result["verified"] is True
    assert result["agent_stopped_reason"] == "verified_after_extract"
    assert navigator.extract_count == 2
    actions = [call["action"] for call in navigator.calls]
    assert actions.count("verify") == 3  # two candidate checks + final authority check
    assert "back" in actions
    # The second model turn must be able to see why its first candidate failed.
    second_turn_context = json.dumps(client.messages_seen[1], default=str)
    assert "verification_after_extract" in second_turn_context
    assert "Candidate rejected" in second_turn_context


@pytest.mark.asyncio
async def test_observation_summary_includes_page_text_and_breadcrumb():
    summary = research_navigator_agent._observation_summary(
        _navigator_result(
            "observe",
            data={
                "url": "https://my.alldata.com/procedure",
                "title": "Procedure",
                "breadcrumb": ["ADAS", "Lane Change Assist"],
                "page_text": "Rear Side Radar Beam Axis Adjustment",
                "elements": [],
            },
        )
    )
    assert summary["page_text"] == "Rear Side Radar Beam Axis Adjustment"
    assert summary["breadcrumb"] == ["ADAS", "Lane Change Assist"]


@pytest.mark.asyncio
async def test_create_task_failure_short_circuits_before_any_model_turn(monkeypatch):
    navigator = _FakeNavigator(create_ok=False)
    monkeypatch.setattr(research_navigator_agent, "scrapex_svc", type("_S", (), {"navigator": navigator}))

    client = _ScriptedClient([[("observe", {})]])  # must never be consumed

    result = await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2023, "make": "Toyota", "model": "Camry"},
        topic="topic",
    )

    assert result["attempted"] is True
    assert result["searched"] is False
    assert result["verified"] is False
    assert navigator.calls == [
        {"action": "create_task", "provider": "alldata", "target": {"year": 2023, "make": "Toyota", "model": "Camry"}, "topic": "topic"}
    ]


@pytest.mark.asyncio
async def test_turn_budget_exhausted_still_runs_the_verify_epilogue(monkeypatch):
    navigator = _FakeNavigator()
    monkeypatch.setattr(research_navigator_agent, "scrapex_svc", type("_S", (), {"navigator": navigator}))

    # The model keeps clicking forever and never calls done or extract.
    client = _ScriptedClient([[("click", {"ref": "e9"})] for _ in range(10)])

    result = await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2023, "make": "Toyota", "model": "Camry"},
        topic="topic",
        max_turns=3,
    )

    assert result["agent_stopped_reason"] == "turn_budget_exhausted"
    assert result["verified"] is False
    assert "verify" in [call["action"] for call in navigator.calls]


@pytest.mark.asyncio
async def test_repeated_identical_invalid_call_stops_the_loop_early(monkeypatch):
    navigator = _FakeNavigator()
    monkeypatch.setattr(research_navigator_agent, "scrapex_svc", type("_S", (), {"navigator": navigator}))

    # click with no ref is invalid and caught before ever reaching ScrapeX.
    client = _ScriptedClient([[("click", {})] for _ in range(10)])

    result = await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2023, "make": "Toyota", "model": "Camry"},
        topic="topic",
        max_turns=10,
    )

    assert result["agent_stopped_reason"] == "repeated_tool_error"
    click_calls = [call for call in navigator.calls if call["action"] == "click"]
    assert click_calls == []  # the malformed call never actually reached ScrapeX


@pytest.mark.asyncio
async def test_unknown_action_is_reported_back_to_the_model_without_calling_scrapex(monkeypatch):
    navigator = _FakeNavigator()
    monkeypatch.setattr(research_navigator_agent, "scrapex_svc", type("_S", (), {"navigator": navigator}))

    client = _ScriptedClient([
        [("teleport", {})],
        None,
    ])

    result = await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2023, "make": "Toyota", "model": "Camry"},
        topic="topic",
    )

    assert result["attempted"] is True
    teleport_calls = [call for call in navigator.calls if call.get("action") == "teleport"]
    assert teleport_calls == []


@pytest.mark.asyncio
async def test_visual_observation_is_passed_to_multimodal_model_when_available(monkeypatch):
    navigator = _FakeNavigator()

    async def screenshot(settings, task_id):  # noqa: ARG001
        assert task_id == "task-1"
        return b"\xff\xd8\xfffake-jpeg", "image/jpeg"

    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator, "navigator_screenshot": screenshot}),
    )
    client = _ScriptedClient([None])

    await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2023, "make": "Toyota", "model": "Camry"},
        topic="blind spot monitor calibration",
    )

    first_user = client.messages_seen[0][1]
    assert isinstance(first_user["content"], list)
    assert first_user["content"][0]["type"] == "text"
    assert first_user["content"][1]["type"] == "image_url"
    assert first_user["content"][1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )


@pytest.mark.asyncio
async def test_multiple_model_actions_do_not_run_blind_against_one_observation(monkeypatch):
    navigator = _FakeNavigator()
    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator}),
    )
    client = _ScriptedClient([
        [
            ("fill", {"ref": "e1", "text": "2023 Toyota Camry"}),
            ("click", {"ref": "e2"}),
        ],
        None,
    ])

    await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2023, "make": "Toyota", "model": "Camry"},
        topic="topic",
    )

    acted = [call["action"] for call in navigator.calls if call["action"] in {"fill", "click"}]
    assert acted == ["fill"]


@pytest.mark.asyncio
async def test_initial_alldata_authentication_boundary_short_circuits_before_model_turn(monkeypatch):
    class AuthBoundaryNavigator(_FakeNavigator):
        async def __call__(self, settings, args):  # noqa: ARG002
            self.calls.append(dict(args))
            action = args.get("action")
            if action == "create_task":
                return _navigator_result(
                    "create_task",
                    status="created",
                    data={
                        "id": "task-1",
                        "provider": args["provider"],
                        "target": args["target"],
                        "topic": args["topic"],
                    },
                )
            if action == "observe":
                return {
                    "service": "ScrapeX",
                    "action": "observe",
                    "provider": "alldata",
                    "status": "authentication_required",
                    "success": False,
                    "executed": False,
                    "verified": False,
                    "work_complete": False,
                    "authentication_required": True,
                    "requires_human": True,
                    "message": "ALLDATA requires interactive authentication in ScrapeX's visible Navigator browser.",
                }
            raise AssertionError(f"unexpected action after auth boundary: {action}")

    navigator = AuthBoundaryNavigator()
    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator}),
    )
    client = _ScriptedClient([[("click", {"ref": "e1"})]])

    result = await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2024, "make": "Hyundai", "model": "Santa Fe"},
        topic="front long-range radar calibration",
    )

    assert result["status"] == "authentication_required"
    assert result["requires_human"] is True
    assert result["searched"] is False
    assert result["verified"] is False
    assert "ALLDATA requires interactive authentication" in result["reason"]
    assert client.messages_seen == []
    assert [call["action"] for call in navigator.calls] == ["create_task", "observe"]
