from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.orchestrator.loop import (
    Orchestrator,
    image_generation_request,
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


class _GenericRefusalModel:
    async def stream(self, messages, tools=None):
        assert any(message.get("role") == "tool" for message in messages)
        yield {
            "type": "content",
            "text": "I cannot provide information or guidance about that subject.",
        }


class _UnexpectedModel:
    async def stream(self, _messages, tools=None):
        raise AssertionError("deterministic image routing must pause before model synthesis")
        yield  # pragma: no cover


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
        "What websites can I find information about printing 3D firearms?",
        "Which sites discuss 3D-printed firearm law?",
        "Find academic sources about additive manufacturing of firearms.",
        "Where can I read about court cases involving firearm CAD distribution?",
        "What websites cover home-built firearm regulations?",
        "Find websites about municipal zoning history",
        "What online sources cover recent battery research?",
    ],
)
def test_natural_web_source_requests_are_deterministic(text):
    assert web_research_request(text) == {"query": text, "max_results": 6}


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


@pytest.mark.asyncio
async def test_verified_research_replaces_generic_model_refusal(tmp_path):
    store = Store(tmp_path / "generic-refusal.sqlite")
    conversation_id = store.create_conversation("Research refusal guard")
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
                "title": "Synthetic research source",
                "url": "https://example.test/research",
                "snippet": "A bounded informational excerpt.",
                "provider": "test",
            }],
            "summary": "Found one source.",
        }

    registry.register("web_research_current", research)
    orchestrator = Orchestrator(
        _Router(),
        _GenericRefusalModel(),
        registry,
        store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )
    text = "What websites can I find information about printing 3D firearms?"
    store.add_message(conversation_id, "user", text)
    events = [event async for event in orchestrator.run_turn(conversation_id, text)]

    assert calls == [{"query": text, "max_results": 6}]
    token = next(event["text"] for event in events if event["type"] == "token")
    assert token.startswith("I searched the live public web and found 1 source result")
    assert "cannot provide" not in token
    assert any(
        event.get("type") == "artifact"
        and (event.get("artifact") or {}).get("type") == "web_research"
        for event in events
    )


@pytest.mark.parametrize(
    "text",
    [
        "Generate an image of a red sports car",
        "Generate an artistic nude figure study of an adult brunette woman",
    ],
)
@pytest.mark.asyncio
async def test_explicit_image_requests_enter_existing_approval_flow(tmp_path, text):
    assert image_generation_request(text) == {"prompt": text}
    store = Store(tmp_path / "image-routing.sqlite")
    conversation_id = store.create_conversation("Image routing")
    message_id = store.add_message(conversation_id, "user", text)
    registry = Registry("config/tools.yaml", store=store)
    handler_calls = 0

    async def image_generate(_args):
        nonlocal handler_calls
        handler_calls += 1
        return {"ok": True}

    registry.register("image_generate", image_generate)
    orchestrator = Orchestrator(
        _Router(),
        _UnexpectedModel(),
        registry,
        store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )
    events = [event async for event in orchestrator.run_turn(
        conversation_id,
        text,
        approval_context={
            "session_id": "local:test",
            "user_id": "owner-test",
            "message_id": message_id,
        },
    )]

    assert handler_calls == 0
    assert [event["type"] for event in events] == ["tool_start", "approval", "done"]
    assert events[0]["name"] == "image_generate"
    assert events[0]["args"] == {"prompt": text}
    assert events[1]["approval"]["tool"] == "image_generate"
    persisted = store.get_messages(conversation_id)[-1]
    assert persisted["content"] == ""
    assert persisted["artifacts"][0]["type"] == "approval_request"
