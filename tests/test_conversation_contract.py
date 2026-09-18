from core.orchestrator import prompt


def test_oem_questions_require_evidence_without_inheriting_active_ro():
    model_contract = prompt.MODEL_FIRST_CONTRACT
    truth_contract = prompt.TRUTH_AND_AUTHORIZATION

    assert "OEM-specific ADAS requirements or procedures need returned technical evidence" in model_contract
    assert "OEM/ADAS questions even when a CIQ RO is active" in model_contract
    assert "Only CIQ-attached RO procedure work is `research_si`" in model_contract
    assert "already-started `research_si` job finished is a status read" in model_contract
    assert "not a new `delegate_research` request" in model_contract
    assert "background-job state without a matching current-turn result" in truth_contract
