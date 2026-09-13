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
    verdict = review.validate_review(_payload(confidence=0.912345, dependencies=[{"title": " Wheel Alignment ", "reason": "required first"}]))
    assert verdict["decision"] == "ACCEPT"
    assert verdict["confidence"] == 0.912
    assert verdict["dependencies"] == [{"title": "Wheel Alignment", "reason": "required first"}]
    assert verdict["malformed"] is False


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
        {"dependencies": [{"title": "x"}]},
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

    # A REJECT is never upgraded, whatever the table says.
    rejected = review.validate_review(_payload(decision="REJECT"))
    assert rejected["decision"] == "REJECT" and review.accepted(rejected) is False


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
