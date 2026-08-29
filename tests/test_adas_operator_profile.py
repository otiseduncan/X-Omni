from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from core.config import ROOT, Settings
from core.main import configured_profile_catalog
from core.orchestrator.prompt import prompt_budget_metrics, system_prompt
from core.orchestrator.loop import no_tool_self_check_reserve_tokens
from core.tools.registry import (
    CALIBRATION_IQ_ADD_CALIBRATION_OPERATIONS,
    CALIBRATION_IQ_OTHER_RO_UNVERSIONED_OPERATIONS,
    CALIBRATION_IQ_RESEARCH_RO_OPERATIONS,
    CALIBRATION_IQ_STAGED_WRITE_TOOLS,
    CALIBRATION_IQ_WORKSPACE_DOCUMENT_RO_OPERATIONS,
    Registry,
    calibration_iq_evidence_from_result,
    scrapex_evidence_from_result,
)


POLICY_PATH = ROOT / "config" / "tools.yaml"
EXPECTED_ADAS_TOOLS = {
    "get_calendar",
    "create_calendar_event",
    "list_tasks",
    "add_task",
    "update_task_status",
    "read_file",
    "list_directory",
    "search_files",
    "assistant_capabilities_read",
    "system_status",
    "camera_request",
    "exterior_camera_request",
    "camera_event_history",
    "camera_snapshot_analyze",
    "adas_si_search",
    "adas_si_inventory",
    "adas_si_open",
    "automotive_knowledge_search",
    "automotive_knowledge_read",
    "automotive_knowledge_capture",
    "calibration_iq_status",
    "calibration_iq_start_native",
    "calibration_iq_summary",
    "calibration_iq_read",
    "calibration_iq_ro",
    "calibration_iq_operator",
    "calibration_iq_destructive",
    "calibration_iq_work_prep",
    "collision_research",
    "research_provider_setup",
    "scrapex_status",
    "scrapex_start_native",
    "scrapex_read",
    "scrapex_adas_map",
}
NON_ADAS_NORMAL_TOOLS = {
    "get_weather",
    "web_research_current",
    "website_preview_generate",
    "image_generation_status",
    "image_generate",
    "video_generation_status",
    "video_generate",
    "run_powershell",
    "write_file",
    "adas_si_file_write",
    "adas_si_records",
    "adas_si_record_write",
    "adas_si_record_modify",
    "automotive_knowledge_lifecycle",
    "calibration_iq_update",
}


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        tools_config=POLICY_PATH,
        tool_profile="adas_operator",
    )


def _omni_router() -> SimpleNamespace:
    return SimpleNamespace(
        active_config=lambda: SimpleNamespace(
            supports_vision=True,
            supports_audio=True,
        )
    )


def test_adas_operator_is_the_configured_default_profile() -> None:
    raw = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    configured = set(raw["profiles"]["adas_operator"]["tools"])

    assert raw["default_profile"] == "adas_operator"
    assert configured == EXPECTED_ADAS_TOOLS
    assert configured.isdisjoint(NON_ADAS_NORMAL_TOOLS)


def test_production_profile_catalog_is_read_only_and_handler_independent() -> None:
    adas_catalog = configured_profile_catalog(_settings())
    full_catalog = configured_profile_catalog(_settings(), profile="full")
    adas_names = {item["function"]["name"] for item in adas_catalog}
    full_names = {item["function"]["name"] for item in full_catalog}

    assert adas_names == EXPECTED_ADAS_TOOLS
    assert len(adas_catalog) == 34
    assert len(full_catalog) == 49
    assert NON_ADAS_NORMAL_TOOLS <= full_names


def test_profile_filters_advertising_without_changing_gateway_policy() -> None:
    # Populate service-owned schemas through the same read-only production
    # catalog path used by the acceptance/budget harness.
    configured_profile_catalog(_settings())
    registry = Registry(POLICY_PATH, profile="adas_operator")
    for item in registry.profile_catalog():
        registry.register(item["function"]["name"], lambda _args: {})

    initial_names = {
        item["function"]["name"] for item in registry.model_tools()
    }
    assert initial_names == EXPECTED_ADAS_TOOLS - CALIBRATION_IQ_STAGED_WRITE_TOOLS
    assert {
        item["function"]["name"]
        for item in registry.model_tools(gate_calibration_iq_writes=False)
    } == EXPECTED_ADAS_TOOLS

    evidence = calibration_iq_evidence_from_result(
        "calibration_iq_ro",
        {
            "status": "verified",
            "repair_order": {"id": "ro-1", "RO": "2400911667", "version": 7},
            "raw": {
                "repair_order": {
                    "id": "ro-1", "ro_number": "2400911667", "version": 7,
                },
            },
        },
        conversation_id=1,
        message_id=2,
        source_tool_call_id="exact-ro-call",
    )
    unlocked = registry.model_tools(calibration_iq_evidence=evidence)
    assert {item["function"]["name"] for item in unlocked} == EXPECTED_ADAS_TOOLS
    operator = next(
        item for item in unlocked
        if item["function"]["name"] == "calibration_iq_operator"
    )
    advertised_operations = set().union(*(
        set(branch["properties"]["operation"]["enum"])
        for branch in operator["function"]["parameters"]["properties"]
        ["actions"]["items"]["oneOf"]
    ))
    assert advertised_operations.isdisjoint({"create_ro", "create_location"})
    assert registry.profile_allows_tool("video_generate") is False
    assert registry.tier("video_generate") == "confirm_required"
    assert registry.tier("calibration_iq_destructive") == "confirm_required"


