from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from core.services import research_navigator_tool_repair as repair


NAV_TOOL = [{"type": "function", "function": {"name": "navigator_browse", "parameters": {}}}]


class _MalformedThenGoodClient:
    def __init__(self):
        self.calls = []

    async def stream(self, messages, tools=None, max_tokens=None, *, tool_choice=None):
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens,
            "tool_choice": tool_choice,
        })
        if len(self.calls) == 1:
            raise RuntimeError(
                "worker returned HTTP 500: Failed to parse tool call arguments as JSON: missing closing quote"
            )
        yield {
            "type": "tool_call",
            "id": "call-1",
            "name": "navigator_browse",
            "arguments": '{"action":"extract"}',
        }


@pytest.mark.asyncio
async def test_repairs_malformed_navigator_tool_json_once():
    delegate = _MalformedThenGoodClient()
    client = repair.NavigatorToolRepairClient(delegate)
    events = [
        event
        async for event in client.stream(
            [{"role": "user", "content": "current page"}],
            tools=NAV_TOOL,
            max_tokens=500,
        )
    ]

    assert events[0]["arguments"] == '{"action":"extract"}'
    assert client.navigator_tool_json_repairs == 1
    assert len(delegate.calls) == 2
    retry = delegate.calls[1]
    assert retry["max_tokens"] == 192
    assert retry["tool_choice"] == {
        "type": "function",
        "function": {"name": "navigator_browse"},
    }
    repair_text = retry["messages"][-1]["content"]
    assert "NO browser action executed" in repair_text
    assert "Never copy page/procedure text" in repair_text
    assert '{"action":"extract"}' in repair_text


@pytest.mark.asyncio
async def test_repairs_tool_call_with_missing_action_before_loop_sees_it():
    class _Client:
        def __init__(self):
            self.calls = 0

        async def stream(self, messages, tools=None, max_tokens=None, *, tool_choice=None):  # noqa: ARG002
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_call",
                    "id": "bad",
                    "name": "navigator_browse",
                    "arguments": "{}",
                }
                return
            yield {
                "type": "tool_call",
                "id": "good",
                "name": "navigator_browse",
                "arguments": '{"action":"extract"}',
            }

    delegate = _Client()
    client = repair.NavigatorToolRepairClient(delegate)
    events = [event async for event in client.stream([], tools=NAV_TOOL, max_tokens=500)]

    assert delegate.calls == 2
    assert client.navigator_tool_json_repairs == 1
    assert events == [{
        "type": "tool_call",
        "id": "good",
        "name": "navigator_browse",
        "arguments": '{"action":"extract"}',
    }]


@pytest.mark.asyncio
async def test_legacy_stream_signature_is_unchanged_on_normal_turns():
    class _LegacyClient:
        def __init__(self):
            self.calls = []

        async def stream(self, messages, tools=None, max_tokens=None):
            self.calls.append((messages, tools, max_tokens))
            yield {
                "type": "tool_call",
                "id": "call-legacy",
                "name": "navigator_browse",
                "arguments": '{"action":"extract"}',
            }

    delegate = _LegacyClient()
    client = repair.NavigatorToolRepairClient(delegate)
    events = [
        event
        async for event in client.stream(
            [{"role": "user", "content": "current page"}],
            tools=NAV_TOOL,
            max_tokens=500,
        )
    ]

    assert len(delegate.calls) == 1
    assert events[0]["arguments"] == '{"action":"extract"}'
    assert client.navigator_tool_json_repairs == 0


@pytest.mark.asyncio
async def test_does_not_retry_non_navigator_or_non_parse_failure():
    class _Client:
        async def stream(self, messages, tools=None, max_tokens=None, *, tool_choice=None):  # noqa: ARG002
            raise RuntimeError("worker returned HTTP 500: unrelated server failure")
            yield  # pragma: no cover

    client = repair.NavigatorToolRepairClient(_Client())
    with pytest.raises(RuntimeError, match="unrelated server failure"):
        _ = [event async for event in client.stream([], tools=NAV_TOOL)]
    assert client.navigator_tool_json_repairs == 0


@pytest.mark.asyncio
async def test_installer_preserves_run_search_signature_and_wraps_client():
    seen = {}

    async def run_navigator_search(*, client, settings, provider, target, topic, objective=None):  # noqa: ARG001
        seen["client"] = client
        return {"ok": True}

    original_signature = inspect.signature(run_navigator_search)
    module = SimpleNamespace(run_navigator_search=run_navigator_search)
    repair.install(module)

    assert inspect.signature(module.run_navigator_search) == original_signature
    delegate = _MalformedThenGoodClient()
    result = await module.run_navigator_search(
        client=delegate,
        settings=object(),
        provider="alldata",
        target={},
        topic="radar",
        objective={"objective": "radar"},
    )
    assert result == {"ok": True}
    assert isinstance(seen["client"], repair.NavigatorToolRepairClient)
