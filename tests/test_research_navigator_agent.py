"""Tests for the model-driven Navigator ALLDATA agent loop.

These exercise the control flow (task creation, tool dispatch, turn budget,
repeated-failure detection) and, most importantly, that verification always
comes from ScrapeX's own verify action -- never from the model's own narration --
by driving a fake scrapex.navigator() rather than a
real ScrapeX service.
"""

from __future__ import annotations

import copy
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

    def __init__(self, *, create_ok: bool = True, bulky: bool = False):
        self.calls: list[dict[str, Any]] = []
        self._create_ok = create_ok
        # bulky reproduces the shape that actually broke the live loop: a
        # large element map on a page that changes every action.
        self._bulky = bulky
        self._acted = 0
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
            if self._bulky:
                self._acted += 1
                return _navigator_result(
                    action,
                    status="acted",
                    work_complete=(action == "done"),
                    data={
                        "url": f"https://my.alldata.com/node/{self._acted}",
                        "title": f"Menu level {self._acted}",
                        "page_text": "Service and repair procedure listing. " * 200,
                        "elements": [
                            {
                                "ref": f"e{self._acted}_{index}",
                                "role": "link",
                                "name": f"Adjustments and calibration subsection {index}",
                                "expanded": None,
                            }
                            for index in range(120)
                        ],
                        "loop_warning": None,
                        "backtrack_available": True,
                        "action_executed": True,
                        "is_search_action": action == "fill",
                    },
                )
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
        self.messages_seen.append(copy.deepcopy(list(messages)))
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


async def _no_sleep(_seconds: float) -> None:
    return None


def test_system_prompt_prefers_alldata_ymme_search_without_make_aliases() -> None:
    prompt = research_navigator_agent._system_prompt(
        {"year": 2021, "make": "Hyundai", "model": "Palisade"},
        "front radar calibration target distance",
    )

    assert "Search by Year, Make, Model, Engine, or VIN" in prompt
    assert "let ALLDATA resolve its own make taxonomy" in prompt
    assert "do not invent or hardcode make aliases" in prompt


@pytest.mark.asyncio
async def test_initial_observation_waits_for_rendered_page_before_model_call(
    monkeypatch,
) -> None:
    class _InitialRaceNavigator(_FakeNavigator):
        def __init__(self):
            super().__init__()
            self.observe_count = 0

        async def __call__(self, settings, args):
            if args.get("action") == "observe":
                self.calls.append(dict(args))
                self.observe_count += 1
                if self.observe_count == 1:
                    return _navigator_result(
                        "observe",
                        status="observed",
                        data={
                            "url": "https://my.alldata.com/#/home",
                            "title": "ALLDATA",
                            "page_text": "",
                            "elements": [],
                        },
                    )
            return await super().__call__(settings, args)

    navigator = _InitialRaceNavigator()
    navigator.verified_after_extract = True
    monkeypatch.setattr(research_navigator_agent.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator}),
    )
    client = _ScriptedClient(
        [
            [("fill", {"ref": "e1", "text": "2021 Hyundai Palisade"})],
            [("extract", {})],
        ]
    )

    result = await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2021, "make": "Hyundai", "model": "Palisade"},
        topic="front radar calibration target distance",
    )

    assert result["verified"] is True
    assert navigator.observe_count == 2
    first_model_input = json.dumps(client.messages_seen[0])
    assert "Vehicle Select" in first_model_input
    assert "e1" in first_model_input


@pytest.mark.asyncio
async def test_empty_initial_page_exits_without_inventing_browser_action(monkeypatch) -> None:
    class _NeverReadyNavigator(_FakeNavigator):
        async def __call__(self, settings, args):
            if args.get("action") == "observe":
                self.calls.append(dict(args))
                return _navigator_result(
                    "observe",
                    status="observed",
                    data={
                        "url": "https://my.alldata.com/#/home",
                        "title": "ALLDATA",
                        "page_text": "",
                        "elements": [],
                    },
                )
            return await super().__call__(settings, args)

    navigator = _NeverReadyNavigator()
    monkeypatch.setattr(research_navigator_agent.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator}),
    )
    client = _ScriptedClient([[("click", {"ref": "e11"})]])

    result = await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2021, "make": "Hyundai", "model": "Palisade"},
        topic="front radar calibration target distance",
    )

    assert result["status"] == "initial_page_not_ready"
    assert result["initial_observe_attempts"] == 5
    assert result["searched"] is False
    assert client.messages_seen == []
    assert [call["action"] for call in navigator.calls].count("observe") == 5


