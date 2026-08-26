from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from core.orchestrator.loop import (
    ARTIFACT_FOR_TOOL,
    Orchestrator,
    tool_result_for_model,
)
from core.orchestrator.prompt import system_prompt
from core.services.website import (
    MAX_WEBSITE_PROMPT_CHARS,
    _harden_preview,
    make_website_preview,
)
from core.state.db import Store, WebsiteRevisionConflict
from core.tools.registry import Registry, TOOL_SCHEMAS, ToolBlocked


class FakeClient:
    def __init__(self, response: str):
        self.response = response
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.response


@pytest.mark.asyncio
async def test_website_preview_is_bounded_buffered_and_network_blocked():
    client = FakeClient(
        "```html\n<!doctype html><html><head><title>Tim's Tow Truck</title>"
        "<link rel='stylesheet' href='https://bad.example/x.css'></head>"
        "<body><h1>Tim's Tow Truck</h1><script>fetch('https://bad.example')</script>"
        "</body></html>\n```"
    )
    result = await make_website_preview(client)({"prompt": "A towing company website"})

    assert result["ok"] is True
    assert result["revision"] == 1
    assert result["website_id"].startswith("website:")
    assert result["parent_sha256"] is None
    assert result["title"] == "Tim's Tow Truck"
    assert result["written_to_disk"] is False
    assert result["deployed"] is False
    assert result["preview"] == {
        "sandboxed": True,
        "network_blocked": True,
        "scripts_enabled": False,
    }
    assert "Content-Security-Policy" in result["html"]
    assert "default-src 'none'" in result["html"]
    assert result["html"].index("Content-Security-Policy") < result["html"].index("bad.example")
    assert len(result["sha256"]) == 64
    assert client.calls[0][1]["max_tokens"] == 5_000
    assert "Return only the HTML document" in client.calls[0][0][0]["content"]


@pytest.mark.asyncio
async def test_website_preview_rejects_missing_oversized_or_non_html_output():
    handler = make_website_preview(FakeClient("I cannot create websites."))
    with pytest.raises(ValueError, match="prompt is required"):
        await handler({"prompt": ""})
    with pytest.raises(ValueError, match="limited"):
        await handler({"prompt": "x" * (MAX_WEBSITE_PROMPT_CHARS + 1)})
    with pytest.raises(ValueError, match="complete HTML"):
        await handler({"prompt": "Make a site"})

    incomplete = make_website_preview(FakeClient("<!doctype html><html><body>cut off"))
    with pytest.raises(ValueError, match="incomplete HTML"):
        await incomplete({"prompt": "Make a site"})


def test_website_tool_is_exposed_and_chat_native():
    normal_registry = Registry("config/tools.yaml")
    normal_registry.register("website_preview_generate", lambda _args: {})
    assert "website_preview_generate" not in {
        item["function"]["name"] for item in normal_registry.model_tools()
    }

    registry = Registry("config/tools.yaml", profile="full")
    registry.register("website_preview_generate", lambda _args: {})
    names = [item["function"]["name"] for item in registry.model_tools()]
    assert "website_preview_generate" in names
    assert registry.tier("website_preview_generate") == "read_only"
    assert ARTIFACT_FOR_TOOL["website_preview_generate"] == "website_preview"


def test_website_semantics_live_in_full_profile_schema_not_normal_prompt():
    class Config:
        supports_vision = True
        supports_audio = True

    class Router:
        def active_config(self):
            return Config()

    prompt = system_prompt(Router())
    assert "website_preview_generate" not in prompt
    schema_description = TOOL_SCHEMAS["website_preview_generate"]["description"]
    assert "sandboxed inline chat preview" in schema_description
    assert "does not write files or deploy" in schema_description


def test_website_html_stays_in_artifact_not_model_feed_or_tool_audit():
    html = "<html><body>" + ("x" * 40_000) + "</body></html>"
    result = {
        "ok": True,
        "status": "generated_preview",
        "title": "Large preview",
        "html": html,
        "bytes": len(html.encode("utf-8")),
        "sha256": "a" * 64,
        "written_to_disk": False,
        "deployed": False,
        "message": "Preview generated.",
    }

    projected = tool_result_for_model("website_preview_generate", result)
    assert "html" not in projected
    assert projected["sha256"] == "a" * 64
    assert "do not repeat or regenerate the code" in projected["assistant_instruction"]

    logged = Registry.log_result("website_preview_generate", result)
    assert logged["html"]["redacted"] is True
    assert logged["html"]["characters"] == len(html)
    assert len(logged["html"]["sha256"]) == 64


