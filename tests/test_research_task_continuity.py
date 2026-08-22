from pathlib import Path

from core.services import research_task, research_task_continuity


def test_store_roundtrips_a_task(tmp_path: Path):
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


def test_task_is_stale_after_the_turn_budget():
    task = research_task.ResearchTask(conversation_id="1", turn_count_at_update=5)
    assert task.is_stale(6) is False
    assert task.is_stale(12) is True


def _f350_task(turn_count_at_update: int = 4) -> research_task.ResearchTask:
    return research_task.ResearchTask(
        conversation_id="1",
        vehicle_year="2018",
        vehicle_make="Ford",
        vehicle_model_trim="F-350",
        vehicle_label="2018 Ford F-350",
        subject="forward facing camera calibration",
        turn_count_at_update=turn_count_at_update,
    )


def test_show_me_the_exact_procedure_merges_the_active_vehicle_and_subject():
    resolved = research_task_continuity.merge_active_task(
        "show me the exact procedure", _f350_task(), current_turn_count=5
    )
    assert "2018 Ford F-350" in resolved
    assert "forward facing camera calibration" in resolved
    assert "show me the exact procedure" in resolved


def test_check_alldata_for_it_merges_the_active_vehicle_and_subject():
    resolved = research_task_continuity.merge_active_task(
        "check ALLDATA for it", _f350_task(), current_turn_count=5
    )
    assert "2018 Ford F-350" in resolved
    assert "forward facing camera calibration" in resolved


def test_a_message_naming_its_own_different_vehicle_is_never_merged():
    resolved = research_task_continuity.merge_active_task(
        "now check the 2021 Jeep Cherokee blind spot monitor",
        _f350_task(),
        current_turn_count=5,
    )
    assert resolved == "now check the 2021 Jeep Cherokee blind spot monitor"


def test_a_stale_task_is_not_merged():
    resolved = research_task_continuity.merge_active_task(
        "show me the exact procedure", _f350_task(turn_count_at_update=1), current_turn_count=20
    )
    assert resolved == "show me the exact procedure"


def test_no_active_task_returns_the_message_unchanged():
    resolved = research_task_continuity.merge_active_task(
        "show me the exact procedure", None, current_turn_count=1
    )
    assert resolved == "show me the exact procedure"


def test_a_fully_unrelated_new_request_is_not_treated_as_a_continuation():
    assert research_task_continuity.looks_like_continuation(
        "what's the weather like this weekend"
    ) is False
