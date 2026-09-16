"""The independent reviewer: clean context, structured verdict, no self-grading."""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.services import research_semantic_review as review


def _payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "classification": "ACTUAL_PROCEDURE",
        "procedure_type": "STATIC_CAMERA",
        "vehicle_match": "MATCHES",
        "evidence": {field: "PRESENT" for field in review.EVIDENCE_FIELDS},
        "objective_match": "EXACT_MATCH",
        "dependencies": [],
        "decision": "ACCEPT",
        "confidence": 0.88,
        "evidence_summary": "Camera aiming with target board placement and scan tool steps.",
    }
    payload.update(overrides)
    return payload


def test_review_messages_hold_only_the_evidence_packet_and_the_stance():
    messages = review.build_review_messages(
        objective={"objective": "front camera calibration", "system": "Windshield camera", "dependency_context": None},
        vehicle={"year": 2026, "make": "Hyundai", "model": "Tucson", "vin": "KM8J3CA46SU000001"},
        candidate={
            "title": "Front Camera (ADAS) - Adjustment",
            "url": "https://my.alldata.com/a",
            "breadcrumb": ["Forward Looking Camera", "Adjustment"],
            "text": "Place the target 1.5 m ahead. " * 20,
            "referenced_links": ["Removal and Replacement", "Wheel Alignment"],
        },
        provider="alldata",
        screenshot=(b"\xff\xd8\xffx", "image/jpeg"),
    )
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "Do not assume this is the requested calibration procedure" in messages[0]["content"]
    text = messages[1]["content"][0]["text"]
    packet = json.loads(text.split("Evidence packet: ", 1)[1].split("\n\n", 1)[0])
    assert packet["research_objective"] == "front camera calibration"
    assert packet["exact_vehicle"] == "2026 Hyundai Tucson"
    assert packet["vin"] == "KM8J3CA46SU000001"
    assert packet["candidate_title"] == "Front Camera (ADAS) - Adjustment"
    assert packet["candidate_breadcrumb"] == ["Forward Looking Camera", "Adjustment"]
    assert packet["documents_referenced_on_page"] == ["Removal and Replacement", "Wheel Alignment"]
    assert "Place the target" in text
    assert messages[1]["content"][1]["type"] == "image_url"
    # Nothing that looks like a navigator transcript is present.
    assert "navigator_browse" not in json.dumps(messages)
    assert "do_not_repeat" not in json.dumps(messages)


def test_valid_verdicts_are_normalized_and_bounded():
    verdict = review.validate_review(_payload(confidence=0.912345, dependencies=[{"title": " Wheel Alignment ", "reason": "required first", "quote": "Perform the wheel alignment first."}]))
    assert verdict["decision"] == "ACCEPT"
    assert verdict["confidence"] == 0.912
    assert verdict["dependencies"] == [{"title": "Wheel Alignment", "reason": "required first", "quote": "Perform the wheel alignment first."}]
    assert verdict["malformed"] is False


