from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from core.models.client import ModelClient
from core.models.router import WorkerSwapError


class LeaseRouter:
    def __init__(self, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.sessions = 0
        self.recoveries = 0
        self.held = False
        self.cfg = SimpleNamespace(
            alias="test-model", base_url="http://127.0.0.1:65530/v1"
        )

    @asynccontextmanager
    async def inference_session(self):
        self.sessions += 1
        if self.fail_first and self.sessions == 1:
            raise WorkerSwapError("not active")
        self.held = True
        try:
            yield
        finally:
            self.held = False

    async def recover(self):
        assert self.held is False
        self.recoveries += 1
        return {"recovered": True}

    def active_config(self):
        return self.cfg


@pytest.mark.asyncio
async def test_stream_holds_inference_lease_for_entire_attempt(monkeypatch):
    router = LeaseRouter()
    client = ModelClient(router)

    async def fake_stream_once(*_args, **_kwargs):
        assert router.held is True
        yield {"type": "content", "text": "safe"}
        assert router.held is True

    monkeypatch.setattr(client, "_stream_once", fake_stream_once)
    events = [event async for event in client.stream([{"role": "user", "content": "hi"}])]
    assert events == [{"type": "content", "text": "safe"}]
    assert router.held is False
    assert router.sessions == 1


@pytest.mark.asyncio
async def test_stream_releases_lease_before_recovery_and_retries_once(monkeypatch):
    router = LeaseRouter(fail_first=True)
    client = ModelClient(router)

    async def fake_stream_once(*_args, **_kwargs):
        assert router.held is True
        yield {"type": "content", "text": "recovered"}

    monkeypatch.setattr(client, "_stream_once", fake_stream_once)
    events = [event async for event in client.stream([{"role": "user", "content": "hi"}])]
    assert events[0]["text"] == "recovered"
    assert router.recoveries == 1
    assert router.sessions == 2


@pytest.mark.asyncio
async def test_non_stream_completion_uses_same_lifecycle_guard(monkeypatch):
    router = LeaseRouter()
    client = ModelClient(router)

    async def fake_complete_once(*_args, **_kwargs):
        assert router.held is True
        return "done"

    monkeypatch.setattr(client, "_complete_once", fake_complete_once)
    assert await client.complete([{"role": "user", "content": "hi"}]) == "done"
    assert router.sessions == 1
