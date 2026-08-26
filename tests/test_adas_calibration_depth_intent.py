from core.services import research_workflow
from core.tools.registry import TOOL_SCHEMAS


def test_adas_calibration_depth_is_an_explicit_structured_search_mode():
    search_mode = TOOL_SCHEMAS["adas_si_search"]["parameters"]["properties"]["search_mode"]
    assert set(search_mode["enum"]) == {"standard", "calibration_requirements"}


def test_calibration_depth_does_not_enable_fixed_full_research_route():
    research_workflow.install()
    actions = TOOL_SCHEMAS["collision_research"]["parameters"]["properties"]["action"]["enum"]
    assert "full_research" not in actions
