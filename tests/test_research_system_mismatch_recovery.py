from __future__ import annotations

from types import SimpleNamespace

from core.services import research_system_mismatch_recovery as recovery


def _module():
    module = SimpleNamespace()

    def next_instruction(review):  # noqa: ARG001
        return "base instruction"

    def system_prompt(*args, **kwargs):  # noqa: ARG001
        return "base prompt"

    module._next_instruction_for_review = next_instruction
    module._system_prompt = system_prompt
    return module


def test_family_mismatch_replaces_generic_keep_searching_with_branch_exit():
    module = _module()
    recovery.install(module)

    instruction = module._next_instruction_for_review(
        {
            "classification": "ACTUAL_PROCEDURE",
            "decision": "CONTINUE_SEARCH",
            "evidence_summary": "Wrong ADAS sensor family: camera instead of radar.",
            "system_family_check": {
                "objective_family": "radar",
                "candidate_family": "camera",
                "procedure_type": "DYNAMIC_CAMERA",
            },
        }
    )

    assert not instruction.startswith("base instruction")
    assert "SENSOR-FAMILY MISMATCH" in instruction
    assert "Leave this branch" in instruction
    assert "alternative ADAS systems/components" in instruction
    assert "do not continue deeper" in instruction.casefold()
    assert "camera" in instruction and "radar" in instruction


def test_real_but_wrong_component_procedure_exits_branch_even_with_same_radar_family():
    """The live Nissan failure: front ICC procedure for a rear BSM objective."""
    module = _module()
    recovery.install(module)

    instruction = module._next_instruction_for_review(
        {
            "classification": "ACTUAL_PROCEDURE",
            "procedure_type": "STATIC_RADAR",
            "decision": "CONTINUE_SEARCH",
            "evidence_summary": (
                "This is a real distance-sensor alignment procedure, but it is not the "
                "requested rear blind-spot side-radar procedure."
            ),
        }
    )

    assert "WRONG PROCEDURE/COMPONENT BRANCH" in instruction
    assert "Leave this branch" in instruction
    assert "alternative ADAS systems/components" in instruction
    assert "fixed menu path" in instruction


def test_rejected_real_wrong_component_also_exits_branch():
    module = _module()
    recovery.install(module)

    instruction = module._next_instruction_for_review(
        {
            "classification": "ACTUAL_PROCEDURE",
            "procedure_type": "BLIND_SPOT_RADAR",
            "decision": "REJECT",
            "evidence_summary": "Procedure is for a different component than requested.",
        }
    )

    assert "WRONG PROCEDURE/COMPONENT BRANCH" in instruction
    assert "Do not extract this page again" in instruction


def test_non_procedure_continue_search_keeps_existing_instruction():
    module = _module()
    recovery.install(module)

    assert module._next_instruction_for_review({"decision": "CONTINUE_SEARCH"}) == "base instruction"
    assert (
        module._next_instruction_for_review(
            {
                "classification": "GENERAL_DESCRIPTION",
                "procedure_type": "NOT_A_PROCEDURE",
                "decision": "CONTINUE_SEARCH",
            }
        )
        == "base instruction"
    )
    assert (
        module._next_instruction_for_review(
            {
                "classification": "ACTUAL_PROCEDURE",
                "decision": "ACCEPT",
                "system_family_check": {
                    "objective_family": "radar",
                    "candidate_family": "radar",
                },
            }
        )
        == "base instruction"
    )


def test_prompt_carries_branch_level_recovery_contract_without_fixed_provider_path():
    module = _module()
    recovery.install(module)

    prompt = module._system_prompt({}, "topic")

    assert "WRONG-PROCEDURE RECOVERY CONTRACT" in prompt
    assert "alternative ADAS systems/components" in prompt
    assert "live controls" in prompt
    assert "invent provider-specific labels" in prompt
