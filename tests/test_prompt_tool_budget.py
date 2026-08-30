from __future__ import annotations

from types import SimpleNamespace

import yaml

from core.config import Settings
from core.main import configured_profile_catalog
from core.orchestrator import prompt
from core.orchestrator.loop import (
    NO_TOOL_SELF_CHECK_MESSAGE,
    NO_TOOL_SELF_CHECK_REQUIRED_MESSAGE,
    no_tool_self_check_reserve_tokens,
)


class _OmniRouter:
    def active_config(self):
        return SimpleNamespace(supports_vision=True, supports_audio=True)


def _catalog() -> list[dict]:
    return configured_profile_catalog(
        Settings.load(),
        role="owner",
        profile="adas_operator",
    )


def _configured_profile_tool_count() -> int:
    settings = Settings.load()
    raw = yaml.safe_load(settings.tools_config.read_text(encoding="utf-8"))
    return len(raw["profiles"]["adas_operator"]["tools"])


def _normal_metrics(*, active_subject=None, history=None) -> dict:
    settings = Settings.load()
    return prompt.prompt_budget_metrics(
        _OmniRouter(),
        _catalog(),
        context_tokens=settings.context_tokens,
        reserve_for_response=settings.max_response_tokens,
        active_subject=active_subject,
        history=history,
    )


def test_adas_profile_prompt_and_catalog_budget_is_reviewable_and_bounded() -> None:
    metrics = _normal_metrics()
    section_names = set(metrics["system_sections"])

    assert section_names == {
        "identity",
        "model_first_contract",
        "truth_and_authorization",
        "working_context",
        "adas_source_roles",
        "operator_truth",
        "active_worker",
        "current_time",
    }
    assert metrics["advertised_tools"]["count"] == _configured_profile_tool_count()
    assert metrics["advertised_tools"]["count"] <= 35
    assert metrics["base_system"]["chars"] <= 6_000
    assert metrics["base_system"]["tokens"] <= 1_800
    assert metrics["advertised_tools"]["catalog_chars"] <= 42_800
    assert metrics["advertised_tools"]["catalog_tokens"] <= 12_300
    # camera_footage's system-prompt guidance grew slightly (range_narrowed
    # coverage-honesty rule, see camera_security.camera_footage_analyze);
    # these ceilings/floors moved with it, not toward zero headroom.
    assert metrics["total_input_used_tokens"] <= 13_900
    assert metrics["remaining_normal_turn_tokens"] >= 17_300


def test_adas_profile_contains_field_surface_not_experimental_catalog_noise() -> None:
    names = {item["function"]["name"] for item in _catalog()}
    required = {
        "assistant_capabilities_read",
        "calibration_iq_summary",
        "calibration_iq_read",
        "calibration_iq_ro",
        "calibration_iq_operator",
        "calibration_iq_destructive",
        "calibration_iq_work_prep",
        "adas_si_search",
        "adas_si_inventory",
        "adas_si_open",
        "automotive_knowledge_search",
        "automotive_knowledge_read",
        "scrapex_status",
        "scrapex_read",
        "scrapex_adas_map",
        "get_calendar",
        "list_tasks",
    }
    experimental = {
        "website_preview_generate",
        "image_generate",
        "video_generate",
        "run_powershell",
        "web_research_current",
    }

    assert required <= names
    assert names.isdisjoint(experimental)


def test_working_context_and_stored_artifacts_have_visible_section_budgets() -> None:
    active_subject = {
        "version": 9,
        "updated_at": "2026-08-26T12:00:00+00:00",
        "source_tool_name": "calibration_iq_ro",
        "payload": {
            "type": "calibration_iq.repair_order",
            "resource_id": "ro-budget-1",
            "repair_order_id": "ro-budget-1",
            "ro_number": "2400911999",
            "vehicle": {
                "year": 2024,
                "make": "Toyota",
                "model": "Camry",
            },
            "known_blockers": ["alignment"] * 200,
        },
    }
    history = [
        {
            "id": index,
            "role": "assistant",
            "content": f"Prior result {index}",
            "worker_used": "omni",
            "artifacts": [
                {
                    "type": "calibration_iq_work_prep",
                    "data": {
                        "marker": index,
                        "repair_orders": [
                            {
                                "ro_number": f"24009{item:05d}",
                                "vehicle": "2024 Toyota Camry " + "x" * 200,
                            }
                            for item in range(50)
                        ],
                    },
                }
            ],
        }
        for index in range(30)
    ]
    metrics = _normal_metrics(active_subject=active_subject, history=history)

    assert (
        0
        < metrics["active_working_context"]["chars"]
        <= (prompt.ACTIVE_SUBJECT_CONTEXT_MAX_CHARS)
    )
    assert (
        0
        < metrics["stored_artifact_context"]["chars"]
        <= (prompt.ARTIFACT_CONTEXT_MAX_CHARS)
    )
    summed_sections = (
        metrics["base_system"]["tokens"]
        + metrics["active_working_context"]["tokens"]
        + metrics["stored_artifact_context"]["tokens"]
    )
    # Separating sections adds four newline characters; conservative integer
    # rounding can move the combined estimate by a token.
    assert abs(metrics["fixed_prompt"]["tokens"] - summed_sections) <= 2
    # The camera_footage system-prompt guidance grew to require analysis:
    # true for temporal/action questions, and to require surfacing a
    # range_narrowed result honestly (see camera_security.
    # camera_footage_analyze); this floor moved down with it, not toward
    # zero headroom.
    assert metrics["remaining_normal_turn_tokens"] >= 15_100


