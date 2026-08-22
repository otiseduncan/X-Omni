from __future__ import annotations

from pathlib import Path

import pytest

from core.orchestrator import loop as loop_mod
from core.services import research_capture, research_operator, research_setup
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
    public_result = research_capture._safe_filename("Toyota / Collision: Position <statement>")
    assert "/" not in public_result
    assert ":" not in public_result
    assert "<" not in public_result


def test_mobile_setup_page_never_uses_browser_secret_storage():
    page = research_operator._setup_html()
    lowered = page.casefold()
    assert "windows credential manager" in lowered
    assert "localstorage" not in lowered
    assert "sessionstorage" not in lowered
    assert "/api/research/providers/alldata/credentials" in page
    assert "one-time-code" in lowered


def test_collision_tool_schema_has_no_password_argument_and_can_capture_public_oem():
    properties = TOOL_SCHEMAS["collision_research"]["parameters"]["properties"]
    assert "password" not in properties
    assert "credential" not in properties
    assert "action" in properties
    assert "public_capture" in properties["action"]["enum"]


def test_setup_regex_does_not_capture_generic_web_login():
    assert research_setup._SETUP_RE.search("log in to my bank") is None


def test_windows_vault_validation_does_not_require_windows_api():
    with pytest.raises(ValueError, match="username"):
        research_operator.WindowsCredentialVault._validate("", "password")
    with pytest.raises(ValueError, match="password"):
        research_operator.WindowsCredentialVault._validate("otis", "")
    username, password = research_operator.WindowsCredentialVault._validate(" otis@example.com ", "secret")
    assert username == "otis@example.com"
    assert password == "secret"


def test_public_html_snapshot_strips_scripts_and_keeps_readable_text():
    document = """
    <html><head><title>Toyota Collision Position Statement</title></head>
    <body><script>steal()</script><h1>Blind Spot Monitor</h1>
    <p>Do not install a recycled sensor when the OEM procedure prohibits it.</p></body></html>
    """
    assert research_capture._html_title(document) == "Toyota Collision Position Statement"
    text = research_capture._visible_text(document)
    assert "steal" not in text
    assert "Blind Spot Monitor" in text
    assert "recycled sensor" in text