def test_scrapex_catalog_stages_opaque_id_actions_until_verified_result() -> None:
    configured_profile_catalog(_settings())
    registry = Registry(POLICY_PATH, profile="adas_operator")
    for item in registry.profile_catalog():
        registry.register(item["function"]["name"], lambda _args: {})

    def action_branches(catalog: list[dict], tool_name: str) -> set[str]:
        function = next(
            item["function"]
            for item in catalog
            if item["function"]["name"] == tool_name
        )
        return {
            branch["properties"]["action"]["const"]
            for branch in function["parameters"]["oneOf"]
        }

    initial = registry.model_tools()
    assert action_branches(initial, "scrapex_read") == {
        "list_batches",
        "preview_ciq_queue",
    }
    assert action_branches(initial, "scrapex_adas_map") == {
        "open_authentication",
        "create_exact_batch",
        "create_phase_batch",
    }
    for tool_name in ("scrapex_read", "scrapex_adas_map"):
        parameters = next(
            item["function"]["parameters"]
            for item in initial
            if item["function"]["name"] == tool_name
        )
        assert '"batch_id"' not in json.dumps(parameters)

    evidence = scrapex_evidence_from_result(
        "scrapex_read",
        {"action": "list_batches"},
        {
            "service": "ScrapeX",
            "action": "list_batches",
            "status": "verified",
            "success": True,
            "executed": True,
            "verified": True,
            "data": {"batches": [{"id": "batch-observed-7"}]},
        },
        conversation_id=1,
        message_id=2,
        source_tool_call_id="list-call",
    )
    assert evidence is not None
    unlocked = registry.model_tools(scrapex_evidence=evidence)
    assert action_branches(unlocked, "scrapex_read") == {
        "list_batches",
        "preview_ciq_queue",
        "batch_summary",
        "batch_exceptions",
        "batch_item",
    }
    assert action_branches(unlocked, "scrapex_adas_map") == {
        "open_authentication",
        "create_exact_batch",
        "create_phase_batch",
        "process_one",
        "start_batch",
        "pause_batch",
    }
    full_for_budget = registry.model_tools(gate_scrapex_batch_ids=False)
    assert action_branches(full_for_budget, "scrapex_read") == action_branches(
        unlocked, "scrapex_read"
    )
    assert action_branches(full_for_budget, "scrapex_adas_map") == action_branches(
        unlocked, "scrapex_adas_map"
    )


def test_unknown_or_malformed_profiles_fail_closed(tmp_path) -> None:
    with pytest.raises(ValueError, match="Unknown or invalid tool profile"):
        Registry(POLICY_PATH, profile="missing")

    malformed = tmp_path / "tools.yaml"
    malformed.write_text(
        "default_profile: broken\n"
        "profiles:\n"
        "  broken:\n"
        "    tools: [unknown_tool]\n"
        "roots: []\n"
        "write_roots: []\n"
        "tools: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unconfigured tools"):
        Registry(malformed)


def test_settings_can_select_an_explicit_maintenance_profile(monkeypatch) -> None:
    monkeypatch.setenv("XOMNI_TOOL_PROFILE", "full")
    assert Settings.load().tool_profile == "full"


def test_normal_prompt_is_concise_and_free_of_capability_micro_routing() -> None:
    prompt = system_prompt(_omni_router())

    assert len(prompt) < 5_000
    assert "model-first and tool contract" in prompt.casefold()
    assert "adas source roles" in prompt.casefold()
    assert "mutations require a direct current-turn command" in prompt.casefold()
    assert "demonstration requests never authorize one" in prompt.casefold()
    assert "use `assistant_capabilities_read`" in prompt
    assert "exact-number list match is a thin row" in prompt.casefold()
    assert "current_calibration_detail_included=false" in prompt
    assert "model memory and current ciq assignments are not oem evidence" in prompt.casefold()
    assert "before any schema-versioned write" in prompt.casefold()
    assert "close_ro` is the normal whole-ro finished/complete transition" in prompt.casefold()
    assert "change_status` is only for an explicitly named target status" in prompt.casefold()
    assert "complete_calibration` only for an explicit child-state request" in prompt.casefold()
    assert "when otis asks" not in prompt.casefold()
    for tool_name in NON_ADAS_NORMAL_TOOLS:
        assert tool_name not in prompt