@pytest.mark.asyncio
async def test_gateway_hash_matches_the_exact_redacted_website_artifact():
    html = '<html><body><script>const api_key="demo-value";</script></body></html>'
    registry = Registry("config/tools.yaml")
    registry.register(
        "website_preview_generate",
        lambda _args: {
            "ok": True,
            "status": "generated_preview",
            "html": html,
            "bytes": len(html.encode("utf-8")),
            "sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        },
    )

    result = await registry.invoke(
        "website_preview_generate", {"prompt": "Make a static demo"}
    )

    assert "demo-value" not in result["html"]
    rendered = result["html"].encode("utf-8")
    assert result["bytes"] == len(rendered)
    assert result["sha256"] == hashlib.sha256(rendered).hexdigest()


def test_preview_sanitizer_blocks_fake_head_navigation_and_network_markup():
    malicious = """<!doctype html><html><body><!-- <head> --><head>
      <meta http-equiv="refresh" content="0;url=https://bad.example/refresh">
      <link rel="stylesheet" href="https://bad.example/site.css">
      <style>@import url(https://bad.example/import.css); .hero { background:url(https://bad.example/style.png) }</style>
      <title>Safe preview</title></head><main style="background:url(https://bad.example/bg.png)">
      <a href="https://bad.example/go" target="_top">leave</a>
      <form action="http://127.0.0.1:9/private"><button formaction="https://bad.example/post">send</button></form>
      <iframe src="http://169.254.169.254/latest/meta-data/"></iframe>
      <img src="https://bad.example/pixel.png" onerror="fetch('https://bad.example/error')">
      <svg><rect fill="url(https://bad.example/svg.svg#paint)"></rect></svg>
      <script>const example = 'https://bad.example/code-only';</script>
      </main></body></html>"""

    hardened = _harden_preview(malicious)
    lowered = hardened.casefold()

    assert lowered.startswith("<!doctype html><html><head>")
    assert lowered.count("<html") == 1
    assert lowered.count("<head") == 1
    assert lowered.count("<body") == 1
    assert lowered.index("<head>") < lowered.index("content-security-policy")
    assert "refresh" not in lowered
    assert "<link" not in lowered
    assert "<iframe" not in lowered
    assert "<a " not in lowered
    assert "<form" not in lowered
    assert " href=" not in lowered
    assert " action=" not in lowered
    assert " formaction=" not in lowered
    assert " src=\"http" not in lowered
    assert "onerror=" not in lowered
    assert 'background:url(&quot;&quot;)' in lowered
    assert "@import" not in lowered
    assert 'fill="url(&quot;&quot;)"' in lowered
    # Generated JavaScript remains available as code, but the iframe policy
    # and script-src boundary prevent it from executing.
    assert "code-only" in hardened


def _source_website() -> dict:
    html = _harden_preview(
        "<!doctype html><html><head><title>Jimmy's Jumpers and Trampolines</title>"
        "<style>.product-card,.service-card{background:#fff}</style></head><body>"
        "<div class='product-card'>Trampolines</div>"
        "<div class='service-card'>Repairs</div></body></html>"
    )
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    return {
        "ok": True,
        "status": "generated_preview",
        "title": "Jimmy's Jumpers and Trampolines",
        "request": "Build Jimmy's Jumpers and Trampolines",
        "html": html,
        "bytes": len(html.encode("utf-8")),
        "sha256": digest,
        "written_to_disk": False,
        "deployed": False,
    }


class _WebsiteStore:
    def __init__(self, source: dict):
        self.messages = [
            {"id": 1, "role": "user", "content": "Build a website", "artifacts": []},
            {
                "id": 2,
                "role": "assistant",
                "content": "Preview generated.",
                "worker_used": "omni",
                "artifacts": [{"type": "website_preview", "data": source}],
            },
        ]
        self.saved = []

    def get_messages(self, _conversation_id):
        return list(self.messages)

    def add_message(self, conversation_id, role, content, **kwargs):
        message_id = len(self.messages) + 1
        message = {
            "id": message_id,
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "worker_used": kwargs.get("worker_used"),
            "artifacts": kwargs.get("artifacts") or [],
        }
        self.messages.append(message)
        self.saved.append(message)
        return message_id

    @staticmethod
    def touch_conversation(_conversation_id, title=None):
        return None


class _NoWebsiteModel:
    def __init__(self):
        self.complete_calls = 0
        self.stream_calls = 0

    async def complete(self, _messages, **_kwargs):
        self.complete_calls += 1
        raise AssertionError("The deterministic glass edit must not call the model")

    async def stream(self, _messages, tools=None):
        self.stream_calls += 1
        raise AssertionError("A completed website revision needs no prose synthesis")
        yield  # pragma: no cover


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["cards", "cords", "csrds"])
async def test_glass_card_revision_uses_latest_persisted_html_without_model(spelling):
    source = _source_website()
    store = _WebsiteStore(source)
    client = _NoWebsiteModel()
    handler = make_website_preview(client, store)
    prompt = f"change all the {spelling} on the website to a translucent glass effect"

    result = await handler({
        "prompt": prompt,
        "operation": "update_latest",
        "conversation_id": 15,
    })

    assert result["ok"] is True
    assert result["status"] == "updated_preview"
    assert result["parent_sha256"] == source["sha256"]
    assert result["revision"] == 2
    assert result["website_id"] == f"website:{source['sha256'][:32]}"
    assert result["changes"] == ["cards.translucent_glass"]
    assert result["html"].count('id="xomni-glass-card-edit"') == 1
    assert ".product-card" in result["html"]
    assert ".service-card" in result["html"]
    assert "backdrop-filter: blur(16px)" in result["html"]
    assert result["sha256"] == hashlib.sha256(result["html"].encode("utf-8")).hexdigest()
    assert result["bytes"] == len(result["html"].encode("utf-8"))
    assert client.complete_calls == 0

    # A repeated request creates a linked revision but does not stack styles.
    store.messages.append({
        "id": 3,
        "role": "assistant",
        "content": "Updated.",
        "artifacts": [{"type": "website_preview", "data": result}],
    })
    repeated = await handler({
        "prompt": prompt,
        "operation": "update_latest",
        "conversation_id": 15,
    })
    assert repeated["parent_sha256"] == result["sha256"]
    assert repeated["status"] == "unchanged_preview"
    assert repeated["changed"] is False
    assert repeated["revision"] == 2
    assert repeated["website_id"] == result["website_id"]
    assert repeated["html"].count('id="xomni-glass-card-edit"') == 1
    assert client.complete_calls == 0


