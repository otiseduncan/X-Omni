"""Technical research is a first-class conversation subject.

A delegate_research result establishes structured technical state -- vehicle,
system, objective, outcome, what accepted evidence said, what is still open --
that follow-ups ("Show me the procedure", "Does the bumper stay on?", "What
about 2024?", "No, I meant BSM", "Where does it say that?") resolve against.
It joins an active RO subject without replacing it, survives an RO change, and
is rendered in its own bounded prompt section so a large RO context can never
crowd it out. Nothing here parses user text: the model reads the state.
"""

from __future__ import annotations

import json
from typing import Any

from core.orchestrator import prompt
from core.services import conversation_working_context as working_context
from core.services.conversation_subjects import track_active_subject_from_tool_result
from core.state.db import Store
from core.tools import meta

K4 = {"year": 2025, "make": "Kia", "model": "K4", "label": "2025 Kia K4"}


def _research(
    *,
    objective: str = "Does the front bumper stay on for front radar calibration?",
    system: str = "front radar",
    outcome: str = "SATISFIED",
    vehicle: dict[str, Any] | None = None,
    accepted: bool = True,
    deliverable: str = "answer",
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "status": outcome.casefold(),
        "verified": outcome == "SATISFIED",
        "objective": objective,
        "deliverable": deliverable,
        "vehicle": vehicle or K4,
        "system": system,
        "sources_checked": ["automotive_knowledge", "adas_si"],
        "unresolved": None if outcome == "SATISFIED" else ["the aiming target distance"],
        "findings": [
            {
                "source": "adas_si",
                "title": "Front Radar Bumper Requirement",
                "page": 1,
                "relative_path": "2025/Kia/K4/Front Radar Bumper Requirement.pdf",
                "url": "/api/adas-si/document?path=x&token=secret",
                "accepted": accepted,
                "excerpt": "K4 | 2025 | OFF",
                "evaluation": {
                    "outcome": outcome if accepted else "UNSATISFIED",
                    "source_answer": "The front bumper comes off for front radar calibration.",
                    "anchor_quote": "K4 | 2025 | OFF",
                    "stage": "calibration",
                    "reasons": [] if accepted else ["the source is related but does not answer the objective"],
                },
            }
        ],
    }


def _ro_result() -> dict[str, Any]:
    return {
        "status": "verified",
        "repair_order": {"id": "ro-1", "RO": "2400911777", "Vehicle": "2025 Kia K4", "version": 3},
        "raw": {"id": "ro-1", "ro_number": "2400911777", "version": 3},
    }


def _track(store: Store, conversation_id: int, tool_name: str, result: dict[str, Any]):
    return track_active_subject_from_tool_result(
        store, conversation_id=conversation_id, tool_name=tool_name, result=result, tool_call_id="call-1"
    )


def _section(row: dict[str, Any]) -> dict[str, Any]:
    return row["payload"]["working_context"]["sections"][working_context.TECHNICAL_RESEARCH_TYPE]


def test_research_creates_a_technical_subject_with_what_the_evidence_established(tmp_path) -> None:
    store = Store(tmp_path / "state.sqlite")
    conversation_id = store.create_conversation()
    row = _track(store, conversation_id, "delegate_research", _research())
    assert row["payload"]["type"] == "technical_research"
    section = _section(row)
    assert section["vehicle"]["label"] == "2025 Kia K4"
    assert section["system"] == "front radar"
    assert section["outcome"] == "SATISFIED"
    established = section["established"][0]
    assert established["says"].startswith("The front bumper comes off")
    assert established["anchor"] == "K4 | 2025 | OFF"
    assert established["page"] == 1
    # Query tokens never become durable context.
    assert "secret" not in json.dumps(row["payload"])
    store.close()


def test_a_related_document_that_was_not_accepted_is_remembered_as_not_accepted(tmp_path) -> None:
    store = Store(tmp_path / "state.sqlite")
    conversation_id = store.create_conversation()
    row = _track(store, conversation_id, "delegate_research", _research(outcome="UNSATISFIED", accepted=False))
    section = _section(row)
    assert "established" not in section
    assert section["retrieved_not_accepted"][0]["why"].startswith("the source is related")
    store.close()


