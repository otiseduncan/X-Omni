"""Coverage for the web-research false-capability-denial guard.

Web research routing used to be regex pre-routed before the model ever saw
the message. That determinism was removed -- the model now chooses
web_research_current itself, guided only by its tool description in
config/tools.yaml and prompt.py. What had to survive that change is the
guard downstream of routing: if the model calls web_research_current, gets
a real verified result back, and then still tries to claim in a later round
that it has no web access (a known local-model failure mode), the guard
replaces that false denial with an honest summary of what the tool actually
found. That guard now triggers off any web_research_current call the model
makes on its own, tracked via last_web_research_result in Orchestrator._run,
not off the removed pre-router.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.orchestrator.loop import Orchestrator
from core.state.db import Store
from core.tools.registry import Registry


class _Router:
    active_name = "omni"

    @staticmethod
    def active_config():
        return SimpleNamespace(supports_vision=True, supports_audio=True)


def _verified_research_result(query: str) -> dict:
    return {
        "ok": True,
        "status": "healthy",
        "query": query,
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


class _CallsToolThenDeniesModel:
    """Round 1: the model chooses web_research_current itself, the way a
    real model does now that nothing pre-routes it. Round 2: despite the
    tool result already being in its own context, it falsely claims it has
    no web access -- the guard must catch this, not the routing layer."""

    def __init__(self, query: str, denial_text: str):
        self.query = query
        self.denial_text = denial_text
        self.round = 0

    async def stream(self, messages, tools=None):
        self.round += 1
        if self.round == 1:
            yield {
                "type": "tool_call",
                "id": "web-research-1",
                "name": "web_research_current",
                "arguments": json.dumps({"query": self.query, "max_results": 6}),
            }
            return
        assert any(message.get("role") == "tool" for message in messages)
        yield {"type": "content", "text": self.denial_text}


@pytest.mark.asyncio
async def test_model_chosen_research_suppresses_false_capability_denial(tmp_path):
    store = Store(tmp_path / "web-denial-guard.sqlite")
    conversation_id = store.create_conversation("Web denial guard")
    registry = Registry("config/tools.yaml", store=store)
    calls = []

    async def research(args):
        calls.append(dict(args))
        return _verified_research_result(args["query"])

    registry.register("web_research_current", research)
    text = "Search the web for current materials-science publications"
    model = _CallsToolThenDeniesModel(
        text, "I don't have access to external websites or the internet."
    )
    orchestrator = Orchestrator(
        _Router(), model, registry, store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )
    store.add_message(conversation_id, "user", text)
    events = [event async for event in orchestrator.run_turn(conversation_id, text)]

    assert calls == [{"query": text, "max_results": 6}]
    token = next(event["text"] for event in events if event["type"] == "token")
    assert token.startswith("I searched the live public web")
    assert "don't have access" not in token
    persisted = store.get_messages(conversation_id)[-1]
    assert persisted["content"] == token


@pytest.mark.asyncio
async def test_model_chosen_research_suppresses_generic_refusal(tmp_path):
    store = Store(tmp_path / "web-denial-guard-refusal.sqlite")
    conversation_id = store.create_conversation("Web denial guard refusal")
    registry = Registry("config/tools.yaml", store=store)
    calls = []

    async def research(args):
        calls.append(dict(args))
        return _verified_research_result(args["query"])

    registry.register("web_research_current", research)
    text = "What websites can I find information about printing 3D firearms?"
    model = _CallsToolThenDeniesModel(
        text, "I cannot provide information or guidance about that subject."
    )
    orchestrator = Orchestrator(
        _Router(), model, registry, store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )
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
