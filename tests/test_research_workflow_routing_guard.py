from core.orchestrator.loop import Orchestrator
from core.services import research_calibration_route, research_workflow_guard


def test_retired_research_route_guards_do_not_patch_the_turn_loop():
    base_run = Orchestrator._run  # noqa: SLF001
    research_workflow_guard.install()
    research_calibration_route.install()
    assert Orchestrator._run is base_run  # noqa: SLF001
