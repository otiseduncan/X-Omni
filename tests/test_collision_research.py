from __future__ import annotations

from pathlib import Path

import pytest

from core.orchestrator import loop as loop_mod
from core.services import research_operator, research_setup
from core.tools.registry import Registry, TOOL_SCHEMAS


def test_alldata_setup_language_routes_directly_to_secure_card():
    phrases = [
        "Set up AllData for me",
        "add my ALLDATA credentials",
        "I need to log in to all data",
        "configure alldata password",
    ]
    for phrase in phrases:
        assert loop_mod.deterministic_read_tool(phrase) == "research_provider_setup"


def test_unrelated_requests_keep_existing_deterministic_router_behavior():
    assert loop_mod.deterministic_read_tool("what time is it") is None


def test_research_tools_are_registered_with_separate_policy_tiers(tmp_path: Path):
    policy = tmp_path / "tools.yaml"
    policy.write_text("roots: []\nwrite_roots: []\ntools: {}\n", encoding="utf-8")
    registry = Registry(policy)
    assert registry.tier("research_provider_setup") == "read_only"
    assert registry.tier("collision_research") == "operator_authorized"
    assert "research_provider_setup" in registry._handlers  # noqa: SLF001
    assert "collision_research" in registry._handlers  # noqa: SLF001
    assert "research_provider_setup" in TOOL_SCHEMAS
    assert "collision_research" in TOOL_SCHEMAS


def test_test_users_do_not_receive_owner_research_provider_tools(tmp_path: Path):
    policy = tmp_path / "tools.yaml"
    policy.write_text("roots: []\nwrite_roots: []\ntools: {}\n", encoding="utf-8")
    registry = Registry(policy)
    names = {
        item["function"]["name"]
        for item in registry.model_tools("test_user")
    }
    assert "research_provider_setup" not in names
    assert "collision_research" not in names


def test_alldata_navigation_is_confined_to_provider_domains():
    assert research_operator._is_alldata_url("https://my.alldata.com/") is True
    assert research_operator._is_alldata_url("https://foo.alldata.com/path") is True
    assert research_operator._is_alldata_url("http://my.alldata.com/") is False
    assert research_operator._is_alldata_url("https://alldata.com.evil.example/") is False
    assert research_operator._is_alldata_url("https://example.com/") is False


def test_research_capture_filename_is_bounded_and_sanitized():
    result = research_operator._safe_filename("2026 Toyota / BSM: Recycled? <test>")
    assert "/" not in result
    assert ":" not in result
    assert "<" not in result
    assert len(result) <= 150


def test_mobile_setup_page_never_uses_browser_secret_storage():
    page = research_operator._setup_html()
    lowered = page.casefold()
    assert "windows credential manager" in lowered
    assert "localstorage" not in lowered
    assert "sessionstorage" not in lowered
    assert "/api/research/providers/alldata/credentials" in page
    assert "one-time-code" in lowered


def test_collision_tool_schema_has_no_password_argument():
    properties = TOOL_SCHEMAS["collision_research"]["parameters"]["properties"]
    assert "password" not in properties
    assert "credential" not in properties
    assert "action" in properties


def test_setup_regex_does_not_capture_generic_web_login():
    assert research_setup._SETUP_RE.search("log in to my bank") is None


def test_windows_vault_validates_before_platform_write(monkeypatch):
    vault = research_operator.WindowsCredentialVault()
    with pytest.raises(ValueError, match="username"):
        vault.write("", "password")
    with pytest.raises(ValueError, match="password"):
        vault.write("otis", "")
