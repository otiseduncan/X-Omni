from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services import research_bottom_decision_guard as guard


def _agent() -> SimpleNamespace:
    def system_prompt(*args, **kwargs):  # noqa: ARG001
        return "base prompt"

    def observation_summary(result):
        return dict(result.get("data") or {})

    return SimpleNamespace(
        _system_prompt=system_prompt,
        _observation_summary=observation_summary,
        NAVIGATOR_AGENT_TOOL_SCHEMA={
            "function": {
                "description": "browse",
                "parameters": {
                    "properties": {
                        "delta_y": {"description": "scroll amount"},
                    }
                },
            }
        },
    )


@pytest.mark.asyncio
async def test_downward_scroll_is_refused_after_bottom_without_browser_call():
    calls: list[dict] = []

    async def navigator(settings, args):  # noqa: ARG001
        calls.append(dict(args))
        return {
            "success": True,
            "verified": True,
            "data": {
                "observation_id": "obs-bottom",
                "scroll_position": {
                    "scroll_y": 900,
                    "scroll_height": 1000,
                    "viewport_height": 100,
                    "at_page_bottom": True,
                },
            },
        }

    scrapex = SimpleNamespace(navigator=navigator)
    agent = _agent()
    guard.install(agent, scrapex)

    observed = await scrapex.navigator(object(), {"action": "observe", "task_id": "task-1"})
    assert observed["success"] is True

    blocked = await scrapex.navigator(
        object(), {"action": "scroll", "task_id": "task-1", "delta_y": 1600}
    )

    assert blocked["success"] is False
    assert blocked["executed"] is False
    assert blocked["status"] == "invalid_request"
    assert blocked["error"]["code"] == "scroll_at_page_bottom"
    assert calls == [{"action": "observe", "task_id": "task-1"}]


@pytest.mark.asyncio
async def test_upward_scroll_is_still_allowed_at_bottom():
    calls: list[dict] = []

    async def navigator(settings, args):  # noqa: ARG001
        calls.append(dict(args))
        at_bottom = args.get("action") == "observe"
        return {
            "success": True,
            "verified": True,
            "data": {
                "scroll_position": {
                    "scroll_y": 900 if at_bottom else 300,
                    "scroll_height": 1000,
                    "viewport_height": 100,
                    "at_page_bottom": at_bottom,
                },
            },
        }

    scrapex = SimpleNamespace(navigator=navigator)
    agent = _agent()
    guard.install(agent, scrapex)

    await scrapex.navigator(object(), {"action": "observe", "task_id": "task-1"})
    result = await scrapex.navigator(
        object(), {"action": "scroll", "task_id": "task-1", "delta_y": -800}
    )

    assert result["success"] is True
    assert calls[-1]["delta_y"] == -800


def test_agent_prompt_and_observation_require_a_bottom_decision():
    async def navigator(settings, args):  # noqa: ARG001
        return {"success": True, "data": {}}

    scrapex = SimpleNamespace(navigator=navigator)
    agent = _agent()
    guard.install(agent, scrapex)

    prompt = agent._system_prompt({}, "topic")
    assert "CANDIDATE-SUBMISSION INVARIANT" in prompt
    assert "extract is not a declaration" in prompt.lower()
    assert "independent reviewer" in prompt
    assert "END-OF-PAGE INVARIANT" in prompt
    assert "call extract" in prompt
    assert "Do not request another downward scroll" in prompt

    summary = agent._observation_summary(
        {
            "data": {
                "scroll_position": {
                    "at_page_bottom": True,
                    "scroll_y": 900,
                    "scroll_height": 1000,
                    "viewport_height": 100,
                }
            }
        }
    )
    contract = summary["bottom_decision_contract"]
    assert contract["scroll_down_allowed"] is False
    assert contract["decision_required"] is True
    assert contract["extract_is_candidate_submission"] is True
    assert "call extract now" in contract["instruction"]
    assert "independent review" in contract["instruction"]

    delta_description = agent.NAVIGATOR_AGENT_TOOL_SCHEMA["function"]["parameters"]["properties"]["delta_y"]["description"]
    assert "at_page_bottom=true" in delta_description
