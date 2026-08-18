from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.orchestrator.loop import (
    Orchestrator,
    web_research_request,
)
from core.state.db import Store
from core.tools.registry import Registry


class _Router:
    active_name = "omni"

    @staticmethod
    def active_config():
        return SimpleNamespace(supports_vision=True, supports_audio=True)


class _DenialModel:
    async def stream(self, messages, tools=None):
        assert any(message.get("role") == "tool" for message in messages)
        yield {
            "type": "content",
            "text": "I don't have access to external websites or the internet.",
        }


def test_explicit_and_current_web_requests_are_deterministic_but_specialists_win():
    assert web_research_request("Search the web for OpenAI product news") == {
        "query": "Search the web for OpenAI product news",
        "max_results": 6,
    }
    assert web_research_request("Where can I find the manual online?") is not None
    assert web_research_request("What is the latest OpenAI release?") is not None
    assert web_research_request("What is the weather today?") is None
    assert web_research_request("What is on my calendar today?") is None
    assert web_research_request("Explain why releases use semantic versions") is None


@pytest.mark.parametrize(
    "text",
    [
        "Search the web for current materials-science publications",
        "Research academic literature about additive manufacturing and firearms",
        "Search the web for federal court cases involving distribution of firearm CAD files",
        (
            "Find academic and legal sources for a college paper about the history, "
            "distribution, regulation, and court treatment of 3D-printed firearm files"
        ),
    ],
)
@pytest.mark.asyncio
async def test_research_subjects_run_tool_and_suppress_false_capability_denial(
    tmp_path,
    text,
):
    store = Store(tmp_path / "web-routing.sqlite")
    conversation_id = store.create_conversation("Web routing")
    registry = Registry("config/tools.yaml", store=store)
    calls = []

    async def research(args):
        calls.append(dict(args))
        return {
            "ok": True,
            "status": "healthy",
            "query": args["query"],
            "external_network": True,
            "source_bounded": True,
            "providers": [{"provider": "test", "status": "healthy", "results": 1}],
            "sources": [{
                "index": 1,
                "title": "Official release note",
                "url": "https://example.test/release",
                "snippet": "A supported excerpt.",
                "provider": "test",
            }],
            "summary": "Found one source.",
        }

    registry.register("web_research_current", research)
    orchestrator = Orchestrator(
        _Router(),
        _DenialModel(),
        registry,
        store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )
    store.add_message(conversation_id, "user", text)
    events = [event async for event in orchestrator.run_turn(conversation_id, text)]

    assert calls == [{"query": text, "max_results": 6}]
    assert [event["type"] for event in events] == [
        "tool_start",
        "tool_result",
        "artifact",
        "token",
        "done",
    ]
    assert events[2]["artifact"]["type"] == "web_research"
    assert events[3]["text"].startswith("I searched the live public web")
    assert "don't have access" not in events[3]["text"]
    persisted = store.get_messages(conversation_id)[-1]
    assert persisted["content"] == events[3]["text"]
    assert persisted["artifacts"][0]["type"] == "web_research"
