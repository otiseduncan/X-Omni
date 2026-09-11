from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from core.config import ROOT, Settings
from core.main import configured_profile_catalog
from core.orchestrator.prompt import prompt_budget_metrics, system_prompt
from core.orchestrator.loop import no_tool_self_check_reserve_tokens
from core.tools.meta import PERMANENT_TOOLS, stage_operations
from core.tools.registry import (
    CALIBRATION_IQ_ADD_CALIBRATION_OPERATIONS,
    CALIBRATION_IQ_DESTRUCTIVE_OPERATIONS,
    CALIBRATION_IQ_OTHER_RO_UNVERSIONED_OPERATIONS,
    CALIBRATION_IQ_RESEARCH_RO_OPERATIONS,
    CALIBRATION_IQ_ROUTINE_OPERATIONS,
    CALIBRATION_IQ_STAGED_WRITE_TOOLS,
    CALIBRATION_IQ_WORKSPACE_DOCUMENT_RO_OPERATIONS,
    TOOL_SCHEMAS,
    NeedsApproval,
    Registry,
    ToolBlocked,
    calibration_iq_evidence_from_result,
    scrapex_evidence_from_result,
)


POLICY_PATH = ROOT / "config" / "tools.yaml"
PERMANENT = set(PERMANENT_TOOLS)
DISCOVERABLE_ADAS_TOOLS = {
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
    "camera_footage",
    "adas_si_search",
    "adas_si_inventory",
    "adas_si_open",
    "automotive_knowledge_search",
    "automotive_knowledge_read",
    "automotive_knowledge_capture",
    "calibration_iq_start_native",
    "scrapex_status",
    "scrapex_start_native",
    "scrapex_read",
    "scrapex_adas_map",
}
EXPECTED_ADAS_TOOLS = PERMANENT | DISCOVERABLE_ADAS_TOOLS
# Reachable only through the meta surface (query_ciq / stage_action expand to
# them inside the gateway) or through the explicit full maintenance profile.
META_WRAPPED_CIQ_TOOLS = {
    "calibration_iq_status",
    "calibration_iq_summary",
    "calibration_iq_read",
    "calibration_iq_ro",
    "calibration_iq_work_prep",
    "calibration_iq_operator",
    "calibration_iq_destructive",
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
    "collision_research",
    "service_information_research",
    "alldata_service_information",
    "research_provider_setup",
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


def _registered(profile: str = "adas_operator") -> Registry:
    configured_profile_catalog(_settings())
    registry = Registry(POLICY_PATH, profile=profile)
    for item in registry.profile_catalog():
        registry.register(item["function"]["name"], lambda _args: {})
    return registry


def _names(catalog: list[dict]) -> set[str]:
    return {item["function"]["name"] for item in catalog}


def test_adas_operator_is_the_configured_default_profile() -> None:
    raw = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    entry = raw["profiles"]["adas_operator"]
    configured = set(entry["tools"])

    assert raw["default_profile"] == "adas_operator"
    assert entry["permanent"] == list(PERMANENT_TOOLS)
    assert configured == EXPECTED_ADAS_TOOLS
    assert configured.isdisjoint(NON_ADAS_NORMAL_TOOLS)
    assert configured.isdisjoint(META_WRAPPED_CIQ_TOOLS)


def test_production_profile_catalog_is_read_only_and_handler_independent() -> None:
    adas_catalog = configured_profile_catalog(_settings())
    full_catalog = configured_profile_catalog(_settings(), profile="full")
    adas_names = _names(adas_catalog)
    full_names = _names(full_catalog)

    assert adas_names == EXPECTED_ADAS_TOOLS
    assert len(adas_catalog) == 30
    assert len(full_catalog) == 56
    assert NON_ADAS_NORMAL_TOOLS <= full_names
    assert META_WRAPPED_CIQ_TOOLS <= full_names
    assert PERMANENT <= full_names


def test_permanent_surface_is_advertised_and_discovery_unlocks_the_rest() -> None:
    registry = _registered()

    assert _names(registry.model_tools()) == PERMANENT
    assert _names(registry.permanent_catalog()) == PERMANENT
    assert _names(registry.discoverable_catalog()) == DISCOVERABLE_ADAS_TOOLS
    assert registry.unlockable_tool_names() == frozenset(DISCOVERABLE_ADAS_TOOLS)

    unlocked = registry.model_tools(unlocked=["get_calendar", "camera_footage"])
    assert _names(unlocked) == PERMANENT | {"get_calendar", "camera_footage"}
    # Permanent tools always come first so the cached prefix stays stable.
    assert [item["function"]["name"] for item in unlocked][:4] == list(PERMANENT_TOOLS)

    # Raw write tools can never be unlocked, even by name; stage_action owns them.
    for raw_write in CALIBRATION_IQ_STAGED_WRITE_TOOLS:
        registry.register(raw_write, lambda _args: {})
    assert _names(registry.model_tools(unlocked=list(CALIBRATION_IQ_STAGED_WRITE_TOOLS))) == PERMANENT
    # Unknown names are ignored rather than advertised.
    assert _names(registry.model_tools(unlocked=["not_a_tool"])) == PERMANENT
    # Budget overrides do not widen the advertised surface either.
    assert _names(
        registry.model_tools(gate_calibration_iq_writes=False, gate_scrapex_batch_ids=False)
    ) == PERMANENT


def test_full_profile_keeps_staged_write_gating_without_a_permanent_surface() -> None:
    registry = _registered(profile="full")
    assert registry.permanent_tools is None
    assert registry.discoverable_catalog() == []

    initial = _names(registry.model_tools())
    # The full maintenance profile never staged its raw write tools; it
    # advertises everything, including the meta surface, at all times.
    assert CALIBRATION_IQ_STAGED_WRITE_TOOLS <= initial
    assert PERMANENT <= initial
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
    assert CALIBRATION_IQ_STAGED_WRITE_TOOLS <= _names(unlocked)
    assert registry.profile_allows_tool("video_generate") is True
    assert registry.tier("video_generate") == "confirm_required"
    assert registry.tier("calibration_iq_destructive") == "confirm_required"


async def test_calibration_iq_update_is_blocked_under_the_default_profile() -> None:
    """Advertising exclusion alone is not execution-level quarantine.

    profile_allows_tool only gates profile_catalog/model_tools advertising --
    invoke() previously never consulted it, so a caller that named the
    legacy 'calibration_iq_update' tool directly would still reach its
    handler (and skip the verified-evidence binding that
    calibration_iq_operator/_destructive require) even though the default
    'adas_operator' profile doesn't advertise it.
    """

    registry = Registry(POLICY_PATH, profile="adas_operator")
    registry.register("calibration_iq_update", lambda _args: {"success": True})
    assert registry.profile_allows_tool("calibration_iq_update") is False

    with pytest.raises(ToolBlocked):
        await registry.invoke("calibration_iq_update", {})


async def test_calibration_iq_update_reaches_its_normal_gate_under_the_full_profile() -> None:
    registry = Registry(POLICY_PATH, profile="full")
    registry.register("calibration_iq_update", lambda _args: {"success": True})
    assert registry.profile_allows_tool("calibration_iq_update") is True

    with pytest.raises(NeedsApproval):
        await registry.invoke("calibration_iq_update", {})


def test_scrapex_catalog_stages_opaque_id_actions_until_verified_result() -> None:
    registry = _registered()
    scrapex = ["scrapex_read", "scrapex_adas_map"]

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

    initial = registry.model_tools(unlocked=scrapex)
    assert action_branches(initial, "scrapex_read") == {
        "list_batches",
        "preview_ciq_queue",
    }
    assert action_branches(initial, "scrapex_adas_map") == {
        "open_authentication",
        "acquire_exact",
        "create_exact_batch",
        "create_phase_batch",
    }
    for tool_name in scrapex:
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
    unlocked = registry.model_tools(scrapex_evidence=evidence, unlocked=scrapex)
    assert action_branches(unlocked, "scrapex_read") == {
        "list_batches",
        "preview_ciq_queue",
        "batch_summary",
        "batch_exceptions",
        "batch_item",
    }
    assert action_branches(unlocked, "scrapex_adas_map") == {
        "open_authentication",
        "acquire_exact",
        "create_exact_batch",
        "create_phase_batch",
        "process_one",
        "start_batch",
        "pause_batch",
    }


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

    outside = tmp_path / "outside.yaml"
    outside.write_text(
        "default_profile: narrow\n"
        "profiles:\n"
        "  narrow:\n"
        "    permanent: [list_tasks]\n"
        "    tools: [get_weather]\n"
        "roots: []\n"
        "write_roots: []\n"
        "tools:\n"
        "  get_weather: {tier: read_only}\n"
        "  list_tasks: {tier: read_only}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="outside the profile"):
        Registry(outside)


def test_settings_can_select_an_explicit_maintenance_profile(monkeypatch) -> None:
    monkeypatch.setenv("XOMNI_TOOL_PROFILE", "full")
    assert Settings.load().tool_profile == "full"


def test_normal_prompt_is_concise_and_free_of_capability_micro_routing() -> None:
    prompt = system_prompt(_omni_router())
    folded = prompt.casefold()

    assert len(prompt) < 4_700
    assert "## right now" not in folded
    for tool in PERMANENT_TOOLS:
        assert f"`{tool}`" in prompt
    assert "answer general technical, conceptual, or conversational questions directly" in folded
    assert "mutations require a direct current-turn command" in folded
    assert "a turn that executed nothing has done nothing" in folded
    assert "ciq state is not oem proof" in folded
    assert "is a fresh identification" in folded
    assert "close_ro` is the normal whole-ro finished/complete transition" in folded
    assert "change_status` is only for an explicitly named target status" in folded
    assert "speak ro numbers back the way otis named them" in folded
    assert "when otis asks" not in folded
    for tool_name in NON_ADAS_NORMAL_TOOLS | META_WRAPPED_CIQ_TOOLS | DISCOVERABLE_ADAS_TOOLS:
        assert tool_name not in prompt


def test_meta_tool_descriptions_expose_distinct_contracts() -> None:
    catalog = {
        item["function"]["name"]: item["function"]["description"]
        for item in configured_profile_catalog(_settings())
    }

    for name in META_WRAPPED_CIQ_TOOLS | NON_ADAS_NORMAL_TOOLS:
        assert name not in catalog
    query = catalog["query_ciq"].casefold()
    research = catalog["delegate_research"].casefold()
    stage = catalog["stage_action"].casefold()
    search = catalog["capability_search"].casefold()
    assert "reads never change anything" in query
    assert "adas_map_inventory" in query
    assert "never scrapex" in query
    assert "never changes calibration iq" in research
    assert "with or without an ro" in research
    assert "exclude_sources" in research
    assert "only way to change calibration iq" in stage
    assert "stage=staged" in stage
    assert "raise otis's approval card when executed" in stage
    assert "take no target_id" in stage
    assert "nothing continues automatically after sign-in" in stage
    assert "acquire_adas_map" in stage
    assert "callable for the rest of this turn" in search
    assert "catalog presence is not execution proof" in search


def test_stage_action_lists_every_operator_operation_without_the_grammar() -> None:
    catalog = {
        item["function"]["name"]: item["function"]
        for item in configured_profile_catalog(_settings())
    }
    operations = set(catalog["stage_action"]["parameters"]["properties"]["operation"]["enum"])

    assert operations == set(stage_operations())
    assert {"create_ro", "create_location"}.isdisjoint(operations)
    assert set(CALIBRATION_IQ_DESTRUCTIVE_OPERATIONS) <= operations
    assert set(CALIBRATION_IQ_ROUTINE_OPERATIONS) - {"create_ro", "create_location"} <= operations
    assert {"acquire_adas_map", "open_adas_map_authentication"} <= operations
    encoded = json.dumps(catalog["stage_action"], separators=(",", ":"))
    operator_encoded = json.dumps(TOOL_SCHEMAS["calibration_iq_operator"], separators=(",", ":"))
    assert len(encoded) < 3_300
    assert len(encoded) * 4 < len(operator_encoded)

    full_catalog = {
        item["function"]["name"]: item["function"]
        for item in configured_profile_catalog(_settings(), profile="full")
    }
    full_operations = set(
        full_catalog["stage_action"]["parameters"]["properties"]["operation"]["enum"]
    )
    assert {"create_ro", "create_location"} <= full_operations


def test_operator_schema_still_exposes_disjoint_unversioned_action_families() -> None:
    operator = TOOL_SCHEMAS["calibration_iq_operator"]
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
    registry = _registered()
    tools = registry.permanent_catalog()
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

    # Measured 2026-09-11: 4,474 chars, estimator 1,279 tokens, exact ~910.
    assert metrics["base_system"]["chars"] < 4_700
    assert metrics["base_system"]["tokens"] < 1_350
    assert metrics["active_working_context"]["chars"] > 0
    assert metrics["active_working_context"]["chars"] <= 2_400
    assert metrics["stored_artifact_context"]["chars"] > 0
    assert metrics["stored_artifact_context"]["chars"] <= 8_000
    assert metrics["advertised_tools"]["count"] == 4
    # Measured 2026-09-11: 7,815 chars, estimator 2,233 tokens, exact Qwen3
    # tokens ~2,150 (the 55-operation stage_action enum and its glossary are
    # most of it), down from ~12,300 exact tokens for the old 33-tool reserve.
    assert metrics["advertised_tools"]["catalog_chars"] < 8_200
    assert metrics["advertised_tools"]["catalog_tokens"] < 2_350
    assert metrics["total_input_used_tokens"] < 4_600
    assert metrics["extra_input_reserve_tokens"] == self_check_reserve
    assert metrics["remaining_normal_turn_tokens"] > 25_000
    assert set(metrics["system_sections"]) == {
        "identity",
        "model_first_contract",
        "truth_and_authorization",
        "working_context",
        "operator_truth",
        "active_worker",
    }


def test_research_attach_cannot_request_a_file_and_a_folder_at_once() -> None:
    """destination_path and destination_folder are mutually exclusive.

    Sending both is rejected downstream, which costs a whole operator turn
    and leaves the RO reporting an unverified outcome. Keep it impossible to
    express rather than only caught after dispatch.
    """
    from jsonschema import Draft202012Validator

    schema = TOOL_SCHEMAS["calibration_iq_operator"]["parameters"]
    Draft202012Validator.check_schema(schema)

    matches = []

    def walk(node):
        if isinstance(node, dict):
            properties = node.get("properties")
            if (
                isinstance(properties, dict)
                and "destination_path" in properties
                and "destination_folder" in properties
            ):
                matches.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    assert len(matches) == 1
    validator = Draft202012Validator(matches[0])

    assert validator.is_valid({"query": "camera", "destination_path": "a/b.pdf"})
    assert validator.is_valid({"query": "camera", "destination_folder": "a"})
    assert not validator.is_valid(
        {"query": "camera", "destination_path": "a/b.pdf", "destination_folder": "a"}
    )
