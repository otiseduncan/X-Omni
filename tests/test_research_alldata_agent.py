"""Tests for the model-driven ALLDATA agent loop.

These exercise the control flow (tool dispatch, turn budget) and, most
importantly, the deterministic epilogue: verification must depend on what the
fake page actually ends up showing, never on what the scripted "model" claims
in its tool-call arguments or final text.
"""

from __future__ import annotations

import json

import pytest

from core.services import research_alldata_agent


class _EmptyLoc:
    def __init__(self):
        self.first = self

    async def is_visible(self, timeout=None):  # noqa: ARG002
        return False


class _TextLoc:
    def __init__(self, text: str):
        self._text = text
        self.first = self

    async def is_visible(self, timeout=None):  # noqa: ARG002
        return True

    async def inner_text(self, timeout=None):  # noqa: ARG002
        return self._text


class _FakePage:
    def __init__(self, title: str, body: str, url: str):
        self._title = title
        self._body = body
        self.url = url

    def locator(self, selector):
        return _TextLoc(self._body) if selector == "body" else _EmptyLoc()

    def get_by_text(self, *_args, **_kwargs):
        return _EmptyLoc()

    async def title(self):
        return self._title


class _ScriptedClient:
    """Yields one scripted turn's worth of tool_call events per .stream() call."""

    def __init__(self, turns: list[list[tuple[str, dict]] | None]):
        self._turns = list(turns)

    async def stream(self, messages, tools=None, max_tokens=None):  # noqa: ARG002
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
                "name": "collision_research",
                "arguments": json.dumps(payload),
            }


class _StuckBrowser:
    """Every action is a no-op -- the page never actually leaves Select Vehicle,
    the same failure mode as the reported F-350 defect."""

    def __init__(self):
        self._page = _FakePage(
            title="ALLDATA Collision - Home",
            body="Select Vehicle YMME/VIN Year 2018 2019 Make Model Recent Vehicles Ford Explorer",
            url="https://my.alldata.com/repair/#/vehicle-select",
        )
        self.actions: list[str] = []

    async def start(self, auto_login=True):  # noqa: ARG002
        return {"authenticated": True}

    async def operator_action(self, args):
        self.actions.append(str(args.get("action")))
        return {"url": self._page.url, "title": self._page._title}


class _ProgressingBrowser:
    """Vehicle selection and search actually take effect, mutating the page --
    a stand-in for a real ALLDATA session responding to correct navigation."""

    def __init__(self):
        self._page = _FakePage(
            title="ALLDATA Collision - Home",
            body="Select Vehicle YMME/VIN Year 2018 2019 Make Model",
            url="https://my.alldata.com/repair/#/vehicle-select",
        )
        self.actions: list[str] = []

    async def start(self, auto_login=True):  # noqa: ARG002
        return {"authenticated": True}

    async def operator_action(self, args):
        action = str(args.get("action"))
        self.actions.append(action)
        text = str(args.get("text") or "")
        selector = str(args.get("selector") or "")
        if action == "fill" and "2018" in text and "ford" in text.casefold():
            self._page.url = "https://my.alldata.com/repair/#/vehicle/2018-ford-f350"
            self._page._title = "2018 Ford F-350 - ALLDATA"
            self._page._body = "Change Vehicle 2018 Ford F-350 Vehicle Information Search"
        if action == "fill" and "search" in selector.casefold():
            self._page._body += (
                " forward facing camera calibration procedure remove and replace "
                "camera reinitialize using the ALLDATA scan tool"
            )
        result = {"url": self._page.url, "title": self._page._title}
        if action == "extract":
            result["page_text"] = self._page._body
        return result


VEHICLE = {"year": "2018", "make": "Ford", "model_trim": "F-350", "label": "2018 Ford F-350"}
TOPIC = "forward facing camera calibration"


@pytest.mark.asyncio
async def test_stuck_on_select_vehicle_is_never_reported_verified():
    browser = _StuckBrowser()
    client = _ScriptedClient([
        [("snapshot", {})],
        [("fill", {"selector": "input[placeholder*='Year' i]", "text": "2018 Ford F-350"})],
        [("extract", {})],
        None,
    ])

    result = await research_alldata_agent.run_agent_search(
        client=client, browser=browser, vehicle=VEHICLE, topic=TOPIC
    )

    # This is the exact reported defect reproduced through the agent path:
    # tool calls happened, but the page never actually left Select Vehicle.
    assert result["verified"] is False
    assert result["vehicle_selection"]["selected"] is False
    assert browser.actions  # the agent did call tools -- it just never got anywhere


@pytest.mark.asyncio
async def test_successful_vehicle_first_navigation_is_verified():
    browser = _ProgressingBrowser()
    client = _ScriptedClient([
        [("fill", {"selector": "input[placeholder*='Year' i]", "text": "2018 Ford F-350"})],
        [("click_text", {"text": "2018 Ford F-350"})],
        [("fill", {"selector": "input[placeholder*='Search vehicle information' i]", "text": TOPIC})],
        [("extract", {})],
        None,
    ])

    result = await research_alldata_agent.run_agent_search(
        client=client, browser=browser, vehicle=VEHICLE, topic=TOPIC
    )

    assert result["verified"] is True
    assert result["vehicle_selection"]["selected"] is True
    assert result["query_submitted"] is True
    assert "calibration" in result["matched_terms"]


@pytest.mark.asyncio
async def test_the_model_claiming_success_in_prose_alone_is_not_enough():
    """The epilogue re-reads the live page; it never trusts assistant text."""
    browser = _StuckBrowser()
    client = _ScriptedClient([
        [("snapshot", {})],
        None,  # model gives up on tools and just asserts success in prose
    ])

    result = await research_alldata_agent.run_agent_search(
        client=client, browser=browser, vehicle=VEHICLE, topic=TOPIC
    )

    assert result["verified"] is False


@pytest.mark.asyncio
async def test_unauthenticated_session_never_calls_the_model():
    class _UnauthenticatedBrowser:
        async def start(self, auto_login=True):  # noqa: ARG002
            return {"authenticated": False}

    client = _ScriptedClient([[("snapshot", {})]])
    result = await research_alldata_agent.run_agent_search(
        client=client, browser=_UnauthenticatedBrowser(), vehicle=VEHICLE, topic=TOPIC
    )
    assert result["verified"] is False
    assert result["human_action_required"] is True
