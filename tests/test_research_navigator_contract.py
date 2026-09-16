"""The Navigator's structural execution contract, as plain functions.

These rules used to be five wrappers installed around the Navigator module and
the ScrapeX client at import time. Their contracts are kept here, unchanged in
intent, against the functions the loop now calls directly.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from core.services import research_navigator_contract as contract

COMPONENT_LANDING = "https://my.alldata.com/repair/#/vehicle/64505/component/7821/filter/noFilter"
ARTICLE = (
    "https://my.alldata.com/repair/#/article/64505/component/3399/itype/376/"
    "nonstandard/317397/isSelfReferenceLink/false"
)
GUID = "https://my.alldata.com/repair/#/article/64505/guid/na-ty2016-2023tacom-RM100000001G149_html"
PICKER = "https://my.alldata.com/repair/#/select-vehicle"


# ------------------------------------------------------------------ routes


def test_component_landing_and_document_routes_are_distinct():
    assert contract.is_routing_page(COMPONENT_LANDING) is True
    assert contract.is_routing_page(ARTICLE) is False
    assert contract.is_document_route(ARTICLE) is True
    assert contract.is_document_route(GUID) is True
    assert contract.is_document_route(PICKER) is False
    assert contract.is_routing_page(PICKER) is False


# -------------------------------------------------------------- completion


def test_done_is_refused_on_a_component_page_until_a_candidate_was_submitted():
    refusal = contract.done_refusal(COMPONENT_LANDING, candidate_submitted=False)
    assert refusal is not None
    assert refusal["refused"]["code"] == "premature_done_on_routing_page"
    assert "Reaching the right component is not completion" in refusal["error"]
    assert contract.done_refusal(COMPONENT_LANDING, candidate_submitted=True) is None


def test_done_is_refused_on_an_untested_article_or_guid_route():
    for url in (ARTICLE, GUID):
        refusal = contract.done_refusal(url, candidate_submitted=False)
        assert refusal is not None
        assert refusal["refused"]["code"] == "premature_done_on_untested_article"
        assert "article/guid" in refusal["error"]
        assert contract.done_refusal(url, candidate_submitted=True) is None


def test_done_may_end_a_task_on_the_vehicle_picker_without_a_candidate():
    # Genuinely not finding the vehicle is an explicit failure, not a routing page.
    assert contract.done_refusal(PICKER, candidate_submitted=False) is None


# -------------------------------------------------------------- end of page


def _at(bottom: bool) -> dict:
    return {"scroll_position": {"scroll_y": 900, "scroll_height": 1000, "at_page_bottom": bottom}}


def test_a_downward_scroll_at_the_bottom_is_refused():
    refusal = contract.bottom_scroll_refusal(_at(True), 1600)
    assert refusal is not None
    assert refusal["refused"]["code"] == "scroll_at_page_bottom"
    assert "extract it for independent review" in refusal["error"]


def test_an_upward_scroll_or_a_page_with_more_below_is_allowed():
    assert contract.bottom_scroll_refusal(_at(True), -800) is None
    assert contract.bottom_scroll_refusal(_at(False), 1600) is None
    assert contract.bottom_scroll_refusal({}, 1600) is None
    assert contract.bottom_scroll_refusal(_at(True), True) is None  # bool is not a delta


# ------------------------------------------------------------------ cycles


def test_a_return_to_an_earlier_page_state_is_a_revisit_through_any_route():
    memory = contract.TaskMemory()
    assert memory.record("obs-a", "page-A") is False
    assert memory.record("obs-b", "page-B") is False
    # A -> B -> A: a different observation of an earlier state.
    assert memory.record("obs-a2", "page-A") is True
    assert memory.revisit_count == 1


def test_reading_the_same_observation_twice_is_not_a_revisit():
    memory = contract.TaskMemory()
    assert memory.record("obs-a", "page-A") is False
    assert memory.record("obs-a", "page-A") is False
    assert memory.revisit_count == 0


def test_the_cycle_warning_suggests_a_different_strategy_without_forcing_a_click():
    warning = contract.cycle_warning(2)
    assert "NAVIGATION CYCLE" in warning
    assert "manufacturer's own name" in warning
    assert "ADAS Quick Reference is visible in THIS observation" in warning
    assert "Revisits so far: 2" in warning


# ----------------------------------------------------------------- vehicle


MANAGED = SimpleNamespace(scrapex_project_path=r"X:\ScrapeX")
UNMANAGED = object()
VIN = {"vin": "1HGCY1F35PA033515"}


def test_every_managed_primary_task_with_an_exact_vin_is_anchored():
    assert contract.must_anchor_vehicle(MANAGED, VIN, "primary") is True


def test_a_dependency_task_keeps_the_verified_vehicle_and_page():
    assert contract.must_anchor_vehicle(MANAGED, VIN, "dependency") is False


def test_without_an_exact_vin_there_is_nothing_to_anchor_to():
    assert contract.must_anchor_vehicle(MANAGED, {"year": 2014, "make": "GMC", "model": "Acadia"}, "primary") is False
    assert contract.must_anchor_vehicle(MANAGED, {"vin": "TOO-SHORT"}, "primary") is False
    assert contract.exact_vin({"vin": " 1hgcy1f35pa033515 "}) == "1HGCY1F35PA033515"


def test_an_unmanaged_harness_keeps_its_hermetic_vehicle_behaviour():
    assert contract.must_anchor_vehicle(UNMANAGED, VIN, "primary") is False


# ------------------------------------------------------------- candidates


def _verdict(decision: str, **extra) -> dict:
    return {"decision": decision, "classification": "REQUIRED_SUPPORTING_PROCEDURE",
            "evidence_summary": "Generic initialization page, not the BSM procedure.", **extra}


def test_a_reviewed_page_is_recognised_by_its_url_and_the_text_that_was_reviewed():
    reviewed = contract.ReviewedCandidates()
    reviewed.remember("https://x/init", "sha-1", "Repair Instruction - Initialization", _verdict("CONTINUE_SEARCH"))
    assert reviewed.prior("https://x/init", "sha-1")["decision"] == "CONTINUE_SEARCH"
    # The same URL after more text loaded is a genuinely new candidate.
    assert reviewed.prior("https://x/init", "sha-2") is None


def test_only_pages_not_accepted_are_briefed_to_a_fresh_task():
    reviewed = contract.ReviewedCandidates()
    reviewed.remember("https://x/init", "a", "Repair Instruction - Initialization", _verdict("CONTINUE_SEARCH", objective_match="DIFFERENT_COMPONENT"))
    reviewed.remember("https://x/ok", "b", "Operation Check", _verdict("ACCEPT"))
    briefing = reviewed.briefing()
    assert "Repair Instruction - Initialization" in briefing
    assert "DIFFERENT_COMPONENT" in briefing
    assert "Operation Check" not in briefing
    assert contract.ReviewedCandidates().briefing() == ""


def test_a_repeated_candidate_reissues_the_earlier_verdict_and_says_so():
    reviewed = contract.ReviewedCandidates()
    reviewed.remember("https://x/init", "a", "Init", _verdict("CONTINUE_SEARCH"))
    verdict = contract.repeated_candidate_verdict(reviewed.prior("https://x/init", "a"))
    assert verdict["decision"] == "CONTINUE_SEARCH"
    assert verdict["repeated_candidate"] is True
    assert "already reviewed for this objective" in verdict["evidence_summary"]


def test_a_retry_briefing_names_the_attempt_the_stop_and_the_rejected_pages():
    reviewed = contract.ReviewedCandidates()
    reviewed.remember("https://x/init", "a", "Repair Instruction - Initialization", _verdict("CONTINUE_SEARCH"))
    note = contract.retry_goal_note(2, {"agent_stopped_reason": "stalled"}, reviewed)
    assert "attempt 2" in note
    assert "stalled" in note
    assert "re-anchored to the exact vehicle" in note
    assert "Repair Instruction - Initialization" in note


# ------------------------------------------------------------ architecture


ROOT = Path(__file__).resolve().parents[1]


def test_the_navigator_wrappers_this_contract_replaces_are_gone():
    services = ROOT / "core" / "services"
    for retired in (
        "research_navigator_cycle_guard.py",
        "research_navigator_completion_guard.py",
        "research_bottom_decision_guard.py",
        "research_navigator_vehicle_anchor.py",
        "research_dependency_budget_extension.py",
        "research_semantic_system_guard.py",
        "research_objective_match_guard.py",
        "research_primary_procedure_guard.py",
        "research_dependency_semantic_guard.py",
    ):
        assert not (services / retired).exists(), retired
    init = (services / "__init__.py").read_text(encoding="utf-8")
    assert "_research_navigator_agent)" not in init, "no installer may rebind the Navigator module"
    assert "_research_semantic_review." not in init


def test_the_navigator_and_reviewer_modules_are_not_rebound_after_import():
    from core.services import research_navigator_agent as agent
    from core.services import research_semantic_review as review

    # The functions the loop calls are the ones defined in its own module.
    for name in ("_run_task", "_system_prompt", "_observation_summary", "_page_state",
                 "_visual_observation_content", "_next_instruction_for_review", "run_navigator_search"):
        assert getattr(agent, name).__module__ == agent.__name__, name
    assert agent.review_candidate is review.review_candidate
    assert review.review_candidate.__module__ == review.__name__
