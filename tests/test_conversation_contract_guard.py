from core.orchestrator import prompt
from core.services import conversation_contract_guard as guard
from core.tools import meta


def test_oem_questions_require_evidence_and_general_adas_si_does_not_inherit_ro():
    guard.install()
    contract = prompt.TRUTH_AND_AUTHORIZATION
    assert "manufacturer-specific ADAS requirement is not a generic knowledge question" in contract
    assert "check ADAS SI" in contract
    assert "do not inherit the active RO" in contract
    assert "Never say an SI/ADAS Map/research job is running" in contract
    assert "Never invent an RO number" in contract


def test_tool_descriptions_separate_general_research_from_ciq_si_work():
    guard.install()
    delegate = meta.DELEGATE_RESEARCH_SCHEMA["description"]
    stage = meta.stage_action_schema()["description"]
    status = meta.QUERY_CIQ_SCHEMA["description"]

    assert "General OEM/manufacturer ADAS questions belong here" in delegate
    assert "Do not select research_si merely because a prior RO is active" in stage
    assert "Never invent a repair_order_id" in stage
    assert "status proves service/data-plane health only" in status
    assert "must never be described as active ROs" in status
