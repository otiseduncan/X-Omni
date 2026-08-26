from __future__ import annotations

from pathlib import Path

import pytest

from core.services import (
    research_alldata_navigation,
    research_capture,
    research_operator,
)
from core.tools.registry import Registry, TOOL_SCHEMAS


def _policy_text(*, include_research: bool = True) -> str:
    tools = ""
    if include_research:
        tools = (
            "  research_provider_setup:\n"
            "    tier: read_only\n"
            "  collision_research:\n"
            "    tier: operator_authorized\n"
        )
    return f"roots: []\nwrite_roots: []\ntools:\n{tools or '  {}\n'}"


def test_alldata_setup_is_exposed_as_a_model_selectable_secure_capability():
    schema = TOOL_SCHEMAS["research_provider_setup"]
    assert schema["parameters"] == {"type": "object", "properties": {}, "required": []}
    description = schema["description"].casefold()
    assert "alldata" in description
    assert "credential" in description
    assert "never enters model context" in description


def test_research_tools_are_registered_with_separate_policy_tiers(tmp_path: Path):
    policy = tmp_path / "tools.yaml"
    policy.write_text(_policy_text(), encoding="utf-8")
    registry = Registry(policy)
    assert registry.tier("research_provider_setup") == "read_only"
    assert registry.tier("collision_research") == "operator_authorized"
    assert "research_provider_setup" in registry._handlers  # noqa: SLF001
    assert "collision_research" in registry._handlers  # noqa: SLF001
    assert "research_provider_setup" in TOOL_SCHEMAS
    assert "collision_research" in TOOL_SCHEMAS


def test_research_handlers_cannot_self_authorize_when_policy_entries_are_removed(tmp_path: Path):
    policy = tmp_path / "tools.yaml"
    policy.write_text(_policy_text(include_research=False), encoding="utf-8")
    registry = Registry(policy)
    assert "research_provider_setup" in registry._handlers  # noqa: SLF001
    assert "collision_research" in registry._handlers  # noqa: SLF001
    assert registry.tier("research_provider_setup") == "blocked"
    assert registry.tier("collision_research") == "blocked"
    names = {item["function"]["name"] for item in registry.model_tools("owner")}
    assert "research_provider_setup" not in names
    assert "collision_research" not in names


def test_test_users_do_not_receive_owner_research_provider_tools(tmp_path: Path):
    policy = tmp_path / "tools.yaml"
    policy.write_text(_policy_text(), encoding="utf-8")
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
    assert "alldata_vehicle_research" in properties["action"]["enum"]
    assert "full_research" not in properties["action"]["enum"]
    assert {"vehicle_year", "vehicle_make", "vehicle_model", "topic"} <= set(
        properties
    )


@pytest.mark.asyncio
async def test_alldata_vehicle_research_executes_model_supplied_structured_fields(
    tmp_path, monkeypatch
):
    browser = research_operator.LicensedBrowser(tmp_path)
    received: dict = {}

    async def structured_search(browser_arg, query="", *, vehicle=None, topic=None):
        assert browser_arg is browser
        assert query == ""
        received.update({"vehicle": vehicle, "topic": topic})
        return {
            "status": "success",
            "searched": True,
            "verified": True,
            "vehicle": vehicle,
            "topic": topic,
        }

    monkeypatch.setattr(
        research_alldata_navigation,
        "search_alldata_vehicle_first",
        structured_search,
    )
    result = await browser.operator_action(
        {
            "action": "alldata_vehicle_research",
            "vehicle_year": 2020,
            "vehicle_make": "Toyota",
            "vehicle_model": "Camry",
            "vehicle_trim": "LE",
            "topic": "front radar calibration prerequisites",
        }
    )
    assert result["verified"] is True
    assert received == {
        "vehicle": {
            "year": 2020,
            "make": "Toyota",
            "model": "Camry",
            "trim": "LE",
        },
        "topic": "front radar calibration prerequisites",
    }


@pytest.mark.asyncio
async def test_alldata_vehicle_research_normalizes_advertised_vehicle_label(
    tmp_path, monkeypatch
):
    browser = research_operator.LicensedBrowser(tmp_path)
    received: dict = {}

    async def structured_search(browser_arg, query="", *, vehicle=None, topic=None):
        assert browser_arg is browser
        assert query == ""
        received.update({"vehicle": vehicle, "topic": topic})
        return {
            "status": "success",
            "searched": True,
            "verified": True,
            "vehicle": vehicle,
            "topic": topic,
        }

    monkeypatch.setattr(
        research_alldata_navigation,
        "search_alldata_vehicle_first",
        structured_search,
    )
    result = await browser.operator_action(
        {
            "action": "alldata_vehicle_research",
            "vehicle": "2023 Chevrolet Tahoe",
            "topic": "forward camera calibration after windshield replacement",
        }
    )
    assert result["verified"] is True
    assert received == {
        "vehicle": {
            "year": "2023",
            "make": "Chevrolet",
            "model": "Tahoe",
            "trim": None,
        },
        "topic": "forward camera calibration after windshield replacement",
    }


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
