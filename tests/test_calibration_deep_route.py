from core.services import research_workflow
from core.tools.registry import TOOL_SCHEMAS


def test_calibration_research_keeps_source_selection_model_driven():
    research_workflow.install()
    actions = TOOL_SCHEMAS["collision_research"]["parameters"]["properties"]["action"]["enum"]
    assert "full_research" not in actions
    assert "public_search" in actions
    assert "public_read" in actions