def test_read_status_and_exact_resource_descriptions_expose_distinct_contracts() -> None:
    catalog = {
        item["function"]["name"]: item["function"]["description"]
        for item in configured_profile_catalog(_settings())
    }

    assert "Primary read for whether X is configured and permitted" in catalog[
        "assistant_capabilities_read"
    ]
    assert "performs no business action" in catalog["assistant_capabilities_read"]
    assert "model-worker and GPU health only" in catalog["system_status"]
    assert "Collection/list read" in catalog["calibration_iq_read"]
    assert "exact RO-number q" in catalog["calibration_iq_read"]
    assert "Exact-resource read" in catalog["calibration_iq_ro"]
    assert "not OEM trigger" in catalog["calibration_iq_ro"]
    assert "WRITE only for a direct current-turn command" in catalog[
        "calibration_iq_operator"
    ]
    assert "change_status is only for an explicitly named target status" in catalog[
        "calibration_iq_operator"
    ]


def test_production_profile_catalog_exposes_disjoint_unversioned_action_families() -> None:
    operator = next(
        item["function"]
        for item in configured_profile_catalog(_settings())
        if item["function"]["name"] == "calibration_iq_operator"
    )
    branches = operator["parameters"]["properties"]["actions"]["items"]["oneOf"]

    def operations_for(expected: set[str]) -> dict:
        matches = [
            branch
            for branch in branches
            if set(branch["properties"]["operation"]["enum"]) == expected
        ]
        assert len(matches) == 1
        return matches[0]

    research = operations_for(set(CALIBRATION_IQ_RESEARCH_RO_OPERATIONS))
    add = operations_for(set(CALIBRATION_IQ_ADD_CALIBRATION_OPERATIONS))
    exact_groups = (
        {"ensure_case_workspace"},
        {"create_folder", "archive_entry"},
        {"rename_entry"},
        {"move_entry", "copy_entry"},
        {"create_file"},
        {"restore_entry"},
        {"import_document"},
        {"import_photo"},
        {"add_note"},
        {"add_blocker"},
        {"add_prerequisite"},
        {"create_assessment"},
    )
    for group in exact_groups:
        operations_for(group)
    assert set().union(*exact_groups) == (
        set(CALIBRATION_IQ_WORKSPACE_DOCUMENT_RO_OPERATIONS)
        | set(CALIBRATION_IQ_OTHER_RO_UNVERSIONED_OPERATIONS)
    )
    assert "source/page docs" in research["description"]
    assert "never add" in research["description"]
    assert "never attach evidence" in add["description"]


def test_prompt_and_profile_budget_remain_visible_and_bounded() -> None:
    tools = configured_profile_catalog(_settings())
    active_subject = {
        "version": 7,
        "source_tool_name": "calibration_iq_ro",
        "payload": {
            "type": "calibration_iq.repair_order",
            "resource_id": "ro-uuid-17",
            "ro_number": "2400911724",
            "vehicle": {"year": 2023, "make": "Chevrolet", "model": "Tahoe"},
        },
    }
    history = [
        {
            "id": 11,
            "role": "assistant",
            "content": "The OEM source was found.",
            "artifacts": [
                {
                    "type": "adas_si_document",
                    "data": {
                        "title": "Forward Camera Learn Procedure",
                        "relative_path": "Chevrolet/Tahoe/camera.pdf",
                        "page": 9,
                    },
                }
            ],
        }
    ]

    self_check_reserve = no_tool_self_check_reserve_tokens(1_536)
    metrics = prompt_budget_metrics(
        _omni_router(),
        tools,
        context_tokens=32_768,
        reserve_for_response=1_536,
        extra_input_reserve_tokens=self_check_reserve,
        active_subject=active_subject,
        history=history,
    )

    assert metrics["base_system"]["chars"] < 5_000
    assert metrics["base_system"]["tokens"] < 1_450
    assert metrics["active_working_context"]["chars"] > 0
    assert metrics["active_working_context"]["chars"] <= 2_400
    assert metrics["stored_artifact_context"]["chars"] > 0
    assert metrics["stored_artifact_context"]["chars"] <= 8_000
    assert metrics["advertised_tools"]["count"] == 34
    assert metrics["advertised_tools"]["catalog_chars"] < 42_200
    assert metrics["advertised_tools"]["catalog_tokens"] < 12_100
    # Visibility ceiling with regression headroom; the independent remaining
    # context floor below is the authoritative 32K safety contract.
    assert metrics["total_input_used_tokens"] < 13_900
    assert metrics["extra_input_reserve_tokens"] == self_check_reserve
    assert metrics["remaining_normal_turn_tokens"] > 15_600
    assert set(metrics["system_sections"]) == {
        "identity",
        "model_first_contract",
        "truth_and_authorization",
        "working_context",
        "adas_source_roles",
        "operator_truth",
        "active_worker",
        "current_time",
    }