@pytest.mark.asyncio
async def test_definitive_stale_ref_failure_supplies_fresh_observation(monkeypatch) -> None:
    class _StaleRefNavigator(_FakeNavigator):
        def __init__(self):
            super().__init__()
            self.observe_count = 0

        async def __call__(self, settings, args):
            action = args.get("action")
            if action == "observe":
                self.observe_count += 1
                if self.observe_count > 1:
                    self.calls.append(dict(args))
                    return _navigator_result(
                        "observe",
                        status="observed",
                        data={
                            "url": "https://my.alldata.com/repair/#/select-vehicle",
                            "title": "Select Vehicle",
                            "page_text": "Search by Year, Make, Model, Engine, or VIN",
                            "elements": [
                                {
                                    "ref": "e9",
                                    "role": "searchbox",
                                    "name": "Search by Year, Make, Model, Engine, or VIN",
                                }
                            ],
                        },
                    )
            if action == "fill":
                self.calls.append(dict(args))
                return {
                    "service": "ScrapeX",
                    "action": "fill",
                    "status": "invalid_request",
                    "success": False,
                    "executed": False,
                    "verified": False,
                    "http_status": 422,
                    "detail": "'e1' is not a ref from the most recent observation.",
                    "error": {
                        "code": "invalid_request",
                        "message": "ScrapeX returned HTTP 422.",
                    },
                }
            return await super().__call__(settings, args)

    navigator = _StaleRefNavigator()
    navigator.verified_after_extract = True
    monkeypatch.setattr(research_navigator_agent.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator}),
    )
    client = _ScriptedClient(
        [
            [("fill", {"ref": "e1", "text": "2021 Hyundai Palisade"})],
            [("extract", {})],
        ]
    )

    result = await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2021, "make": "Hyundai", "model": "Palisade"},
        topic="front radar calibration target distance",
    )

    assert result["verified"] is True
    second_model_input = json.dumps(client.messages_seen[1])
    assert "fresh observation of the current rendered page" in second_model_input
    assert "e9" in second_model_input
    assert "not a ref from the most recent observation" in second_model_input
    actions = [call["action"] for call in navigator.calls]
    assert actions[:4] == ["create_task", "observe", "fill", "observe"]


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


def _prompt_chars(messages: list[dict[str, Any]]) -> int:
    return len(json.dumps(messages, default=str))


@pytest.mark.asyncio
async def test_transcript_stays_bounded_instead_of_growing_with_every_action(monkeypatch):
    """The regression that stopped every live ALLDATA task at 2-5 actions.

    The loop used to append each observation twice -- as the tool result and
    again as the visual user message -- and never drop one, so the prompt
    grew by thousands of tokens per browser action and the 32K worker
    rejected the fifth call outright. Drill-down needs far more actions than
    that, so bounded growth is the feature.
    """
    navigator = _FakeNavigator(bulky=True)
    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator}),
    )
    turns = [[("click", {"ref": f"e{index}"})] for index in range(14)]
    client = _ScriptedClient(turns + [None])

    await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2021, "make": "Hyundai", "model": "Palisade"},
        topic="front radar calibration target distance",
    )

    assert len(client.messages_seen) >= 14
    after_first_action = _prompt_chars(client.messages_seen[1])
    after_last_action = _prompt_chars(client.messages_seen[-1])
    # Thirteen more browser actions must not multiply the prompt. The only
    # growth allowed is one short digest line per superseded observation.
    assert after_last_action < after_first_action * 2

    # Exactly one full element map is ever in context: stale refs are
    # rejected by ScrapeX, so older maps cost context and buy nothing.
    final = client.messages_seen[-1]
    full_maps = [
        message
        for message in final
        if "Adjustments and calibration subsection 119" in json.dumps(message, default=str)
    ]
    assert len(full_maps) == 1