def test_research_joins_the_active_ro_without_replacing_it_and_survives_an_ro_change(tmp_path) -> None:
    store = Store(tmp_path / "state.sqlite")
    conversation_id = store.create_conversation()
    _track(store, conversation_id, "calibration_iq_ro", _ro_result())
    row = _track(store, conversation_id, "delegate_research", _research())
    assert row["payload"]["type"] == "calibration_iq.repair_order"
    assert row["payload"]["resource_id"] == "ro-1"
    assert _section(row)["system"] == "front radar"

    other_ro = _ro_result()
    other_ro["repair_order"] = {**other_ro["repair_order"], "id": "ro-2", "RO": "2400911778"}
    other_ro["raw"] = {**other_ro["raw"], "id": "ro-2", "ro_number": "2400911778"}
    changed = _track(store, conversation_id, "calibration_iq_ro", other_ro)
    assert changed["payload"]["resource_id"] == "ro-2"
    assert _section(changed)["objective"].startswith("Does the front bumper")
    store.close()


def test_a_correction_to_the_system_keeps_the_vehicle_and_replaces_the_subject(tmp_path) -> None:
    """ "No, I meant BSM": the model researches again with the same vehicle and
    a different system; the subject follows the newest result."""

    store = Store(tmp_path / "state.sqlite")
    conversation_id = store.create_conversation()
    _track(store, conversation_id, "delegate_research", _research())
    row = _track(
        store,
        conversation_id,
        "delegate_research",
        _research(objective="Does BSM need calibration after rear bumper R&I?", system="blind spot monitoring", outcome="PARTIAL"),
    )
    section = _section(row)
    assert section["system"] == "blind spot monitoring"
    assert section["vehicle"]["label"] == "2025 Kia K4"
    assert section["outcome"] == "PARTIAL"
    assert section["unresolved"] == ["the aiming target distance"]
    store.close()


def test_the_technical_subject_has_its_own_bounded_prompt_section(tmp_path) -> None:
    store = Store(tmp_path / "state.sqlite")
    conversation_id = store.create_conversation()
    _track(store, conversation_id, "calibration_iq_ro", _ro_result())
    row = _track(store, conversation_id, "delegate_research", _research())
    sections = prompt.turn_context_sections([], row)
    research = sections["technical_research"]
    assert research.startswith("## Active technical research")
    assert "2025 Kia K4" in research and "front radar" in research and "K4 | 2025 | OFF" in research
    assert len(research) <= prompt.TECHNICAL_RESEARCH_CONTEXT_MAX_CHARS
    # The RO envelope no longer carries it, so an oversized RO context cannot
    # collapse the technical subject to an identity stub.
    assert "technical_research" not in sections["active_subject"]

    # A research-only subject is not rendered as an empty RO shell.
    store2 = Store(tmp_path / "other.sqlite")
    other = store2.create_conversation()
    only = _track(store2, other, "delegate_research", _research())
    only_sections = prompt.turn_context_sections([], only)
    assert "active_subject" not in only_sections and "technical_research" in only_sections

    # Budget-constrained rendering drops detail, never the identity of the subject.
    tight = prompt._technical_research_context(row, 700)  # noqa: SLF001
    assert tight and "2025 Kia K4" in tight
    store.close()
    store2.close()


def test_the_prompt_tells_x_to_carry_technical_follow_ups() -> None:
    text = prompt.WORKING_CONTEXT
    assert "Active technical research carries technical follow-ups" in text
    assert "keep what Otis did not change" in text
    assert "call `delegate_research`" in text
    assert "vehicle or repair description by itself is a technical subject" in text
    assert "11774" not in text
    assert "Do not query CIQ for a technical follow-up" in text

    ciq_description = meta.QUERY_CIQ_SCHEMA["description"]
    ro_description = meta.QUERY_CIQ_SCHEMA["parameters"]["properties"]["repair_order_id"]["description"]
    assert "technical-research subject is not an RO" in ciq_description
    assert "year/make/model" in ro_description


def test_an_ro_read_needs_an_ro_the_conversation_actually_supplied() -> None:
    from core.orchestrator.loop import unsupplied_repair_order

    static = {"role": "system", "content": "prompt example such as 11774 in Warner Robins"}
    vehicle_only = [static, {"role": "user", "content": "I've got a 2025 Kia K4 in with rear bumper damage."}]
    # An invented identifier -- even one the static prompt happens to contain -- is refused.
    assert unsupplied_repair_order({"repair_order_id": "11774"}, vehicle_only) == "11774"
    assert unsupplied_repair_order({"repair_order_id": "25K4-001"}, vehicle_only) == "25K4-001"
    # Otis's own short form, spoken with spacing, and ids a prior result returned pass.
    named = [static, {"role": "user", "content": "check 11 774 in Warner Robins"}]
    assert unsupplied_repair_order({"repair_order_id": "11774", "shop": "Warner Robins"}, named) is None
    from_result = [static, {"role": "tool", "content": '{"repair_order": {"id": "ro-uuid-9"}}'}]
    assert unsupplied_repair_order({"repair_order_id": "ro-uuid-9"}, from_result) is None
