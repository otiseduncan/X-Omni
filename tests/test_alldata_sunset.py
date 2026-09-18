"""ALLDATA is sunset: preserved in source control, not executable.

These prove the hard boundary. No research source order, model schema, prompt,
or profile names ALLDATA; capability_search cannot find it; the gateway blocks
it whatever the policy file says; and every runtime entry point that could open
the licensed browser, read the saved credential, or ask ScrapeX to drive an
ALLDATA Navigator task refuses before it touches anything. The code, tests,
captures, and provenance are still in the repository.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from core.config import Settings
from core.main import configured_profile_catalog
from core.orchestrator import prompt
from core.services import adas_artifact_catalog
from core.services import alldata_sunset
from core.services import calibration_iq_work_prep as work_prep
from core.services import research_alldata_agent
from core.services import research_alldata_navigation
from core.services import research_alldata_quick_reference as quick
from core.services import research_delegate
from core.services import research_navigator_agent
from core.services import research_operator
from core.services import research_workflow
from core.services import scrapex
from core.tools import meta
from core.tools.registry import TOOL_SCHEMAS, Registry, ToolBlocked

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "config" / "tools.yaml"


def test_the_sunset_is_a_code_constant_not_a_setting(monkeypatch) -> None:
    assert alldata_sunset.ALLDATA_SUNSET is True
    monkeypatch.setenv("XOMNI_ALLDATA_NAVIGATOR_ENABLED", "1")
    assert Settings.load().alldata_navigator_enabled is False
    assert Settings.__dataclass_fields__["alldata_navigator_enabled"].default is False
    config_source = (ROOT / "core" / "config.py").read_text(encoding="utf-8")
    assert "XOMNI_ALLDATA_NAVIGATOR_ENABLED" not in config_source


def test_alldata_is_in_no_source_order_schema_or_prompt() -> None:
    assert "alldata" not in research_delegate.DEFAULT_SOURCE_ORDER
    assert "alldata" not in meta.RESEARCH_SOURCES
    properties = meta.DELEGATE_RESEARCH_SCHEMA["parameters"]["properties"]
    assert "alldata" not in properties["sources"]["items"]["enum"]
    for schema in meta.meta_tool_schemas().values():
        assert "alldata" not in str(schema).casefold()
    static_prompt = prompt.system_prompt(SimpleNamespace(active_config=lambda: None))
    assert "alldata" not in static_prompt.casefold()


@pytest.mark.parametrize("profile", ["adas_operator", "full"])
def test_no_profile_advertises_or_discovers_an_alldata_capability(profile: str) -> None:
    settings = SimpleNamespace(tools_config=POLICY, tool_profile="adas_operator")
    names = {item["function"]["name"] for item in configured_profile_catalog(settings, profile=profile)}
    assert names.isdisjoint(alldata_sunset.SUNSET_TOOLS)
    for name in names:
        description = str(TOOL_SCHEMAS.get(name, {}).get("description") or "")
        assert "licensed alldata" not in description.casefold(), name


def test_the_gateway_blocks_every_sunset_tool_even_if_policy_grants_it(tmp_path: Path) -> None:
    raw = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    for name in alldata_sunset.SUNSET_TOOLS:
        assert raw["tools"].get(name, {"tier": "blocked"})["tier"] == "blocked", name
        raw["tools"][name] = {"tier": "operator_authorized", "description": "re-enabled by a config edit"}
    raw["default_profile"] = "full"
    policy = tmp_path / "tools.yaml"
    policy.write_text(yaml.safe_dump(raw), encoding="utf-8")
    registry = Registry(policy)
    ran: list[str] = []
    for name in alldata_sunset.SUNSET_TOOLS:
        registry.register(name, lambda _args, _name=name: ran.append(_name))
        assert registry.tier(name) == "blocked"
    assert {item["function"]["name"] for item in registry.model_tools()}.isdisjoint(alldata_sunset.SUNSET_TOOLS)
    assert {item["function"]["name"] for item in registry.capability_catalog()}.isdisjoint(alldata_sunset.SUNSET_TOOLS)
    for name in alldata_sunset.SUNSET_TOOLS:
        with pytest.raises(ToolBlocked):
            asyncio.run(registry.invoke(name, {}))
    assert ran == []


def test_capability_search_cannot_find_alldata() -> None:
    from core.tools.builtin import system as builtin

    for profile in ("adas_operator", "full"):
        registry = Registry(POLICY, profile=profile)
        for name in alldata_sunset.SUNSET_TOOLS:
            registry.register(name, lambda _args: None)
        search = builtin.make_capability_search(SimpleNamespace(), registry)
        for query in ("alldata", "ALLDATA service information", "licensed research sign in", "navigator", ""):
            result = asyncio.run(search({"query": query}))
            found = {str(item.get("name")) for item in result.get("tools") or []}
            assert found.isdisjoint(alldata_sunset.SUNSET_TOOLS), (profile, query, found)


def _refuses(call: Any, entry: str) -> None:
    with pytest.raises(alldata_sunset.AlldataSunset) as raised:
        result = call()
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    assert raised.value.entry_point == entry


def test_no_browser_session_credential_read_or_navigator_task_can_start(tmp_path: Path) -> None:
    settings = SimpleNamespace(root=tmp_path, scrapex_base_url="http://127.0.0.1:9", scrapex_project_path=tmp_path)
    browser = research_operator.LicensedBrowser(tmp_path)
    _refuses(lambda: browser.start(auto_login=True), "research_operator.licensed_browser_start")
    _refuses(lambda: browser._ensure(), "research_operator.licensed_browser")  # noqa: SLF001
    vault = research_operator.WindowsCredentialVault()
    _refuses(vault.read, "research_operator.credential_read")
    _refuses(lambda: vault.write("user", "secret-password"), "research_operator.credential_write")
    _refuses(
        lambda: scrapex.navigator(settings, {"action": "create_task", "provider": "alldata", "target": {"year": 2025, "make": "Kia", "model": "K4"}, "topic": "radar"}),
        "scrapex_navigator_runtime.navigator",
    )
    _refuses(lambda: scrapex.navigator_current_page_signals(settings, "alldata"), "scrapex.navigator_current_page_signals")
    _refuses(lambda: scrapex.navigator_current_target_signal(settings, "alldata", {"year": 2025}), "scrapex.navigator_current_target_signal")
    _refuses(lambda: scrapex.navigator_screenshot(settings, "task-1"), "scrapex.navigator_screenshot")
    _refuses(lambda: scrapex.navigator_capture(settings, "task-1"), "scrapex.navigator_capture")
    _refuses(
        lambda: research_navigator_agent.run_navigator_search(
            client=object(), settings=settings, provider="alldata", target={"year": 2025}, topic="radar"
        ),
        "research_navigator_agent.run_navigator_search",
    )
    _refuses(lambda: research_alldata_navigation.search_alldata_vehicle_first(browser, "2025 Kia K4 radar"), "research_alldata_navigation.search_alldata_vehicle_first")
    _refuses(
        lambda: research_alldata_agent.run_agent_search(client=object(), browser=browser, vehicle={}, topic="radar"),
        "research_alldata_agent.run_agent_search",
    )
    _refuses(lambda: quick.collect_general_reference(settings, None, {}), "research_alldata_quick_reference.collect_general_reference")
    _refuses(lambda: quick.collect_for_calibration_iq_ro(settings, None, {"repair_order_id": "1"}), "research_alldata_quick_reference.collect_for_calibration_iq_ro")


def test_no_http_route_or_legacy_research_path_reaches_alldata(tmp_path: Path) -> None:
    from fastapi import APIRouter

    router = APIRouter(prefix="/api")
    research_operator.install_http_routes(
        router, SimpleNamespace(root=tmp_path, local_origin="http://127.0.0.1:8100", public_origin=""), lambda: {}
    )
    assert not any("alldata" in route.path for route in router.routes)
    result = asyncio.run(research_workflow._search_alldata_best_available(None, "2025 Kia K4 radar"))  # noqa: SLF001
    assert result["sunset"] is True and result["executed"] is False


def test_work_prep_never_acquires_from_alldata() -> None:
    for mode in ("ro_si_acquire", "queue_next"):
        result = asyncio.run(work_prep.handle(SimpleNamespace(), SimpleNamespace(), {"mode": mode, "repair_order_id": "1"}))
        assert result["sunset"] is True and result["executed"] is False
    gaps = asyncio.run(
        work_prep._acquire_si_gaps(  # noqa: SLF001
            SimpleNamespace(),
            SimpleNamespace(),
            {"vehicle": {"year": 2025, "make": "Kia", "model": "K4"}},
            [{"state": adas_artifact_catalog.MISSING, "calibration": "Front Radar"}],
        )
    )
    assert gaps and all(item["sunset"] is True for item in gaps)


def test_ciq_research_has_no_alldata_fallback() -> None:
    from core.services import adas_si_research

    source = (ROOT / "core" / "services" / "adas_si_research.py").read_text(encoding="utf-8")
    assert "run_navigator_search" not in source
    assert 'provider="alldata"' not in source

    async def nothing_in_the_library(_service, _objective, reviews):
        reviews.append({"title": "Bumper R&I", "outcome": "UNSATISFIED"})
        return None

    service = adas_si_research.AdasSiResearchService(
        SimpleNamespace(), object(), client=object(), library_search=nothing_in_the_library
    )
    result = asyncio.run(
        service._research({"topic": "Blind Spot Monitor", "vehicle": {"year": 2025, "make": "Kia", "model": "K4"}, "vin": "KNAF24A28S5000001"})  # noqa: SLF001
    )
    assert result["outcome"] == "UNSATISFIED" and result["verified"] is False
    assert result["source"] == "adas_si"
    assert "ALLDATA is retired" in result["reason"]


def test_alldata_work_is_preserved_in_source_control() -> None:
    services = ROOT / "core" / "services"
    for name in (
        "research_navigator_agent.py",
        "research_alldata_navigation.py",
        "research_alldata_quick_reference.py",
        "research_alldata_agent.py",
        "research_operator.py",
        "research_verification.py",
        "adas_si_harvest.py",
    ):
        assert (services / name).is_file(), name
    # Historical ALLDATA provenance still files under its own name in ADAS SI.
    from core.services import adas_storage

    assert "ALLDATA" in (ROOT / "core" / "services" / "adas_storage.py").read_text(encoding="utf-8")
    assert adas_storage is not None
    assert os.path.exists(ROOT / "tests" / "test_research_navigator_agent.py")
