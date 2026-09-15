from core.orchestrator import prompt
from core.services import conversation_contract_guard as guard


def test_oem_questions_require_evidence_without_inheriting_active_ro():
    guard.install()
    contract = prompt.TRUTH_AND_AUTHORIZATION

    assert "OEM-specific ADAS requirements" in contract
    assert "explicit ADAS SI question" in contract
    assert "use delegate_research even if an RO is active" in contract
    assert "use research_si only to obtain/attach SI for a named CIQ RO or scope" in contract
    assert "Never claim background work is running or complete without a current result" in contract
    assert "never invent an RO identifier" in contract
