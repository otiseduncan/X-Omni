from core.orchestrator import prompt
from core.services import conversation_contract_guard as guard


def test_oem_questions_require_evidence_without_inheriting_active_ro():
    guard.install()

    model_contract = prompt.MODEL_FIRST_CONTRACT
    truth_contract = prompt.TRUTH_AND_AUTHORIZATION

    assert "OEM-specific ADAS requirements or procedures need returned technical evidence" in model_contract
    assert "general OEM/ADAS SI questions even when a CIQ RO is active" in model_contract
    assert "Only CIQ-attached RO procedure work is `research_si`" in model_contract
    assert "background-job state without a matching current-turn result" in truth_contract
