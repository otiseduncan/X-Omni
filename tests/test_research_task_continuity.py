from pathlib import Path

from core.orchestrator.loop import Orchestrator
from core.services import research_task, research_task_continuity


def test_legacy_research_task_store_roundtrips_without_rewriting_chat(tmp_path: Path):
    store = research_task.ResearchTaskStore(tmp_path / "research_tasks.json")
    task = research_task.ResearchTask(
        conversation_id="42",
        vehicle_year="2018",
        vehicle_make="Ford",
        vehicle_model_trim="F-350",
        vehicle_label="2018 Ford F-350",
        subject="forward facing camera calibration",
        alldata_status="vehicle_selection_required",
        turn_count_at_update=3,
    )
    store.save(task)

    loaded = store.get("42")
    assert loaded is not None
    assert loaded.vehicle_label == "2018 Ford F-350"
    assert loaded.alldata_status == "vehicle_selection_required"
    assert store.get("does-not-exist") is None


def test_retired_continuity_installer_does_not_wrap_orchestrator_run():
    base_run = Orchestrator._run  # noqa: SLF001
    research_task_continuity.install()
    assert Orchestrator._run is base_run  # noqa: SLF001
    assert Orchestrator._run.__module__ == "core.orchestrator.loop"  # noqa: SLF001
