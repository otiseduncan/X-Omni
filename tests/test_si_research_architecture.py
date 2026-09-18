"""Model-first boundary for service-information research.

X owns meaning; the runtime owns execution truth. These are static checks
that the production SI research path keeps that split: no scripted branch
choice, no procedure-word lists, no page-length classification, no title
matching, and no candidate page grading itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

from core.services import adas_si_harvest, adas_si_research
from core.services import research_navigator_agent as agent
from core.services import research_semantic_review as review

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "core" / "services"
PRODUCTION_SI_MODULES = (
    SERVICES / "adas_si_research.py",
    SERVICES / "research_navigator_agent.py",
    SERVICES / "research_semantic_review.py",
    SERVICES / "research_delegate.py",
)
# The scripted harvester's semantic knobs. They may exist only there.
HARVESTER_SEMANTICS = (
    "PROCEDURE_WORDS",
    "DOCUMENT_CHARS",
    "DOCUMENT_WITH_LINKS_CHARS",
    "MAX_DEPTH",
    "procedure_children",
    "QUICK_REFERENCE",
    "PICKER_URL",
)


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_scripted_harvester_is_legacy_and_never_registered(monkeypatch):
    assert adas_si_harvest.LEGACY is True
    assert adas_si_harvest.AUTHORITATIVE is False
    header = _source(SERVICES / "adas_si_harvest.py")[:1200]
    for word in ("LEGACY", "EXPERIMENTAL", "NON-DEFAULT", "NOT AUTHORITATIVE"):
        assert word in header
    main_source = _source(ROOT / "core" / "main.py")
    # ALLDATA is sunset: no environment switch registers the harvester any more.
    assert "XOMNI_LEGACY_SI_HARVEST" not in main_source
    assert "adas_si_harvest.start" not in main_source
    assert 'registry.register("adas_si_research", adas_si_research.start)' in main_source


def test_production_research_modules_carry_no_harvester_semantics():
    for path in PRODUCTION_SI_MODULES:
        source = _source(path)
        for token in HARVESTER_SEMANTICS:
            assert token not in source, f"{path.name} reintroduces {token}"


def test_the_scheduler_never_chooses_an_alldata_branch_or_grades_a_page():
    """The research job may read Calibration IQ and call the Navigator; it may
    not name a URL inside the provider, a menu, a link, or a page property."""
    source = _source(SERVICES / "adas_si_research.py")
    tree = ast.parse(source)
    strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not any("alldata.com" in text for text in strings)
    assert not any(text.casefold() in {"adas quick reference", "recent vehicles", "removal and replacement"} for text in strings)
    for forbidden in ("page_text", "elements", "title.casefold", "len(text)", "relevance"):
        assert forbidden not in source, forbidden
    # The objective the job builds is the requirement's own name plus the
    # research goal; nothing about where in ALLDATA it should live.
    target = adas_si_research.target_from_read(
        {
            "status": "verified",
            # A target is only exact with a VIN; year/make/model alone is
            # deliberately not one, so the fixture carries a real 17-character
            # VIN rather than relying on the looser identity that predated the
            # exact-VIN guard.
            "repair_order": {"RO": "1", "id": "x", "vin": "2HGFC2F69KH000001"},
            "raw": {"id": "x", "ro_number": "1",
                    "vehicle": {"year": 2024, "make": "Honda", "model": "Civic",
                                "vin": "2HGFC2F69KH000001"},
                    "calibrations": [{"id": "c", "title": "Millimeter Wave Radar", "determination": "REQUIRED"}]},
        }
    )
    objective = adas_si_research.objectives_for(target)[0]
    assert objective["requirement_label"] == "Millimeter Wave Radar"
    assert objective["topic"] == adas_si_research.research_goal("Millimeter Wave Radar")


def test_the_navigator_loop_holds_no_page_classifier():
    source = _source(SERVICES / "research_navigator_agent.py")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert not node.name.startswith(("classify", "is_procedure", "score_relevance")), node.name
    # Acceptance is the reviewer's verdict, checked only for structure.
    assert "review_accepted(verdict)" in source
    assert "len(page_text)" not in source and "len(text) >" not in source


def test_scrapex_contract_carries_mechanical_gates_not_semantic_scores():
    contract = _source(SERVICES / "scrapex.py")
    navigator = _source(SERVICES / "research_navigator_agent.py")
    for field in ("navigation_performed", "candidate_extracted", "content_extracted"):
        assert field in contract
    for removed in ("subject_verified", "procedure_leaf_verified", "matched_terms"):
        assert removed not in contract
        assert removed not in navigator


def test_the_reviewer_decides_type_and_python_only_validates_structure():
    source = _source(SERVICES / "research_semantic_review.py")
    # No regex or keyword table decides the classification or the procedure type.
    assert "import re" not in source
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called = ast.unparse(node.func)
            assert not called.endswith(("re.search", "re.match", "re.findall")), called
    # Every classification and procedure type is a schema enum the model fills.
    properties = review.REVIEW_TOOL_SCHEMA["function"]["parameters"]["properties"]
    assert set(properties["classification"]["enum"]) == set(review.CLASSIFICATIONS)
    assert set(properties["procedure_type"]["enum"]) == set(review.PROCEDURE_TYPES)
    assert "REMOVAL_REPLACEMENT" in review.CLASSIFICATIONS
    assert "UNCERTAIN" in review.DECISIONS


def test_candidate_titles_never_become_their_own_verification_topic():
    """Dependencies are pursued under the reviewer's named requirement, judged
    against the original objective; the loop never re-verifies a page against
    words taken from that same page."""
    source = _source(SERVICES / "research_navigator_agent.py")
    assert "topic=dependency[\"title\"]" in source
    assert "dependency_context" in source
    assert 'topic=result.get("title")' not in source
    assert "topic=summary.get(\"title\")" not in source


def test_model_facing_actions_follow_the_ref_mark_visual_hierarchy():
    description = agent.NAVIGATOR_AGENT_TOOL_SCHEMA["function"]["description"]
    assert "Prefer refs" in description
    assert "observe_marks" in description and "click_mark" in description
    assert "click_visual" in description
    actions = set(agent.NAVIGATOR_AGENT_TOOL_SCHEMA["function"]["parameters"]["properties"]["action"]["enum"])
    assert {"click", "observe_marks", "click_mark", "click_visual", "type", "select_vehicle", "extract", "done"} <= actions
    assert agent._OBSERVATION_BOUND_ACTIONS == frozenset({"click_mark", "click_visual"})


def test_service_information_descriptions_say_it_is_live():
    import yaml

    from core.tools.registry import TOOL_SCHEMAS

    raw = yaml.safe_load((ROOT / "config" / "tools.yaml").read_text(encoding="utf-8"))
    tools = raw["tools"]
    for name in ("calibration_iq_operator", "calibration_iq_work_prep", "delegate_research", "stage_action"):
        assert "dormant" not in str(tools[name]["description"]).casefold(), name
        assert "disabled" not in str(tools[name]["description"]).casefold(), name
    assert "adas_si_research" in tools and tools["adas_si_research"]["tier"] == "operator_authorized"
    assert tools["adas_si_research_status"]["tier"] == "read_only"
    assert "dormant" not in TOOL_SCHEMAS["calibration_iq_work_prep"]["description"].casefold()
    assert "research_si" in TOOL_SCHEMAS["calibration_iq_work_prep"]["description"]
    assert "LEGACY" in TOOL_SCHEMAS["adas_si_harvest"]["description"]
