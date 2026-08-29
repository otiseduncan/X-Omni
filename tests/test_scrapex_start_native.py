"""
Tests for scrapex.start_native -- the self-healing recovery tool that lets
X Omni bring ScrapeX's local server back up itself instead of the operator
having to run scripts\\start.ps1 by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from core.services import scrapex
from core.tools.registry import TOOL_SCHEMAS, Registry


@dataclass
class FakeSettings:
    scrapex_base_url: str
    scrapex_project_path: Path
    root: Path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "ScrapeX"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "start.ps1").write_text("# stub\n", encoding="utf-8")
    return root


@pytest.fixture
def settings(project: Path, tmp_path: Path) -> FakeSettings:
    return FakeSettings(
        scrapex_base_url="http://127.0.0.1:8125",
        scrapex_project_path=project,
        root=tmp_path / "x-omni-root",
    )


def _install_transport(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        scrapex.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )


class FakeProcess:
    def __init__(self, returncode=None):
        self._returncode = returncode
        self.returncode = returncode

    def poll(self):
        return self._returncode


@pytest.mark.asyncio
async def test_already_healthy_short_circuits_without_spawning(settings, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True}, request=request)

    _install_transport(monkeypatch, handler)

    def fake_popen(*args, **kwargs):
        raise AssertionError("should not spawn ScrapeX when already healthy")

    monkeypatch.setattr(scrapex.subprocess, "Popen", fake_popen)

    result = await scrapex.start_native(settings)

    assert result["status"] == "already_healthy"
    assert result["verified"] is True


@pytest.mark.asyncio
async def test_becomes_healthy_after_spawning(settings, monkeypatch):
    state = {"up": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if not state["up"]:
            raise httpx.ConnectError("Connection refused", request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    _install_transport(monkeypatch, handler)

    def fake_popen(cmd, *, cwd, stdout, stderr, creationflags):
        state["up"] = True  # the "server" is now serving
        return FakeProcess(returncode=None)

    monkeypatch.setattr(scrapex.subprocess, "Popen", fake_popen)

    result = await scrapex.start_native(settings)

    assert result["status"] == "healthy"
    assert result["executed"] is True
    assert result["verified"] is True


@pytest.mark.asyncio
async def test_process_exiting_early_fails_fast_with_log_tail(settings, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    _install_transport(monkeypatch, handler)

    def fake_popen(cmd, *, cwd, stdout, stderr, creationflags):
        stdout.write("Run .\\scripts\\install.ps1 first.\n")
        stdout.flush()
        return FakeProcess(returncode=1)

    monkeypatch.setattr(scrapex.subprocess, "Popen", fake_popen)

    result = await scrapex.start_native(settings)

    assert result["status"] == "failed"
    assert result["error"]["code"] == "failed"
    assert result["exit_code"] == 1
    assert "install.ps1" in result["detail"]


@pytest.mark.asyncio
async def test_polling_timeout_fails_without_a_process_exit(settings, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    _install_transport(monkeypatch, handler)

    def fake_popen(cmd, *, cwd, stdout, stderr, creationflags):
        return FakeProcess(returncode=None)  # keeps "running" but never answers

    monkeypatch.setattr(scrapex.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(scrapex, "NATIVE_START_TIMEOUT_S", 0.05)
    monkeypatch.setattr(scrapex, "NATIVE_START_POLL_INTERVAL_S", 0.01)

    result = await scrapex.start_native(settings)

    assert result["status"] == "failed"
    assert result["exit_code"] is None


@pytest.mark.asyncio
async def test_missing_launcher_script_fails_without_spawning(tmp_path: Path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    _install_transport(monkeypatch, handler)

    empty_project = tmp_path / "no scrapex here"
    empty_project.mkdir()
    settings = FakeSettings(
        scrapex_base_url="http://127.0.0.1:8125",
        scrapex_project_path=empty_project,
        root=tmp_path / "x-omni-root",
    )

    def fake_popen(*args, **kwargs):
        raise AssertionError("should not spawn a launcher that does not exist")

    monkeypatch.setattr(scrapex.subprocess, "Popen", fake_popen)

    result = await scrapex.start_native(settings)

    assert result["status"] == "configuration_error"
    assert "Native launcher not found" in result["error"]["message"]


def test_tool_is_wired_at_operator_authorized_tier():
    TOOL_SCHEMAS.update(scrapex.SCRAPEX_TOOL_SCHEMAS)
    assert "scrapex_start_native" in TOOL_SCHEMAS
    registry = Registry("config/tools.yaml")
    assert registry.tier("scrapex_start_native") == "operator_authorized"
    names = {item["function"]["name"] for item in registry.profile_catalog("owner")}
    assert "scrapex_start_native" in names