def test_budget_metrics_match_actual_generated_system_prompt() -> None:
    tools = _catalog()
    settings = Settings.load()
    metrics = prompt.prompt_budget_metrics(
        _OmniRouter(),
        tools,
        context_tokens=settings.context_tokens,
        reserve_for_response=settings.max_response_tokens,
    )
    generated = prompt.system_prompt(_OmniRouter())

    assert metrics["base_system"] == {
        "chars": len(generated),
        "tokens": prompt.estimate_tokens(generated),
    }
    assert metrics["remaining_normal_turn_tokens"] == (
        settings.context_tokens
        - settings.max_response_tokens
        - metrics["total_input_used_tokens"]
    )


def test_packed_turn_reserves_exact_serialized_tool_catalog_budget() -> None:
    tools = _catalog()
    settings = Settings.load()
    history = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"turn-{index}:" + ("x" * 4_000),
            "artifacts": [],
        }
        for index in range(80)
    ]

    messages = prompt.build_messages(
        _OmniRouter(),
        history,
        settings.context_tokens,
        settings.max_response_tokens,
        tools=tools,
    )
    packed_prompt_tokens = prompt.estimate_tokens(messages[0]["content"]) + sum(
        prompt.estimate_tokens(message["content"]) + 8 for message in messages[1:]
    )
    catalog_tokens = prompt.estimate_tool_catalog_tokens(tools)

    assert len(messages) < len(history) + 1
    assert (
        packed_prompt_tokens + catalog_tokens + settings.max_response_tokens
        <= settings.context_tokens
    )
    metrics = _normal_metrics()
    assert metrics["advertised_tools"]["catalog_tokens"] == catalog_tokens
    assert metrics["advertised_tools"]["catalog_chars"] == len(
        prompt.serialized_tool_catalog(tools)
    )


def test_packed_turn_reserves_bounded_no_tool_review_request() -> None:
    tools = _catalog()
    settings = Settings.load()
    self_check_reserve = no_tool_self_check_reserve_tokens(
        settings.max_response_tokens
    )
    history = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"turn-{index}:" + ("x" * 4_000),
            "artifacts": [],
        }
        for index in range(80)
    ]

    messages = prompt.build_messages(
        _OmniRouter(),
        history,
        settings.context_tokens,
        settings.max_response_tokens,
        tools=tools,
        extra_input_reserve_tokens=self_check_reserve,
    )
    packed_prompt_tokens = prompt.estimate_tokens(messages[0]["content"]) + sum(
        prompt.estimate_tokens(message["content"]) + 8
        for message in messages[1:]
    )
    catalog_tokens = prompt.estimate_tool_catalog_tokens(tools)

    assert self_check_reserve > settings.max_response_tokens
    assert self_check_reserve >= (
        settings.max_response_tokens
        + max(
            prompt.estimate_tokens(NO_TOOL_SELF_CHECK_MESSAGE),
            prompt.estimate_tokens(NO_TOOL_SELF_CHECK_REQUIRED_MESSAGE),
        )
        + 24
    )
    assert (
        packed_prompt_tokens
        + catalog_tokens
        + settings.max_response_tokens
        + self_check_reserve
        <= settings.context_tokens
    )
    metrics = prompt.prompt_budget_metrics(
        _OmniRouter(),
        tools,
        context_tokens=settings.context_tokens,
        reserve_for_response=settings.max_response_tokens,
        extra_input_reserve_tokens=self_check_reserve,
    )
    assert metrics["extra_input_reserve_tokens"] == self_check_reserve
    assert metrics["remaining_normal_turn_tokens"] == (
        settings.context_tokens
        - settings.max_response_tokens
        - self_check_reserve
        - metrics["total_input_used_tokens"]
    )
