from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.routes import create_router
from core.services import research
from core.tools.builtin import system as builtin
from core.tools.registry import Registry


def test_duckduckgo_parser_returns_safe_bounded_sources():
    page = """
    <div class="result">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fstory%3Ftoken%3Dsecret%26id%3D7">Example result</a>
      <a class="result__snippet">A <b>current</b> excerpt.</a>
    </div>
    """
    sources = research._parse_duckduckgo(page, "example", 1)
    assert len(sources) == 1
    assert sources[0]["title"] == "Example result"
    assert sources[0]["snippet"] == "A current excerpt."
    assert sources[0]["url"] == "https://example.com/story?id=7"
    assert "secret" not in sources[0]["url"]


def test_duckduckgo_lite_parser_returns_sources():
    page = """
    <a class="result-link" href="https://example.com/story">Lite result</a>
    <td class="result-snippet">A <b>lite</b> excerpt.</td>
    """
    sources = research._parse_duckduckgo(page, "example", 1)
    assert len(sources) == 1
    assert sources[0]["title"] == "Lite result"
    assert sources[0]["snippet"] == "A lite excerpt."
    assert sources[0]["url"] == "https://example.com/story"


@pytest.mark.asyncio
async def test_provider_fetch_refuses_redirects_and_oversized_bodies(monkeypatch):
    real_client = research.httpx.AsyncClient
    requested = []

    def redirect_handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    redirect_transport = httpx.MockTransport(redirect_handler)
    monkeypatch.setattr(
        research.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=redirect_transport, **kwargs),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await research._bounded_get(
            "https://html.duckduckgo.com/html/",
            params={"q": "safe"},
            allowed_content_types=("text/html",),
        )
    assert len(requested) == 1
    assert "127.0.0.1" not in requested[0]

    def oversized_handler(_request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"x" * (research.MAX_PROVIDER_RESPONSE_BYTES + 1),
        )

    oversized_transport = httpx.MockTransport(oversized_handler)
    monkeypatch.setattr(
        research.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=oversized_transport, **kwargs),
    )
    with pytest.raises(research.ProviderResponseError, match="byte limit"):
        await research._bounded_get(
            "https://html.duckduckgo.com/html/",
            params={"q": "safe"},
            allowed_content_types=("text/html",),
        )


@pytest.mark.asyncio
async def test_duckduckgo_search_submits_query_as_form_data(monkeypatch):
    requested = []

    async def bounded_post(url, *, data, allowed_content_types):
        requested.append((url, data, allowed_content_types))
        return b'<a class="result__a" href="https://example.com/">Result</a>'

    monkeypatch.setattr(research, "_bounded_post", bounded_post)
    sources, status = await research._search_duckduckgo("safe query", 1)

    assert requested == [(
        "https://html.duckduckgo.com/html/",
        {"q": "safe query"},
        ("text/html",),
    )]
    assert len(sources) == 1
    assert status == {"provider": "duckduckgo", "status": "healthy", "results": 1}


@pytest.mark.asyncio
async def test_current_research_is_source_bounded_and_truthful_when_empty(monkeypatch):
    async def duck(_query, _limit):
        return ([research._source("duckduckgo", "One", "https://example.com/one", "Evidence")],
                {"provider": "duckduckgo", "status": "healthy", "results": 1})

    async def news(_query, _limit):
        return ([], {"provider": "google_news", "status": "degraded", "results": 0})

    monkeypatch.setattr(research, "_search_duckduckgo", duck)
    monkeypatch.setattr(research, "_search_google_news", news)
    result = await research.search_current({"query": "latest example", "max_results": 4})
    assert result["ok"] is True
    assert result["source_bounded"] is True
    assert result["external_network"] is True
    assert result["sources"][0]["index"] == 1
    assert "cite their source numbers" in result["summary"]

    async def empty(_query, _limit):
        return ([], {"provider": "none", "status": "degraded", "results": 0})

    monkeypatch.setattr(research, "_search_duckduckgo", empty)
    monkeypatch.setattr(research, "_search_google_news", empty)
    result = await research.search_current({"query": "unconfirmed event"})
    assert result["ok"] is False
    assert result["status"] == "warning"
    assert "Do not infer that an event did not happen" in result["summary"]


@pytest.mark.asyncio
async def test_current_research_rejects_secret_query_before_provider_egress(monkeypatch):
    calls = []

    async def provider(query, _limit):
        calls.append(query)
        return [], {"provider": "test", "status": "healthy", "results": 0}

    monkeypatch.setattr(research, "_search_duckduckgo", provider)
    monkeypatch.setattr(research, "_search_google_news", provider)
    with pytest.raises(ValueError, match="sensitive material"):
        await research.search_current({"query": 'password="super secret phrase"'})
    assert calls == []


def test_bounded_file_search_excludes_protected_paths(tmp_path):
    (tmp_path / "visible.py").write_text("first\nNEEDLE here\nlast\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("NEEDLE=secret", encoding="utf-8")
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "roots:\n"
        f"  - '{str(tmp_path).replace(chr(92), chr(92) * 2)}'\n"
        "write_roots: []\n"
        "tools: {}\n",
        encoding="utf-8",
    )
    registry = Registry(policy)
    result = builtin.make_search_files(registry)({
        "query": "needle", "path": str(tmp_path), "glob": "*",
    })
    assert result["match_count"] == 1
    assert result["matches"][0]["path"].endswith("visible.py")
    assert result["matches"][0]["line"] == 2
    assert result["skipped_protected_paths"] >= 1
    assert "secret" not in str(result)


