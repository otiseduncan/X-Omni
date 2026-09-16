from __future__ import annotations

from core.services import research_navigator_cycle_guard as guard


def test_page_state_revisit_is_detected_across_different_observations():
    state = guard._new_state()

    assert guard.record_page_state(
        state, observation_id="obs-a1", page_state="menu-a"
    ) is False
    assert guard.record_page_state(
        state, observation_id="obs-b1", page_state="menu-b"
    ) is False
    # A -> B -> A is a cycle even though A differs from the immediately
    # preceding B page.
    assert guard.record_page_state(
        state, observation_id="obs-a2", page_state="menu-a"
    ) is True
    assert state["revisit_count"] == 1


def test_second_internal_read_of_same_observation_does_not_create_false_revisit():
    state = guard._new_state()

    assert guard.record_page_state(
        state, observation_id="obs-a1", page_state="menu-a"
    ) is False
    assert guard.record_page_state(
        state, observation_id="obs-a1", page_state="menu-a"
    ) is False
    assert state["revisit_count"] == 0


def test_cycle_warning_changes_strategy_without_forcing_a_click():
    warning = guard.cycle_warning(2)

    assert "NAVIGATION CYCLE DETECTED" in warning
    assert "ADAS Quick Reference" in warning
    assert "if" in warning.casefold()
    assert "not a forced route" in warning
    assert "choose the next action" in warning
    assert "click ADAS Quick Reference" not in warning