@pytest.mark.asyncio
async def test_superseded_observations_collapse_to_a_digest_of_what_was_tried(monkeypatch):
    navigator = _FakeNavigator(bulky=True)
    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator}),
    )
    client = _ScriptedClient([
        [("click", {"ref": "e1"})],
        [("click", {"ref": "e2"})],
        [("click", {"ref": "e3"})],
        None,
    ])

    await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2021, "make": "Hyundai", "model": "Palisade"},
        topic="front radar calibration target distance",
    )

    final = client.messages_seen[-1]
    digests = [
        message["content"]
        for message in final
        if isinstance(message.get("content"), str)
        and message["content"].startswith("[action ")
    ]
    # Dropping the old element maps must not drop the navigation memory:
    # each superseded observation leaves behind what was tried and where it
    # led, so the model can still tell a new branch from a repeat.
    assert digests, final
    assert any("click" in digest and "ref=e1" in digest for digest in digests)
    assert any("/node/1" in digest for digest in digests)
    assert any(
        isinstance(message.get("content"), str)
        and message["content"].startswith("[initial page]")
        for message in final
    )


@pytest.mark.asyncio
async def test_tool_receipt_does_not_repeat_the_observation(monkeypatch):
    navigator = _FakeNavigator(bulky=True)
    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator}),
    )
    client = _ScriptedClient([[("click", {"ref": "e1"})], None])

    await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2021, "make": "Hyundai", "model": "Palisade"},
        topic="front radar calibration target distance",
    )

    tool_messages = [
        message for message in client.messages_seen[-1] if message.get("role") == "tool"
    ]
    assert tool_messages
    for message in tool_messages:
        payload = json.loads(message["content"])
        assert payload["executed"] is True
        assert payload["url"] == "https://my.alldata.com/node/1"
        assert "elements" not in payload
        assert "page_text" not in payload


@pytest.mark.asyncio
async def test_model_is_told_plainly_when_an_action_changed_nothing(monkeypatch):
    """Three identical scrolls on the live vehicle picker is what this stops.

    ScrapeX's loop warning covers its own navigation graph; an action that
    leaves the rendered page byte-identical never reaches it. The signal is
    a statement about observed state, not a hint about what to click.
    """
    navigator = _FakeNavigator()
    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator}),
    )
    client = _ScriptedClient([
        [("scroll", {"delta_y": 1600})],
        [("scroll", {"delta_y": 1600})],
        None,
    ])

    await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2021, "make": "Hyundai", "model": "Palisade"},
        topic="front radar calibration target distance",
    )

    first_action_context = json.dumps(client.messages_seen[1], default=str)
    second_action_context = json.dumps(client.messages_seen[2], default=str)
    assert "left the page exactly as it was" not in first_action_context
    assert "left the page exactly as it was" in second_action_context


@pytest.mark.asyncio
async def test_one_oversized_page_is_degraded_rather_than_refused(monkeypatch):
    navigator = _FakeNavigator(bulky=True)
    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator}),
    )
    monkeypatch.setattr(research_navigator_agent, "_TRANSCRIPT_TOKEN_BUDGET", 900)
    client = _ScriptedClient([[("click", {"ref": "e1"})], [("click", {"ref": "e2"})], None])

    result = await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2021, "make": "Hyundai", "model": "Palisade"},
        topic="front radar calibration target distance",
    )

    # A degraded turn beats the HTTP 400 that used to end the task, and the
    # degradation is reported rather than silent.
    assert result["context_degraded"] is True
    assert "cut to fit the model's context" in json.dumps(
        client.messages_seen[-1], default=str
    )


def test_a_cut_off_procedure_page_says_so_and_says_to_scroll():
    """The last blocker in the 2026-09-12 live Palisade run.

    The ALLDATA article "Front Radar Unit (ADAS) - Repair Procedures" came
    back as exactly 8000 characters -- ScrapeX's cap -- ending mid-word at
    "RELATED INFORMATION Parts an". The calibration target distance sits at
    the bottom of that procedure, so it was absent, and nothing in the
    observation distinguished "the text ends here" from "the page ends
    here". The model clicked accordion refs instead of scrolling down.
    """
    summary = research_navigator_agent._observation_summary(
        _navigator_result(
            "observe",
            data={
                "url": "https://my.alldata.com/repair/#/article/62400/component/4037",
                "title": "Front Radar Unit (ADAS) - Repair Procedures (Distance Sensor)",
                "page_text": "x" * 8000,
                "page_text_truncated": True,
                "page_text_total_chars": 31450,
                "scroll_position": {
                    "scroll_y": 0,
                    "scroll_height": 9400,
                    "viewport_height": 900,
                    "at_page_bottom": False,
                },
                "elements": [{"ref": "e1", "role": "link", "name": "Parts and Labor"}],
            },
        )
    )

    note = summary["page_continues"]
    assert "CUT SHORT" in note
    assert "31450" in note
    assert "not at the bottom" in note
    # It must send the model to read, not to chase the end of the document.
    # The live Palisade article grew from 15,676px to 18,116px while being
    # scrolled, so "scroll to the bottom" spent an entire 40-turn budget and
    # extracted nothing.
    assert "extract" in note.casefold()
    assert "do not try to reach the bottom" in note.casefold()
    assert summary["page_text_truncated"] is True