@pytest.mark.asyncio
async def test_website_update_failure_keeps_prior_revision_and_never_claims_success():
    source = _source_website()
    store = _WebsiteStore(source)

    class DisconnectingClient:
        async def complete(self, _messages, **_kwargs):
            raise RuntimeError("server disconnected")

    result = await make_website_preview(DisconnectingClient(), store)({
        "prompt": "replace the website layout with an entirely different composition",
        "operation": "update_latest",
        "conversation_id": 15,
    })

    assert result["ok"] is False
    assert result["status"] == "update_failed"
    assert result["parent_sha256"] == source["sha256"]
    assert "html" not in result
    assert "previous revision remains unchanged" in result["message"]
    projected = tool_result_for_model("website_preview_generate", result)
    assert "Do not claim success" in projected["assistant_instruction"]


@pytest.mark.asyncio
async def test_registry_binds_website_update_to_authoritative_conversation():
    source = _source_website()
    store = _WebsiteStore(source)
    client = _NoWebsiteModel()
    registry = Registry("config/tools.yaml")
    registry.register("website_preview_generate", make_website_preview(client, store))

    with pytest.raises(ToolBlocked, match="another conversation"):
        await registry.invoke(
            "website_preview_generate",
            {
                "prompt": "change the website cards to glass",
                "operation": "update_latest",
                "conversation_id": 99,
            },
            conversation_id=15,
        )

    result = await registry.invoke(
        "website_preview_generate",
        {
            "prompt": "change the website cards to glass",
            "operation": "update_latest",
        },
        conversation_id=15,
    )
    assert result["parent_sha256"] == source["sha256"]
    assert result["ok"] is True


def test_initial_website_is_persisted_before_artifact_event():
    class InitialClient:
        def __init__(self):
            self.stream_calls = 0
            self.complete_calls = 0

        async def stream(self, _messages, tools=None):
            self.stream_calls += 1
            if self.stream_calls > 1:
                raise AssertionError("Initial website must not enter a synthesis round")
            yield {
                "type": "tool_call",
                "id": "website-call",
                "name": "website_preview_generate",
                "arguments": json.dumps({"prompt": "Jimmy's Jumpers and Trampolines"}),
            }

        async def complete(self, _messages, **_kwargs):
            self.complete_calls += 1
            return (
                "<!doctype html><html><head><title>Jimmy's Jumpers and Trampolines</title>"
                "</head><body><main>Jump safely.</main></body></html>"
            )

    class InitialStore(_WebsiteStore):
        def __init__(self):
            self.messages = [{
                "id": 1,
                "role": "user",
                "content": "display a website preview in chat for Jimmy's Jumpers",
                "artifacts": [],
            }]
            self.saved = []

    client = InitialClient()
    store = InitialStore()
    registry = Registry("config/tools.yaml")
    registry.register("website_preview_generate", make_website_preview(client, store))

    class Router:
        active_name = "omni"

        @staticmethod
        def active_config():
            return SimpleNamespace(supports_vision=True, supports_audio=True)

    orchestrator = Orchestrator(
        Router(), client, registry, store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )

    async def collect():
        stream = orchestrator.run_turn(
            15, "display a website preview in chat for Jimmy's Jumpers"
        )
        first = await anext(stream)
        assert store.saved and store.saved[-1]["artifacts"]
        return [first, *[event async for event in stream]]

    events = asyncio.run(collect())
    assert [event["type"] for event in events] == [
        "tool_start", "tool_result", "artifact", "token", "done"
    ]
    assert events[2]["artifact"]["data"]["status"] == "generated_preview"
    assert "html" not in events[1]["result"]
    assert "Generated Jimmy's Jumpers and Trampolines" in events[3]["text"]
    assert client.complete_calls == 1
    # Website generation is model-chosen now, not deterministically
    # pre-routed -- the model gets one real round to pick the tool, unlike
    # before when routing bypassed the model for this call entirely.
    assert client.stream_calls == 1