def test_file_search_traversal_and_quoted_secret_results_are_bounded(tmp_path, monkeypatch):
    for index in range(8):
        (tmp_path / f"visible-{index}.txt").write_text(
            'PASSWORD="super secret phrase"\n', encoding="utf-8"
        )
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "roots:\n"
        f"  - '{str(tmp_path).replace(chr(92), chr(92) * 2)}'\n"
        "write_roots: []\n"
        "tools:\n"
        "  search_files:\n"
        "    tier: read_only\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(builtin, "MAX_SEARCH_ENTRIES", 4)
    registry = Registry(policy)
    registry.register("search_files", builtin.make_search_files(registry))
    result = asyncio.run(registry.invoke(
        "search_files", {"query": "password", "path": str(tmp_path), "glob": "*.txt"}
    ))
    assert result["truncated"] is True
    assert result["visited_entries"] == 5
    assert "super secret phrase" not in str(result)
    assert "[REDACTED]" in str(result)


def test_capability_catalog_reports_real_tools_and_known_limits(tmp_path):
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "roots:\n"
        f"  - '{str(tmp_path).replace(chr(92), chr(92) * 2)}'\n"
        "write_roots: []\n"
        "tools:\n"
        "  web_research_current:\n"
        "    tier: read_only\n",
        encoding="utf-8",
    )
    registry = Registry(policy)
    registry.register("web_research_current", lambda _args: {})
    router = SimpleNamespace(
        active_name="omni",
        configs={
            "omni": SimpleNamespace(supports_vision=True, supports_audio=True),
            "coder": SimpleNamespace(supports_vision=False, supports_audio=False),
        },
    )
    result = builtin.make_assistant_capabilities(router, registry)({})
    assert result["delivery"] == "existing_chat_stream"
    assert result["catalog_is_execution_proof"] is False
    assert [item["name"] for item in result["tools"]] == ["web_research_current"]
    assert any(item["name"] == "image attachments" for item in result["not_wired"])


def test_task_mutations_are_approval_gated_and_status_update_is_real(tmp_path):
    registry = Registry("config/tools.yaml")
    assert registry.tier("list_tasks") == "read_only"
    assert registry.tier("add_task") == "confirm_required"
    assert registry.tier("update_task_status") == "confirm_required"

    class TaskStore:
        def __init__(self):
            self.tasks = [{"id": 7, "title": "Inspect receipts", "status": "open", "due_at": None}]

        def list_tasks(self, status=None):
            return [item for item in self.tasks if status is None or item["status"] == status]

        def add_task(self, title, due_at=None):
            self.tasks.append({"id": 8, "title": title, "status": "open", "due_at": due_at})
            return 8

        def set_task_status(self, task_id, status):
            next(item for item in self.tasks if item["id"] == task_id)["status"] = status

    store = TaskStore()
    _list_tasks, _add_task, update = builtin.make_task_tools(store)
    result = update({"task_id": 7, "status": "done"})
    assert result["status"] == "done"
    assert store.tasks[0]["status"] == "done"
    with pytest.raises(ValueError, match="does not exist"):
        update({"task_id": 999, "status": "done"})


def test_direct_read_artifact_is_persisted_to_the_active_conversation():
    class DirectRegistry:
        policy = {"list_tasks": {"tier": "read_only"}}
        roots = []
        _handlers = {"list_tasks": object()}

        def tier(self, name):
            return "read_only" if name == "list_tasks" else "blocked"

        async def invoke(self, name, args, **kwargs):
            assert name == "list_tasks"
            assert args == {}
            assert kwargs == {"conversation_id": 9}
            return {"tasks": [{"id": 1, "title": "Durable", "status": "open"}]}

    class DirectStore:
        def __init__(self):
            self.saved = []

        def add_message(self, conversation_id, role, content, **kwargs):
            self.saved.append((conversation_id, role, content, kwargs))
            return 44

    async def session():
        return {"google_sub": "owner"}

    store = DirectStore()
    router = SimpleNamespace(active_name="omni")
    app = FastAPI()
    app.include_router(create_router(SimpleNamespace(), store, router, DirectRegistry(), session))
    response = TestClient(app).post(
        "/api/tools/list_tasks/run", json={"conversation_id": 9}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["artifact"]["type"] == "tasks"
    assert payload["executed"] is True
    assert payload["success"] is True
    assert payload["message_id"] == 44
    assert store.saved == [(
        9,
        "assistant",
        "",
        {"worker_used": "omni", "artifacts": [payload["artifact"]]},
    )]


def test_direct_read_route_does_not_claim_success_for_failed_result():
    class DirectRegistry:
        policy = {"get_calendar": {"tier": "read_only"}}
        roots = []
        _handlers = {"get_calendar": object()}

        @staticmethod
        def tier(_name):
            return "read_only"

        @staticmethod
        async def invoke(_name, _args, **kwargs):
            assert kwargs == {"conversation_id": 9}
            return {"ok": False, "connected": False, "message": "Unavailable", "events": []}

    class DirectStore:
        @staticmethod
        def add_message(_conversation_id, _role, _content, **_kwargs):
            return 45

    async def session():
        return {"google_sub": "owner"}

    app = FastAPI()
    app.include_router(create_router(
        SimpleNamespace(), DirectStore(), SimpleNamespace(active_name="omni"),
        DirectRegistry(), session,
    ))
    payload = TestClient(app).post(
        "/api/tools/get_calendar/run", json={"conversation_id": 9}
    ).json()
    assert payload["ok"] is False
    assert payload["executed"] is True
    assert payload["success"] is False
    assert payload["result"]["ok"] is False