def test_a_complete_page_at_the_bottom_gets_no_scroll_note():
    summary = research_navigator_agent._observation_summary(
        _navigator_result(
            "observe",
            data={
                "url": "https://my.alldata.com/repair/#/select-vehicle",
                "title": "ALLDATA Collision - Home",
                "page_text": "Select Vehicle YMME/VIN Plate",
                "page_text_truncated": False,
                "page_text_total_chars": 29,
                "scroll_position": {
                    "scroll_y": 0,
                    "scroll_height": 900,
                    "viewport_height": 900,
                    "at_page_bottom": True,
                },
                "elements": [{"ref": "e1", "role": "combobox", "name": "Model"}],
            },
        )
    )

    assert "page_continues" not in summary


def test_an_observation_with_no_scroll_signal_is_not_assumed_to_continue():
    """An older ScrapeX, or a failed page.evaluate, sends no scroll_position.

    Absent evidence must not be read as "there is more below" -- that would
    push the model to scroll every page forever.
    """
    summary = research_navigator_agent._observation_summary(
        _navigator_result(
            "observe",
            data={
                "url": "https://my.alldata.com/repair/",
                "title": "ALLDATA",
                "page_text": "short page",
                "elements": [{"ref": "e1", "role": "link", "name": "Repair"}],
            },
        )
    )

    assert "page_continues" not in summary


@pytest.mark.asyncio
async def test_scrolling_without_reading_is_called_out(monkeypatch):
    """A working scroll on a lazily-loaded page is its own trap.

    Once scrolling actually moved ALLDATA's container, the model scrolled 40
    times in a row and never extracted, because the page loads more as you go
    and its bottom keeps receding. Scrolling is for reading; a run of them
    with nothing read has to be said out loud.
    """
    navigator = _FakeNavigator(bulky=True)
    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator}),
    )
    client = _ScriptedClient([
        [("scroll", {"delta_y": 1600})],
        [("scroll", {"delta_y": 1600})],
        [("scroll", {"delta_y": 1600})],
        [("scroll", {"delta_y": 1600})],
        None,
    ])

    await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2021, "make": "Hyundai Truck", "model": "Palisade"},
        topic="front radar calibration target distance",
    )

    second = json.dumps(client.messages_seen[1], default=str)
    last = json.dumps(client.messages_seen[-1], default=str)
    assert "times in a row" not in second
    assert "times in a row" in last
    assert "call extract NOW" in last


@pytest.mark.asyncio
async def test_reading_between_scrolls_resets_the_count(monkeypatch):
    navigator = _FakeNavigator(bulky=True)
    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator}),
    )
    client = _ScriptedClient([
        [("scroll", {"delta_y": 1600})],
        [("scroll", {"delta_y": 1600})],
        [("scroll", {"delta_y": 1600})],
        [("click", {"ref": "e1"})],
        [("scroll", {"delta_y": 1600})],
        None,
    ])

    await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2021, "make": "Hyundai Truck", "model": "Palisade"},
        topic="front radar calibration target distance",
    )

    # The click breaks the run, so the scroll after it is not turn four of a drift.
    last = json.dumps(client.messages_seen[-1], default=str)
    assert "times in a row" not in last


