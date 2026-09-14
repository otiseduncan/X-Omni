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


def test_family_mismatch_tells_x_to_leave_the_rejected_sensor_branch():
    module = _module()
    recovery.install(module)

    instruction = module._next_instruction_for_review(
        {
            "decision": "CONTINUE_SEARCH",
            "system_family_check": {
                "objective_family": "radar",
                "candidate_family": "camera",
                "procedure_type": "DYNAMIC_CAMERA",
            },
        }
    )

    assert instruction.startswith("base instruction")
    assert "SENSOR-FAMILY MISMATCH" in instruction
    assert "Leave this sensor-family branch" in instruction
    assert "alternative ADAS systems/components" in instruction
    assert "Do not continue deeper under this rejected family" in instruction


def test_non_mismatch_review_keeps_existing_instruction():
    module = _module()
    recovery.install(module)

    assert module._next_instruction_for_review({"decision": "CONTINUE_SEARCH"}) == "base instruction"
    assert (
        module._next_instruction_for_review(
            {
                "decision": "CONTINUE_SEARCH",
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

    assert "SYSTEM-FAMILY RECOVERY CONTRACT" in prompt
    assert "alternative ADAS systems/components" in prompt
    assert "live controls" in prompt
    assert "invent provider-specific labels or refs" in prompt