def test_model_preamble_is_not_emitted_when_website_persistence_fails():
    class PreambleClient:
        async def stream(self, _messages, tools=None):
            yield {"type": "content", "text": "Done — I generated the website."}
            yield {
                "type": "tool_call",
                "id": "late-website-call",
                "name": "website_preview_generate",
                "arguments": json.dumps({"prompt": "Jimmy's Jumpers"}),
            }

        async def complete(self, _messages, **_kwargs):
            return (
                "<!doctype html><html><head><title>Jimmy's Jumpers</title></head>"
                "<body><main>Jump safely.</main></body></html>"
            )

    class FailingStore(_WebsiteStore):
        def add_message(self, *_args, **_kwargs):
            raise RuntimeError("durable store unavailable")

    client = PreambleClient()
    store = FailingStore(_source_website())
    registry = Registry("config/tools.yaml")
    registry.register("website_preview_generate", make_website_preview(client, store))

    class Router:
        active_name = "omni"

        @staticmethod
        def active_config():
            return SimpleNamespace(supports_vision=True, supports_audio=True)

    orchestrator = Orchestrator(
        Router(), client, registry, store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )

    async def collect_until_failure():
        return [
            event
            async for event in orchestrator.run_turn(15, "Build a homepage for Jimmy")
        ]

    emitted = asyncio.run(collect_until_failure())
    assert [event["type"] for event in emitted] == ["error"]
    assert "durable store unavailable" in emitted[0]["message"]


def test_website_revision_commit_rejects_a_stale_sibling(tmp_path):
    store = Store(tmp_path / "website-cas.db")
    conversation_id = store.create_conversation("Website CAS")
    source = _source_website()
    source["website_id"] = f"website:{source['sha256'][:32]}"
    source["revision"] = 1
    source["parent_sha256"] = None
    store.add_message(
        conversation_id,
        "assistant",
        "Generated the original preview.",
        worker_used="omni",
        artifacts=[{"type": "website_preview", "data": source}],
    )

    first = dict(source)
    first.update({
        "html": source["html"] + "<!-- first child -->",
        "revision": 2,
        "parent_sha256": source["sha256"],
        "status": "updated_preview",
    })
    first["sha256"] = hashlib.sha256(first["html"].encode("utf-8")).hexdigest()
    first["bytes"] = len(first["html"].encode("utf-8"))

    second = dict(first)
    second["html"] = source["html"] + "<!-- stale sibling -->"
    second["sha256"] = hashlib.sha256(second["html"].encode("utf-8")).hexdigest()
    second["bytes"] = len(second["html"].encode("utf-8"))

    first_id = store.add_website_revision_message(
        conversation_id,
        "Updated the preview.",
        worker_used="omni",
        artifacts=[{"type": "website_preview", "data": first}],
        website_id=source["website_id"],
        expected_parent_sha256=source["sha256"],
    )
    with pytest.raises(WebsiteRevisionConflict, match="changed"):
        store.add_website_revision_message(
            conversation_id,
            "Updated a stale sibling.",
            worker_used="omni",
            artifacts=[{"type": "website_preview", "data": second}],
            website_id=source["website_id"],
            expected_parent_sha256=source["sha256"],
        )

    tampered = dict(first)
    tampered["html"] += "<!-- digest mismatch -->"
    with pytest.raises(ValueError, match="commit identity"):
        store.add_website_revision_message(
            conversation_id,
            "Attempted a tampered child.",
            worker_used="omni",
            artifacts=[{"type": "website_preview", "data": tampered}],
            website_id=source["website_id"],
            expected_parent_sha256=first["sha256"],
        )

    messages = store.get_messages(conversation_id)
    assert [message["id"] for message in messages][-1] == first_id
    website_artifacts = [
        artifact
        for message in messages
        for artifact in message.get("artifacts", [])
        if artifact.get("type") == "website_preview"
    ]
    assert [artifact["data"]["sha256"] for artifact in website_artifacts] == [
        source["sha256"],
        first["sha256"],
    ]