@pytest.mark.asyncio
async def test_the_same_rejected_extract_is_not_submitted_forever(monkeypatch):
    """40 identical extracts, 40 identical refusals, one exhausted budget.

    An extract that verification rejects still *executes*, so it carried no
    error and the repeat guard never saw it. The live run on 2026-09-12
    submitted the same candidate until the turn budget ran out.
    """
    navigator = _FakeNavigator()
    navigator.verified_after_extract = False
    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator}),
    )
    same = ("extract", {"text": "2.5 m (8.2 ft) from the front radar"})
    client = _ScriptedClient([[same], [same], [same], [same], [same], [same]])

    result = await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2021, "make": "Hyundai Truck", "model": "Palisade"},
        topic="front radar calibration target distance",
    )

    assert result["agent_stopped_reason"] == "repeated_tool_error"
    assert navigator.extract_count <= 3, navigator.extract_count
    # The verification feedback must survive being counted as a repeat.
    context = json.dumps(client.messages_seen[-1], default=str)
    assert "verification_after_extract" in context
    assert "REPEATED MISTAKE" in context


@pytest.mark.asyncio
async def test_the_receipt_names_the_action_it_closes(monkeypatch):
    """Half of every live budget went to re-issuing clicks that had worked.

    On an SPA a click often leaves url and title untouched, so a receipt that
    echoed only those read as "nothing happened" and the model sent the same
    ref again. The receipt now names what was carried out and says not to
    repeat it.
    """
    navigator = _FakeNavigator(bulky=True)
    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator}),
    )
    client = _ScriptedClient([[("click", {"ref": "f24e983"})], None])

    await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2021, "make": "Hyundai Truck", "model": "Palisade"},
        topic="front radar calibration target distance",
    )

    tool_messages = [
        m for m in client.messages_seen[-1] if m.get("role") == "tool"
    ]
    assert tool_messages
    payload = json.loads(tool_messages[-1]["content"])
    assert payload["executed"] is True
    assert payload["completed_action"] == "click f24e983"
    assert "Do not send it again" in payload["do_not_repeat"]
    # Still a receipt, not a second copy of the page.
    assert "elements" not in payload
    assert "page_text" not in payload


class _LaggingNavigator(_FakeNavigator):
    """An act that answers before the navigation lands, then catches up.

    This is ALLDATA's real behaviour: the click returns an observation still
    describing the page being left, and a moment later the new page is there.
    """

    def __init__(self):
        super().__init__()
        self._observe_calls = 0

    async def __call__(self, settings, args):
        action = args.get("action")
        if action == "click":
            self.calls.append(dict(args))
            # Byte-identical to the page the model just acted from: the
            # navigation has been started but has not landed yet.
            return _navigator_result(
                "click",
                status="acted",
                data={
                    "url": "https://my.alldata.com/vehicle-select",
                    "title": "Vehicle Select",
                    "elements": [
                        {"ref": "e1", "role": "textbox", "name": "Vehicle search", "expanded": None},
                        {"ref": "e2", "role": "button", "name": "Search", "expanded": None},
                    ],
                    "loop_warning": None,
                    "backtrack_available": False,
                    "action_executed": True,
                },
            )
        if action == "observe":
            self.calls.append(dict(args))
            self._observe_calls += 1
            if self._observe_calls <= 1:
                return await super().__call__(settings, args)
            return _navigator_result(
                "observe",
                status="observed",
                data={
                    "url": "https://my.alldata.com/repair/#/select-vehicle",
                    "title": "ALLDATA Collision - Home",
                    "page_text": "Select Vehicle",
                    "elements": [
                        {"ref": "f8e396", "role": "combobox", "name": "2021", "expanded": None},
                    ],
                },
            )
        return await super().__call__(settings, args)


@pytest.mark.asyncio
async def test_a_click_that_answers_before_the_page_lands_is_waited_out(monkeypatch):
    """The alternation that cost half of every live turn budget.

    ALLDATA answered the click while still showing the old page, so the model
    saw "nothing changed", sent the same ref again, and by then the page had
    moved and the ref no longer resolved -- a 409 after every single action.
    """
    navigator = _LaggingNavigator()
    monkeypatch.setattr(
        research_navigator_agent,
        "scrapex_svc",
        type("_S", (), {"navigator": navigator}),
    )
    client = _ScriptedClient([[("click", {"ref": "e2"})], None])

    await research_navigator_agent.run_navigator_search(
        client=client,
        settings=object(),
        provider="alldata",
        target={"year": 2021, "make": "Hyundai Truck", "model": "Palisade"},
        topic="front radar calibration target distance",
    )

    # The model must be shown the page that actually arrived, with refs it can
    # use -- not the page the click was leaving.
    last = json.dumps(client.messages_seen[-1], default=str)
    assert "ALLDATA Collision - Home" in last
    assert "f8e396" in last
    assert "settled_after_observations" in last

