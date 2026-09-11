"""Prompt and tool-catalog budget contract for the permanent model surface.

Numbers here are ceilings measured with the conservative chars/3.5 estimator.
Exact counts against the live Qwen3-Omni vocabulary run about 10-15% lower
for prose and match the estimator for JSON schemas (measured 2026-09-11).
"""

from __future__ import annotations

from types import SimpleNamespace

import yaml

from core.config import Settings
from core.main import configured_profile_catalog
from core.orchestrator import prompt
from core.orchestrator.loop import (
    NO_TOOL_SELF_CHECK_MESSAGE,
    no_tool_self_check_reserve_tokens,
)
from core.tools.meta import MAX_UNLOCKED_TOOLS, PERMANENT_TOOLS
from core.tools.registry import Registry


class _OmniRouter:
    def active_config(self):
        return SimpleNamespace(supports_vision=True, supports_audio=True)


def _registry() -> Registry:
    settings = Settings.load()
    configured_profile_catalog(settings, role="owner", profile="adas_operator")
    return Registry(settings.tools_config, profile="adas_operator")


def _permanent_catalog() -> list[dict]:
    return _registry().permanent_catalog()


def _reachable_catalog() -> list[dict]:
    return configured_profile_catalog(
        Settings.load(),
        role="owner",
        profile="adas_operator",
    )


def _configured_profile_lists() -> tuple[list[str], list[str]]:
    settings = Settings.load()
    raw = yaml.safe_load(settings.tools_config.read_text(encoding="utf-8"))
    entry = raw["profiles"]["adas_operator"]
    return list(entry["permanent"]), list(entry["tools"])


def _normal_metrics(*, tools=None, active_subject=None, history=None) -> dict:
    settings = Settings.load()
    return prompt.prompt_budget_metrics(
        _OmniRouter(),
        tools if tools is not None else _permanent_catalog(),
        context_tokens=settings.context_tokens,
        reserve_for_response=settings.max_response_tokens,
        active_subject=active_subject,
        history=history,
    )


def test_permanent_surface_is_exactly_the_four_meta_tools() -> None:
    permanent, reachable = _configured_profile_lists()
    catalog_names = [item["function"]["name"] for item in _permanent_catalog()]

    assert permanent == list(PERMANENT_TOOLS)
    assert catalog_names == list(PERMANENT_TOOLS)
    assert set(permanent) <= set(reachable)
    # Raw Calibration IQ read/write tools are reachable only through the
    # meta surface; they are not part of the profile at all.
    for name in (
        "calibration_iq_ro",
        "calibration_iq_summary",
        "calibration_iq_read",
        "calibration_iq_work_prep",
        "calibration_iq_status",
        "calibration_iq_operator",
        "calibration_iq_destructive",
    ):
        assert name not in reachable


def test_permanent_catalog_and_static_prompt_budgets_are_bounded() -> None:
    metrics = _normal_metrics()

    assert set(metrics["system_sections"]) == {
        "identity",
        "model_first_contract",
        "truth_and_authorization",
        "working_context",
        "operator_truth",
        "active_worker",
    }
    assert set(metrics["turn_context_sections"]) == {"current_time"}
    assert metrics["advertised_tools"]["count"] == len(PERMANENT_TOOLS)
    # Permanent schemas, measured 2026-09-11: 7,815 chars; estimator 2,233
    # tokens; exact Qwen3-Omni tokens ~2,150 through the live chat template.
    # The review's 1,500-token target is not met: the 55-operation
    # stage_action enum, its operation glossary, and the 11-value status enum
    # are the remainder and are what let llama.cpp's grammar constrain those
    # fields. The old 33-tool reserve was ~12,300 exact tokens.
    assert metrics["advertised_tools"]["catalog_tokens"] < 2_350
    assert metrics["advertised_tools"]["catalog_chars"] < 8_200
    # Static system prompt, measured: 4,474 chars, estimator 1,279, exact ~910.
    assert metrics["base_system"]["chars"] < 4_700
    assert metrics["base_system"]["tokens"] < 1_350
    assert metrics["total_input_used_tokens"] < 3_700
    assert metrics["remaining_normal_turn_tokens"] > 27_000