def test_a_dependency_must_cite_a_sentence_that_is_on_the_page():
    page = "Before aiming, perform the wheel alignment first. Place the target 2.5 m ahead. RELATED INFORMATION: Removal and Replacement."
    grounded = {"title": "Wheel Alignment", "reason": "required first", "quote": "perform the wheel alignment first"}
    invented = {"title": "Removal and Replacement", "reason": "it is linked", "quote": "the sensor must be removed and replaced before aiming"}
    verdict = review.validate_review(_payload(decision="ACCEPT_WITH_DEPENDENCIES", dependencies=[grounded, invented]), page_text=page)
    assert [item["title"] for item in verdict["dependencies"]] == ["Wheel Alignment"]
    assert verdict["unsupported_dependencies"][0]["title"] == "Removal and Replacement"
    assert verdict["decision"] == "ACCEPT_WITH_DEPENDENCIES"

    # Every claimed dependency ungrounded: the page stays accepted on its own
    # evidence, without dependencies.
    only_invented = review.validate_review(_payload(decision="ACCEPT_WITH_DEPENDENCIES", dependencies=[invented]), page_text=page)
    assert only_invented["decision"] == "ACCEPT" and only_invented["dependencies"] == []
    assert only_invented["original_decision"] == "ACCEPT_WITH_DEPENDENCIES"
    assert review.accepted(only_invented) is True

    # FOLLOW_DEPENDENCY with nothing grounded cannot be followed.
    nothing_to_follow = review.validate_review(_payload(classification="GENERAL_DESCRIPTION", decision="FOLLOW_DEPENDENCY", dependencies=[invented]), page_text=page)
    assert nothing_to_follow["decision"] == "UNCERTAIN"
    # Without page text there is nothing to check against; the claim stands.
    unchecked = review.validate_review(_payload(decision="ACCEPT_WITH_DEPENDENCIES", dependencies=[invented]))
    assert [item["title"] for item in unchecked["dependencies"]] == ["Removal and Replacement"]


@pytest.mark.parametrize(
    "broken",
    [
        {"classification": "PROCEDURE"},
        {"decision": "MAYBE"},
        {"evidence": None},
        {"evidence": {"execution_steps": "YES"}},
        {"confidence": "high"},
        {"confidence": 1.4},
        {"evidence_summary": ""},
        {"dependencies": [{"title": "x", "quote": "some quoted sentence here"}]},
        {"procedure_type": "RADAR"},
    ],
)
def test_malformed_verdicts_are_refused(broken):
    with pytest.raises(review.SemanticReviewError):
        review.validate_review(_payload(**broken))
    with pytest.raises(review.SemanticReviewError):
        review.validate_review("not an object")


def test_an_accept_its_own_evidence_does_not_support_is_downgraded_never_promoted():
    unsupported = review.validate_review(_payload(evidence={**{f: "PRESENT" for f in review.EVIDENCE_FIELDS}, "execution_steps": "MISSING_OR_UNCERTAIN"}))
    assert unsupported["decision"] == "UNCERTAIN"
    assert unsupported["original_decision"] == "ACCEPT"
    assert any("execution_steps" in item for item in unsupported["inconsistent"])
    assert review.accepted(unsupported) is False

    wrong_kind = review.validate_review(_payload(classification="REMOVAL_REPLACEMENT"))
    assert wrong_kind["decision"] == "UNCERTAIN"

    wrong_vehicle = review.validate_review(_payload(vehicle_match="DIFFERENT_VEHICLE"))
    assert wrong_vehicle["decision"] == "UNCERTAIN"

    no_dependency = review.validate_review(_payload(decision="ACCEPT_WITH_DEPENDENCIES"))
    assert no_dependency["decision"] == "UNCERTAIN"
    assert no_dependency["original_decision"] == "ACCEPT_WITH_DEPENDENCIES"

    # A REJECT is never upgraded, whatever the table says.
    rejected = review.validate_review(_payload(decision="REJECT"))
    assert rejected["decision"] == "REJECT" and review.accepted(rejected) is False


# ------------------------------------------------ objective match and role
#
# These contracts used to live in three separate wrappers around the reviewer
# (a keyword radar/camera veto, a second "objective match" model call, and a
# primary/supporting boundary). They are one verdict now, checked in one place.


def test_a_camera_procedure_cannot_close_a_radar_objective():
    """The 2023 Accord front-radar run captured Multipurpose Camera Aiming.

    The reviewer called it a real procedure and accepted it. It is caught now
    because the reviewer must say what the page performs before it decides,
    and a page it says belongs to a different sensor family cannot be accepted.
    """
    verdict = review.validate_review(
        _payload(procedure_type="DYNAMIC_CAMERA", objective_match="DIFFERENT_SENSOR_FAMILY")
    )
    assert verdict["decision"] == "CONTINUE_SEARCH"
    assert verdict["original_decision"] == "ACCEPT"
    assert "objective_match=DIFFERENT_SENSOR_FAMILY" in verdict["inconsistent"]
    assert review.accepted(verdict) is False