def test_budget_reserve_covers_permanent_plus_largest_unlockable_set() -> None:
    registry = _registry()
    reserve = registry.budget_reserve_tools()
    reserve_names = [item["function"]["name"] for item in reserve]
    discoverable = registry.discoverable_catalog()
    discoverable_names = {item["function"]["name"] for item in discoverable}

    assert reserve_names[: len(PERMANENT_TOOLS)] == list(PERMANENT_TOOLS)
    assert len(reserve_names) == len(PERMANENT_TOOLS) + MAX_UNLOCKED_TOOLS
    assert set(reserve_names[len(PERMANENT_TOOLS):]) <= discoverable_names
    reserve_tokens = prompt.estimate_tool_catalog_tokens(reserve)
    permanent_tokens = prompt.estimate_tool_catalog_tokens(registry.permanent_catalog())
    assert permanent_tokens < reserve_tokens < 6_500
    # The full reachable catalog is much larger than what any single turn can
    # see: discovery is what keeps the permanent prompt small.
    reachable_tokens = prompt.estimate_tool_catalog_tokens(_reachable_catalog())
    assert reachable_tokens > reserve_tokens


def test_static_system_prompt_is_cache_stable_and_volatile_context_trails_history() -> None:
    settings = Settings.load()
    active_subject = {
        "version": 9,
        "updated_at": "2026-08-26T12:00:00+00:00",
        "source_tool_name": "calibration_iq_ro",
        "payload": {
            "type": "calibration_iq.repair_order",
            "resource_id": "ro-budget-1",
            "ro_number": "2400911999",
        },
    }
    history = [
        {"id": 1, "role": "user", "content": "Pull up 11999 in Macon.", "artifacts": []},
        {
            "id": 2,
            "role": "assistant",
            "content": "Here it is.",
            "artifacts": [{"type": "calibration_iq_ro", "data": {"ro_number": "2400911999"}}],
        },
        {"id": 3, "role": "user", "content": "What phase is it in now?", "artifacts": []},
    ]
    messages = prompt.build_messages(
        _OmniRouter(),
        history,
        settings.context_tokens,
        settings.max_response_tokens,
        active_subject=active_subject,
        tools=_permanent_catalog(),
    )

    static_system = messages[0]["content"]
    assert static_system == prompt.system_prompt(_OmniRouter())
    assert "## Right now" not in static_system
    assert "<active_subject_json>" not in static_system
    assert "<stored_artifacts_json>" not in static_system
    # [system, user1, assistant, turn-context, user3]
    assert [message["role"] for message in messages] == [
        "system", "user", "assistant", "system", "user",
    ]
    context = messages[-2]["content"]
    assert context.startswith("## Right now")
    assert "<active_subject_json>" in context
    assert "<stored_artifacts_json>" in context
    assert messages[-1] == {"role": "user", "content": "What phase is it in now?"}


def test_budget_metrics_match_actual_generated_system_prompt() -> None:
    tools = _permanent_catalog()
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


def _packed_tokens(messages: list[dict]) -> int:
    return sum(
        prompt.estimate_tokens(message["content"]) + 8 for message in messages
    )


def test_packed_turn_reserves_exact_serialized_tool_catalog_budget() -> None:
    registry = _registry()
    tools = registry.budget_reserve_tools()
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
    catalog_tokens = prompt.estimate_tool_catalog_tokens(tools)

    assert len(messages) < len(history) + 2
    assert (
        _packed_tokens(messages) + catalog_tokens + settings.max_response_tokens
        <= settings.context_tokens
    )
    metrics = _normal_metrics(tools=tools)
    assert metrics["advertised_tools"]["catalog_tokens"] == catalog_tokens
    assert metrics["advertised_tools"]["catalog_chars"] == len(
        prompt.serialized_tool_catalog(tools)
    )


def test_packed_turn_reserves_bounded_no_tool_review_request() -> None:
    tools = _permanent_catalog()
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
    catalog_tokens = prompt.estimate_tool_catalog_tokens(tools)

    assert self_check_reserve > settings.max_response_tokens
    assert self_check_reserve >= (
        settings.max_response_tokens
        + prompt.estimate_tokens(NO_TOOL_SELF_CHECK_MESSAGE)
        + 24
    )
    assert (
        _packed_tokens(messages)
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
            "vehicle": {"year": 2024, "make": "Toyota", "model": "Camry"},
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

    assert 0 < metrics["active_working_context"]["chars"] <= prompt.ACTIVE_SUBJECT_CONTEXT_MAX_CHARS
    assert 0 < metrics["stored_artifact_context"]["chars"] <= prompt.ARTIFACT_CONTEXT_MAX_CHARS
    assert set(metrics["turn_context_sections"]) == {
        "current_time", "active_subject", "stored_artifacts",
    }
    summed_sections = (
        metrics["base_system"]["tokens"]
        + metrics["turn_context"]["tokens"]
    )
    assert abs(metrics["fixed_prompt"]["tokens"] - summed_sections) <= 2
    assert metrics["remaining_normal_turn_tokens"] >= 25_000