@pytest.mark.parametrize("category", ["SAME_COMPONENT_WRONG_PROCEDURE", "DIFFERENT_COMPONENT"])
def test_a_real_procedure_for_the_wrong_target_keeps_the_search_going(category):
    verdict = review.validate_review(_payload(objective_match=category))
    assert verdict["decision"] == "CONTINUE_SEARCH"
    assert review.accepted(verdict) is False


def test_an_acceptance_that_never_states_the_objective_match_is_not_an_acceptance():
    # Fail closed: the grammar makes the field required, so a verdict without
    # it did not come from the reviewer's own contract.
    missing = _payload()
    missing.pop("objective_match")
    assert review.validate_review(missing)["decision"] == "UNCERTAIN"
    assert review.validate_review(_payload(objective_match="UNCERTAIN"))["decision"] == "UNCERTAIN"


def test_an_unknown_objective_match_is_malformed():
    with pytest.raises(review.SemanticReviewError):
        review.validate_review(_payload(objective_match="CLOSE_ENOUGH"))


def test_a_supporting_page_cannot_close_a_primary_objective():
    verdict = review.validate_review(_payload(classification="REQUIRED_SUPPORTING_PROCEDURE"))
    assert verdict["decision"] == "CONTINUE_SEARCH"
    assert review.accepted(verdict) is False


def test_a_supporting_page_may_close_the_dependency_task_that_asked_for_it():
    verdict = review.validate_review(
        _payload(classification="REQUIRED_SUPPORTING_PROCEDURE"), role="dependency"
    )
    assert verdict["decision"] == "ACCEPT"
    assert review.accepted(verdict) is True


def test_a_dependency_task_is_not_regraded_against_the_primary_objective():
    # A dependency task fetches a different, named document on purpose.
    verdict = review.validate_review(
        _payload(objective_match="DIFFERENT_COMPONENT"), role="dependency"
    )
    assert verdict["decision"] == "ACCEPT"


def test_an_actual_procedure_that_matches_still_closes_the_primary_objective():
    verdict = review.validate_review(_payload())
    assert verdict["decision"] == "ACCEPT"
    assert verdict["objective_match"] == "EXACT_MATCH"
    assert verdict["review_role"] == "primary"


def test_a_non_accepting_verdict_is_never_promoted_or_rewritten():
    for decision in ("CONTINUE_SEARCH", "REJECT", "UNCERTAIN"):
        verdict = review.validate_review(
            _payload(decision=decision, objective_match="DIFFERENT_SENSOR_FAMILY")
        )
        assert verdict["decision"] == decision
        assert "original_decision" not in verdict


def test_the_review_role_comes_from_cores_own_task_record():
    assert review.review_role({"objective": "x"}) == "primary"
    assert review.review_role({"objective": "x", "dependency_context": "Required document"}) == "dependency"
    assert review.review_role(None) == "primary"


def test_the_match_is_declared_before_the_decision():
    # llama.cpp emits properties in declared order, so the model must commit
    # to what the page performs before it may decide.
    order = list(review.REVIEW_TOOL_SCHEMA["function"]["parameters"]["properties"])
    assert order.index("objective_match") < order.index("decision")
    assert order.index("evidence") < order.index("decision")
    assert "objective_match" in review.REVIEW_TOOL_SCHEMA["function"]["parameters"]["required"]


def test_the_prompt_judges_a_page_by_its_steps_not_its_title():
    prompt = review.REVIEW_SYSTEM_PROMPT
    assert "not evidence in either direction" in prompt
    assert "Operation Check" in prompt
    # The shop's requirement label is not the manufacturer's term.
    assert "not from the vehicle manufacturer" in prompt
    # One prompt, written once -- not a base plus appended suffixes.
    assert prompt.count("DEPENDENCIES.") == 1
    assert prompt.count("WORKFLOW ROLE.") == 1


class _Client:
    def __init__(self, events: list[dict[str, Any]]):
        self.events = events
        self.requests: list[dict[str, Any]] = []

    async def stream(self, messages, tools=None, max_tokens=None, tool_choice=None):
        self.requests.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        for event in self.events:
            yield event


@pytest.mark.asyncio
async def test_review_candidate_reads_a_tool_call_verdict_and_records_usage():
    client = _Client([
        {"type": "tool_call", "id": "c1", "name": review.REVIEW_TOOL_NAME, "arguments": json.dumps(_payload(decision="CONTINUE_SEARCH", classification="GENERAL_DESCRIPTION"))},
        {"type": "usage", "usage": {"prompt_tokens": 3210}, "timings": {}},
    ])
    verdict = await review.review_candidate(
        client=client,
        objective={"objective": "blind spot calibration"},
        vehicle={"year": 2025, "make": "Toyota", "model": "Camry"},
        candidate={"title": "Blind Spot Monitor System", "url": "https://my.alldata.com/x", "text": "overview"},
        provider="alldata",
    )
    assert verdict["decision"] == "CONTINUE_SEARCH"
    assert verdict["review_prompt_tokens"] == 3210
    assert verdict["reviewed_title"] == "Blind Spot Monitor System"
    # One model call reviews one candidate; there is no second critic turn.
    assert len(client.requests) == 1
    request = client.requests[0]
    assert request["tools"][0]["function"]["name"] == review.REVIEW_TOOL_NAME
    assert request["tool_choice"] == "required"
    assert request["messages"][0]["role"] == "system"


@pytest.mark.asyncio
async def test_prose_or_unparseable_or_failing_reviews_are_uncertain_not_accepted():
    prose = await review.review_candidate(client=_Client([{"type": "content", "text": "Looks fine to me, accept it."}]), objective={"objective": "x"}, vehicle={}, candidate={"title": "t", "url": "u", "text": "body"}, provider="alldata")
    assert prose["decision"] == "UNCERTAIN" and prose["malformed"] is True
    assert review.accepted(prose) is False

    broken = await review.review_candidate(client=_Client([{"type": "tool_call", "id": "c", "name": review.REVIEW_TOOL_NAME, "arguments": "{not json"}]), objective={"objective": "x"}, vehicle={}, candidate={"title": "t", "url": "u", "text": "body"}, provider="alldata")
    assert broken["decision"] == "UNCERTAIN" and broken["malformed"] is True

    class _Failing:
        async def stream(self, *args, **kwargs):
            raise RuntimeError("worker down")
            yield  # pragma: no cover

    failed = await review.review_candidate(client=_Failing(), objective={"objective": "x"}, vehicle={}, candidate={"title": "t", "url": "u", "text": "body"}, provider="alldata")
    assert failed["decision"] == "UNCERTAIN" and "worker down" in failed["error"]


@pytest.mark.asyncio
async def test_review_candidate_applies_the_dependency_role_from_the_task_record():
    supporting = _payload(classification="REQUIRED_SUPPORTING_PROCEDURE")
    client = _Client([{"type": "tool_call", "id": "c", "name": review.REVIEW_TOOL_NAME, "arguments": json.dumps(supporting)}])
    primary = await review.review_candidate(client=client, objective={"objective": "radar aiming"}, vehicle={}, candidate={"title": "t", "url": "u", "text": "body"}, provider="alldata")
    assert primary["decision"] == "CONTINUE_SEARCH"

    client = _Client([{"type": "tool_call", "id": "c", "name": review.REVIEW_TOOL_NAME, "arguments": json.dumps(supporting)}])
    dependency = await review.review_candidate(
        client=client,
        objective={"objective": "radar aiming", "dependency_context": "Required document 'Wheel Alignment'"},
        vehicle={}, candidate={"title": "t", "url": "u", "text": "body"}, provider="alldata",
    )
    assert dependency["decision"] == "ACCEPT"
